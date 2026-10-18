"""Côté siège : traitement d'une vente reçue du broker.

Ordre des protections :
 1. panne simulée de la base  -> TransientError  (le broker retente plus tard)
 2. validation du message     -> PermanentError  (direction la DLQ, inutile de réessayer)
 3. idempotence               -> un id_vente déjà traité est ignoré (puis ACK) : zéro doublon
 4. écriture atomique         -> vente + lignes + stock + marqueur "traité" dans UNE transaction
"""
import datetime as dt

from broker import get_broker
from db import db, get_setting, log_event, now_iso
from errors import PermanentError, TransientError


def _validate(body, stores, prices):
    if not isinstance(body, dict):
        raise PermanentError("le message n'est pas un objet JSON")
    for k in ("id_vente", "magasin", "caisse", "date", "lignes", "total"):
        if k not in body:
            raise PermanentError(f"champ manquant : {k}")
    if not isinstance(body["id_vente"], str) or not body["id_vente"].strip() or len(body["id_vente"]) > 128:
        raise PermanentError("id_vente invalide")
    if body["magasin"] not in stores:
        raise PermanentError(f"magasin inconnu : {body['magasin']}")
    if not isinstance(body["caisse"], int) or isinstance(body["caisse"], bool):
        raise PermanentError("numéro de caisse invalide")
    if not 1 <= body["caisse"] <= stores[body["magasin"]]:
        raise PermanentError(f"caisse invalide pour {body['magasin']}")
    try:
        dt.datetime.fromisoformat(body["date"])
    except (TypeError, ValueError):
        raise PermanentError("date invalide")
    if not isinstance(body["total"], int) or isinstance(body["total"], bool) or body["total"] < 0:
        raise PermanentError("total invalide")
    lignes = body["lignes"]
    if not isinstance(lignes, list) or not lignes:
        raise PermanentError("aucune ligne de vente")
    if len(lignes) > 100:
        raise PermanentError("trop de lignes de vente")
    total = 0
    for ln in lignes:
        if not isinstance(ln, dict) or ln.get("produit") not in prices:
            raise PermanentError(f"produit inconnu : {ln.get('produit') if isinstance(ln, dict) else ln}")
        q, p = ln.get("qte"), ln.get("prix")
        if not isinstance(q, int) or isinstance(q, bool) or not 1 <= q <= 999 or not isinstance(p, int) or isinstance(p, bool) or p < 0:
            raise PermanentError(f"quantité ou prix invalide pour {ln['produit']}")
        if p != prices[ln["produit"]]:
            raise PermanentError(f"prix incohérent pour {ln['produit']}")
        total += q * p
    if total != body["total"]:
        raise PermanentError(f"total incohérent ({body['total']} annoncé, {total} calculé)")


def handle(body):
    if get_setting("hq_down") == "1":
        raise TransientError("base de données du siège indisponible")

    with db("siege") as c:
        stores = {r["code"]: r["caisses"] for r in c.execute("SELECT code, caisses FROM stores")}
        prices = {r["sku"]: r["price"] for r in c.execute("SELECT sku, price FROM products")}
        _validate(body, stores, prices)

        cur = c.execute("INSERT INTO processed(id_vente, at) VALUES(?, ?) ON CONFLICT(id_vente) DO NOTHING", (body["id_vente"], now_iso()))
        if cur.rowcount == 0:  # déjà traité : doublon ignoré, le message sera quand même acquitté
            c.execute("INSERT INTO events(ts,kind,detail) VALUES(?,?,?)",
                      (now_iso(), "doublon", f"{body['id_vente']} ignoré (déjà traité)"))
            return

        c.execute(
            "INSERT INTO sales(id_vente,store,caisse,cashier,ts,received_at,total,nb_items) VALUES(?,?,?,?,?,?,?,?)",
            (body["id_vente"], body["magasin"], body["caisse"], body.get("caissier"), body["date"], now_iso(),
             body["total"], sum(l["qte"] for l in body["lignes"])),
        )
        for ln in body["lignes"]:
            c.execute("INSERT INTO sale_lines(id_vente,sku,qty,price) VALUES(?,?,?,?)",
                      (body["id_vente"], ln["produit"], ln["qte"], ln["prix"]))
            cur = c.execute(
                "UPDATE stock SET qty = qty - ? WHERE store=? AND sku=? AND qty >= ?",
                (ln["qte"], body["magasin"], ln["produit"], ln["qte"]),
            )
            if cur.rowcount == 0:
                raise PermanentError(f"stock insuffisant pour {ln['produit']}")


def start_consumer():
    get_broker().start_consumer(handle, on_event=log_event)
