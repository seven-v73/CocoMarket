"""Côté caisse : enregistrement local de la vente (outbox) puis envoi au broker.

Principe "store and forward" : la vente est d'abord écrite sur le disque de la caisse (table outbox),
PUIS envoyée. Si la liaison est coupée, elle reste 'pending' et un agent de relance la renvoie
automatiquement dès que le broker est de nouveau joignable. Une vente ne peut donc jamais être perdue.
"""
import datetime as dt
import json
import threading
import time
import uuid

import config
from broker import get_broker
from db import db, now_iso
from errors import BrokerUnavailable

_send_lock = threading.Lock()


# ---------- liaison Internet (simulée par magasin) ----------
def link_up(store):
    if config.LINK_MODE == "auto":
        try:
            get_broker().stats()
            return True
        except Exception:
            return False
    with db("caisse") as c:
        r = c.execute("SELECT up FROM link WHERE store=?", (store,)).fetchone()
        return True if r is None else bool(r["up"])


def set_link(store, up):
    with db("caisse") as c:
        c.execute("INSERT INTO link VALUES(?,?) ON CONFLICT(store) DO UPDATE SET up=excluded.up", (store, 1 if up else 0))


# ---------- création d'une vente ----------
def make_sale(user, lines, ts=None):
    """user: dict(username, store, caisse). lines: [{sku, qty}]. Retourne (id_vente, accepté: bool)."""
    if not lines:
        raise ValueError("Le ticket est vide.")
    if len(lines) > 100:
        raise ValueError("Le ticket ne peut pas dépasser 100 lignes.")
    with db("siege") as c:
        prices = {r["sku"]: r["price"] for r in c.execute("SELECT sku, price FROM products")}
        stores = {r["code"]: r["caisses"] for r in c.execute("SELECT code, caisses FROM stores")}
    store = user.get("store")
    try:
        caisse = int(user.get("caisse"))
    except (TypeError, ValueError):
        raise ValueError("Numéro de caisse invalide.")
    if store not in stores or not 1 <= caisse <= stores[store]:
        raise ValueError("Magasin ou numéro de caisse invalide.")
    clean, total = [], 0
    positions = {}
    for ln in lines:
        sku, qty = ln.get("sku"), ln.get("qty")
        if sku not in prices:
            raise ValueError(f"Produit inconnu : {sku}")
        if not isinstance(qty, int) or isinstance(qty, bool) or not 1 <= qty <= 999:
            raise ValueError(f"Quantité invalide pour {sku}")
        if sku in positions:
            clean[positions[sku]]["qte"] += qty
        else:
            positions[sku] = len(clean)
            clean.append({"produit": sku, "qte": qty, "prix": prices[sku]})
        total += qty * prices[sku]

    ts = ts or now_iso()
    prefix = f"{store}-C{caisse:02d}-{ts[:10].replace('-', '')}-"
    id_vente = f"{prefix}{uuid.uuid4().hex[:8]}"
    payload = {
        "id_vente": id_vente,
        "magasin": store,
        "caisse": caisse,
        "caissier": user["username"],
        "date": ts,
        "lignes": clean,
        "total": total,
    }
    with db("caisse") as c:
        c.execute(
            "INSERT INTO outbox(id_vente,store,caisse,cashier,ts,total,payload,created) VALUES(?,?,?,?,?,?,?,?)",
            (id_vente, store, caisse, user["username"], ts, total, json.dumps(payload, ensure_ascii=False), time.time()),
        )
    return id_vente, send(id_vente)


# ---------- envoi ----------
def send(id_vente):
    """Tente d'envoyer une vente de l'outbox. True si le broker a confirmé la réception."""
    with _send_lock:
        with db("caisse") as c:
            row = c.execute("SELECT * FROM outbox WHERE id_vente=?", (id_vente,)).fetchone()
        if row is None or row["status"] == "sent":
            return row is not None

    try:
        if not link_up(row["store"]):
            raise BrokerUnavailable("Liaison Internet coupée")
        get_broker().publish(json.loads(row["payload"]))  # confirmé par le broker (publisher confirm)
    except BrokerUnavailable as e:
        with _send_lock:
            with db("caisse") as c:
                c.execute("UPDATE outbox SET attempts=attempts+1, last_error=? WHERE id_vente=?", (str(e)[:200], id_vente))
        return False
    with _send_lock:
        with db("caisse") as c:
            c.execute("UPDATE outbox SET status='sent', sent_at=?, last_error=NULL, attempts=attempts+1 WHERE id_vente=?",
                      (now_iso(), id_vente))
    return True


def flush_pending(store=None):
    """Renvoie, dans l'ordre, les ventes en attente. S'arrête pour un magasin dès le premier échec."""
    with db("caisse") as c:
        if store:
            rows = c.execute("SELECT id_vente, store FROM outbox WHERE status='pending' AND store=? ORDER BY created", (store,)).fetchall()
        else:
            rows = c.execute("SELECT id_vente, store FROM outbox WHERE status='pending' ORDER BY created").fetchall()
    blocked, sent = set(), 0
    for r in rows:
        if r["store"] in blocked:
            continue
        if send(r["id_vente"]):
            sent += 1
        else:
            blocked.add(r["store"])
    return sent


def start_flusher():
    def loop():
        while True:
            try:
                flush_pending()
            except Exception:
                pass
            time.sleep(config.FLUSH_INTERVAL)

    threading.Thread(target=loop, daemon=True, name="caisse-flusher").start()


# ---------- outils de test (démonstration) ----------
def resend_duplicate(id_vente, store):
    """Republie volontairement un ticket déjà envoyé, pour démontrer le rejet des doublons."""
    with db("caisse") as c:
        row = c.execute("SELECT payload FROM outbox WHERE id_vente=? AND store=?", (id_vente, store)).fetchone()
    if row is None:
        raise ValueError("Ticket introuvable")
    if not link_up(store):
        raise BrokerUnavailable("Liaison Internet coupée")
    get_broker().publish(json.loads(row["payload"]))


def send_invalid(user):
    """Publie volontairement une vente invalide (produit inconnu) pour démontrer la Dead Letter Queue."""
    if not link_up(user["store"]):
        raise BrokerUnavailable("Liaison Internet coupée")
    stamp = dt.datetime.now().strftime("%H%M%S%f")
    get_broker().publish({
        "id_vente": f"{user['store']}-C{int(user['caisse']):02d}-INVALIDE-{stamp}",
        "magasin": user["store"], "caisse": int(user["caisse"]), "caissier": user["username"],
        "date": now_iso(), "lignes": [{"produit": "PRODUIT-INCONNU", "qte": 1, "prix": 1000}], "total": 1000,
    })


def tickets(store, caisse, limit=15):
    with db("caisse") as c:
        rows = c.execute(
            "SELECT id_vente, ts, total, status, attempts, last_error FROM outbox WHERE store=? AND caisse=? ORDER BY created DESC LIMIT ?",
            (store, caisse, limit),
        ).fetchall()
        pending = c.execute("SELECT COUNT(*) FROM outbox WHERE store=? AND status='pending'", (store,)).fetchone()[0]
    return [dict(r) for r in rows], pending
