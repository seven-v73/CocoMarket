"""CocoMarket : interface caisse + dashboard siège (Flask)."""
import datetime as dt
import csv
import io
import os
import random
import secrets
import threading
import time
from functools import wraps

from flask import Flask, abort, flash, g, jsonify, redirect, render_template, request, session, url_for
from werkzeug.security import check_password_hash, generate_password_hash

import caisse as K
import config
import consumer as HQ
import db as D
from broker import get_broker
from errors import BrokerUnavailable

app = Flask(__name__)
app.secret_key = config.SECRET_KEY
app.config.update(
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE="Lax",
    SESSION_COOKIE_SECURE=os.environ.get("COCO_HTTPS", "0") == "1",
    PERMANENT_SESSION_LIFETIME=dt.timedelta(hours=8),
)


@app.after_request
def security_headers(response):
    response.headers["X-Request-ID"] = getattr(g, "request_id", secrets.token_hex(12))
    started = getattr(g, "request_started", None)
    if started is not None:
        app.logger.info("request_id=%s method=%s path=%s status=%s duration_ms=%.1f",
                        g.request_id, request.method, request.path, response.status_code,
                        (time.perf_counter() - started) * 1000)
    response.headers.setdefault("X-Content-Type-Options", "nosniff")
    response.headers.setdefault("X-Frame-Options", "DENY")
    response.headers.setdefault("Referrer-Policy", "strict-origin-when-cross-origin")
    response.headers.setdefault("Permissions-Policy", "camera=(), microphone=(), geolocation=()")
    if app.config["SESSION_COOKIE_SECURE"]:
        response.headers.setdefault("Strict-Transport-Security", "max-age=31536000; includeSubDomains")
    if request.path.startswith("/api/") or request.path.startswith("/health/") or request.path == "/metrics":
        response.headers.setdefault("Cache-Control", "no-store")
    return response

ROLE_LABELS = {"caissier": "Caissier(ère)", "siege": "Siège", "admin": "Administrateur"}
_login_guard = threading.Lock()
_login_attempts = {}
LOGIN_LIMIT = 5
LOGIN_WINDOW = 60


# ---------------------------------------------------------------- auth / sécurité
def current_user():
    if "user" not in g:
        g.user = None
        uid = session.get("uid")
        if uid:
            with D.db("siege") as c:
                r = c.execute("SELECT * FROM users WHERE id=? AND active=1", (uid,)).fetchone()
            g.user = dict(r) if r else None
            if g.user is None:
                session.pop("uid", None)
    return g.user


def login_required(*roles):
    def deco(fn):
        @wraps(fn)
        def wrapper(*a, **kw):
            u = current_user()
            if u is None:
                if request.path.startswith("/api/"):
                    return jsonify(error="Session expirée, reconnectez-vous."), 401
                return redirect(url_for("login"))
            if roles and u["role"] not in roles:
                if request.path.startswith("/api/"):
                    return jsonify(error="Accès refusé."), 403
                abort(403)
            return fn(*a, **kw)
        return wrapper
    return deco


@app.before_request
def csrf_protect():
    g.request_id = request.headers.get("X-Request-ID") or secrets.token_hex(12)
    g.request_started = time.perf_counter()
    if "csrf" not in session:
        session["csrf"] = secrets.token_hex(16)
    if request.method == "POST":
        token = request.headers.get("X-CSRF") or request.form.get("csrf")
        if not token or not secrets.compare_digest(token, session["csrf"]):
            if request.path.startswith("/api/"):
                return jsonify(error="Jeton CSRF invalide."), 400
            abort(400)


@app.context_processor
def inject():
    return {"me": current_user(), "csrf": session.get("csrf", ""), "role_labels": ROLE_LABELS,
            "broker_name": get_broker().name}


@app.errorhandler(403)
def forbidden(_):
    return render_template("error.html", code=403, msg="Vous n'avez pas accès à cette page."), 403


@app.errorhandler(404)
def notfound(_):
    return render_template("error.html", code=404, msg="Page introuvable."), 404


def home_for(u):
    return url_for("caisse_page") if u["role"] == "caissier" else url_for("dashboard")


@app.route("/")
def index():
    u = current_user()
    return redirect(home_for(u) if u else url_for("login"))


@app.route("/health/live")
def health_live():
    return jsonify(status="ok", component="web")


@app.route("/health/ready")
def health_ready():
    checks = {"database": False, "broker": False}
    try:
        with D.db("siege") as c:
            c.execute("SELECT 1").fetchone()
        checks["database"] = True
    except Exception:
        pass
    try:
        get_broker().stats()
        checks["broker"] = True
    except Exception:
        pass
    return jsonify(status="ok" if all(checks.values()) else "degraded", checks=checks), 200 if all(checks.values()) else 503


@app.route("/metrics")
def metrics():
    """Métriques minimales au format Prometheus, sans dépendance externe."""
    try:
        with D.db("caisse") as c:
            pending = c.execute("SELECT COUNT(*) FROM outbox WHERE status='pending'").fetchone()[0]
            sent = c.execute("SELECT COUNT(*) FROM outbox WHERE status='sent'").fetchone()[0]
            attempts = c.execute("SELECT COALESCE(SUM(attempts),0) FROM outbox").fetchone()[0]
            pending_by_store = c.execute(
                "SELECT store, COUNT(*) AS n FROM outbox WHERE status='pending' GROUP BY store"
            ).fetchall()
        with D.db("siege") as c:
            sales = c.execute("SELECT COUNT(*) FROM sales").fetchone()[0]
            events_dlq = c.execute("SELECT COUNT(*) FROM events WHERE kind='dlq'").fetchone()[0]
        queue = get_broker().stats()
        body = "\n".join([
            "# TYPE cocomarket_outbox_pending gauge",
            f"cocomarket_outbox_pending {pending}",
            "# TYPE cocomarket_outbox_pending_by_store gauge",
            *[f'cocomarket_outbox_pending_by_store{{store="{r["store"]}"}} {r["n"]}' for r in pending_by_store],
            "# TYPE cocomarket_outbox_sent counter",
            f"cocomarket_outbox_sent {sent}",
            "# TYPE cocomarket_outbox_attempts counter",
            f"cocomarket_outbox_attempts {attempts}",
            "# TYPE cocomarket_sales_total counter",
            f"cocomarket_sales_total {sales}",
            "# TYPE cocomarket_dlq_total gauge",
            f"cocomarket_dlq_total {queue['dlq']}",
            "# TYPE cocomarket_broker_ready gauge",
            f"cocomarket_broker_ready {queue['ready']}",
            "# TYPE cocomarket_broker_unacked gauge",
            f"cocomarket_broker_unacked {queue['unacked']}",
            "# TYPE cocomarket_events_dlq_total counter",
            f"cocomarket_events_dlq_total {events_dlq}",
            "",
        ])
        return app.response_class(body, mimetype="text/plain; version=0.0.4")
    except Exception as e:
        return app.response_class(f"# metrics unavailable: {e}\n", status=503, mimetype="text/plain")


@app.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        ip = request.remote_addr or "unknown"
        now = time.time()
        with _login_guard:
            # Évite que le limiteur mémoire conserve indéfiniment les IP
            # qui ne se reconnectent plus.
            for address in list(_login_attempts):
                recent = [t for t in _login_attempts[address] if now - t < LOGIN_WINDOW]
                if recent:
                    _login_attempts[address] = recent
                else:
                    del _login_attempts[address]
            attempts = [t for t in _login_attempts.get(ip, []) if now - t < LOGIN_WINDOW]
            if len(attempts) >= LOGIN_LIMIT:
                app.logger.warning("login_throttled ip=%s", ip)
                return "Trop de tentatives. Réessayez dans une minute.", 429
        name = request.form.get("username", "").strip().lower()
        with D.db("siege") as c:
            r = c.execute("SELECT * FROM users WHERE username=?", (name,)).fetchone()
        if r and r["active"] and check_password_hash(r["pw_hash"], request.form.get("password", "")):
            with _login_guard:
                _login_attempts.pop(ip, None)
            csrf = session.get("csrf")
            session.clear()
            session["csrf"] = csrf
            session["uid"] = r["id"]
            return redirect(home_for(r))
        with _login_guard:
            _login_attempts[ip] = attempts + [now]
        flash("Identifiant ou mot de passe incorrect, ou compte désactivé.", "error")
    return render_template("login.html")


@app.route("/logout", methods=["POST"])
def logout():
    session.clear()
    return redirect(url_for("login"))


@app.route("/compte", methods=["GET", "POST"])
@login_required()
def account():
    u = current_user()
    if request.method == "POST":
        old, new, new2 = request.form.get("old", ""), request.form.get("new", ""), request.form.get("new2", "")
        if not check_password_hash(u["pw_hash"], old):
            flash("Mot de passe actuel incorrect.", "error")
        elif len(new) < 8:
            flash("Le nouveau mot de passe doit contenir au moins 8 caractères.", "error")
        elif new != new2:
            flash("Les deux nouveaux mots de passe sont différents.", "error")
        else:
            with D.db("siege") as c:
                c.execute("UPDATE users SET pw_hash=? WHERE id=?", (generate_password_hash(new), u["id"]))
            flash("Mot de passe modifié.", "ok")
            return redirect(url_for("account"))
    return render_template("account.html")


# ---------------------------------------------------------------- caisse
@app.route("/caisse")
@login_required("caissier")
def caisse_page():
    u = current_user()
    with D.db("siege") as c:
        store = c.execute("SELECT * FROM stores WHERE code=?", (u["store"],)).fetchone()
    return render_template("caisse.html", store=store)


@app.route("/api/caisse/products")
@login_required("caissier")
def api_products():
    with D.db("siege") as c:
        rows = c.execute("SELECT sku,name,category,price FROM products ORDER BY category,name").fetchall()
    return jsonify([dict(r) for r in rows])


def _caisse_state(u):
    tickets, pending = K.tickets(u["store"], u["caisse"])
    return {"link": K.link_up(u["store"]), "pending": pending, "tickets": tickets}


@app.route("/api/caisse/state")
@login_required("caissier")
def api_caisse_state():
    return jsonify(_caisse_state(current_user()))


@app.route("/api/caisse/sale", methods=["POST"])
@login_required("caissier")
def api_sale():
    u = current_user()
    data = request.get_json(silent=True) or {}
    try:
        id_vente, sent = K.make_sale(u, data.get("lines") or [])
        if K.link_up(u["store"]):
            K.flush_pending()
    except ValueError as e:
        return jsonify(error=str(e)), 400
    return jsonify(id_vente=id_vente, sent=sent, **_caisse_state(u))


@app.route("/api/caisse/link", methods=["POST"])
@login_required("caissier")
def api_link():
    u = current_user()
    up = bool((request.get_json(silent=True) or {}).get("up"))
    K.set_link(u["store"], up)
    D.log_event("liaison", f"{u['store']} : liaison {'rétablie' if up else 'coupée (simulation)'} par {u['username']}")
    if up:
        K.flush_pending()
    return jsonify(**_caisse_state(u))


@app.route("/api/caisse/test/duplicate", methods=["POST"])
@login_required("caissier")
def api_test_duplicate():
    u = current_user()
    id_vente = (request.get_json(silent=True) or {}).get("id_vente", "")
    try:
        K.resend_duplicate(id_vente, u["store"])
    except (ValueError, BrokerUnavailable) as e:
        return jsonify(error=str(e)), 400
    return jsonify(ok=True)


@app.route("/api/caisse/test/invalid", methods=["POST"])
@login_required("caissier")
def api_test_invalid():
    try:
        K.send_invalid(current_user())
    except BrokerUnavailable as e:
        return jsonify(error=str(e)), 400
    return jsonify(ok=True)


# ---------------------------------------------------------------- siège : dashboard
def _period_start(period):
    today = dt.date.today()
    days = {"today": 0, "7d": 6, "30d": 29}.get(period, 0)
    return (today - dt.timedelta(days=days)).isoformat() + "T00:00:00", ("hour" if days == 0 else "day")


@app.route("/siege")
@login_required("siege", "admin")
def dashboard():
    with D.db("siege") as c:
        stores = c.execute("SELECT code,name FROM stores ORDER BY code").fetchall()
    return render_template("dashboard.html", stores=stores)


@app.route("/api/siege/overview")
@login_required("siege", "admin")
def api_overview():
    store = request.args.get("store") or None
    period = request.args.get("period", "today")
    start, mode = _period_start(period)
    sw = " AND store=?" if store else ""
    sp = (start, store) if store else (start,)

    with D.db("siege") as c:
        names = {r["code"]: r["name"] for r in c.execute("SELECT code,name FROM stores")}
        k = c.execute(
            f"""SELECT COUNT(*) nb, COALESCE(SUM(total),0) ca, COALESCE(SUM(nb_items),0) items,
                AVG(CASE WHEN COALESCE(cashier,'')!='simulation' THEN (julianday(received_at)-julianday(ts))*86400 END) d_avg,
                MAX(CASE WHEN COALESCE(cashier,'')!='simulation' THEN (julianday(received_at)-julianday(ts))*86400 END) d_max
                FROM sales WHERE ts>=?{sw}""", sp).fetchone()
        dup = c.execute("SELECT COUNT(*) FROM events WHERE kind='doublon' AND ts>=?", (start,)).fetchone()[0]

        # série temporelle (par heure aujourd'hui, par jour sinon) selon l'heure de la VENTE (pas de réception)
        cut = 13 if mode == "hour" else 10
        rows = c.execute(f"SELECT substr(ts,1,{cut}) b, COUNT(*) nb, SUM(total) ca FROM sales WHERE ts>=?{sw} GROUP BY b", sp).fetchall()
        got = {r["b"]: r for r in rows}
        today = dt.date.today()
        if mode == "hour":
            keys = [f"{today.isoformat()}T{h:02d}" for h in range(24)]
            labels = [f"{h:02d}h" for h in range(24)]
        else:
            n = 7 if period == "7d" else 30
            days = [(today - dt.timedelta(days=i)) for i in range(n - 1, -1, -1)]
            keys = [d.isoformat() for d in days]
            labels = [d.strftime("%d/%m") for d in days]
        series = {"labels": labels, "ca": [got[x]["ca"] if x in got else 0 for x in keys],
                  "nb": [got[x]["nb"] if x in got else 0 for x in keys]}

        by_store = [dict(r, name=names.get(r["store"], r["store"])) for r in c.execute(
            f"SELECT store, COUNT(*) nb, SUM(total) ca FROM sales WHERE ts>=?{sw} GROUP BY store ORDER BY ca DESC", sp)]
        top = [dict(r) for r in c.execute(
            f"""SELECT l.sku, p.name, SUM(l.qty) qty, SUM(l.qty*l.price) ca
                FROM sale_lines l JOIN sales s ON s.id_vente=l.id_vente JOIN products p ON p.sku=l.sku
                WHERE s.ts>=?{' AND s.store=?' if store else ''} GROUP BY l.sku ORDER BY ca DESC LIMIT 8""", sp)]
        alerts = [dict(r) for r in c.execute(
            f"""SELECT s.store, s.sku, p.name, s.qty, p.threshold FROM stock s JOIN products p ON p.sku=s.sku
                WHERE s.qty<=p.threshold{' AND s.store=?' if store else ''} ORDER BY s.qty LIMIT 12""",
            (store,) if store else ())]
        received = {r["store"]: (r["nb"], r["tot"]) for r in c.execute(
            f"SELECT store, COUNT(*) nb, COALESCE(SUM(total),0) tot FROM sales WHERE ts>=?{sw} GROUP BY store", sp)}
        last_sale = {r["store"]: r["m"] for r in c.execute("SELECT store, MAX(ts) m FROM sales GROUP BY store")}
        events = [dict(r) for r in c.execute("SELECT ts, kind, detail FROM events ORDER BY id DESC LIMIT 14")]
        store_list = [r["code"] for r in c.execute("SELECT code FROM stores ORDER BY code")]

    with D.db("caisse") as c:
        emitted = {r["store"]: dict(r) for r in c.execute(
            f"""SELECT store, COUNT(*) nb, COALESCE(SUM(total),0) tot,
                SUM(status='pending') pending, COALESCE(SUM(CASE WHEN status='pending' THEN total END),0) pending_tot
                FROM outbox WHERE ts>=?{sw} GROUP BY store""", sp)}
        links = {r["store"]: bool(r["up"]) for r in c.execute("SELECT store, up FROM link")}
        pend_all = {r["store"]: r["n"] for r in c.execute("SELECT store, COUNT(*) n FROM outbox WHERE status='pending' GROUP BY store")}

    recon = []
    for code in store_list:
        if store and code != store:
            continue
        e = emitted.get(code, {"nb": 0, "tot": 0, "pending": 0, "pending_tot": 0})
        rn, rt = received.get(code, (0, 0))
        ecart = e["tot"] - rt
        pend = e["pending"] or 0
        status = "ok" if ecart == 0 else ("pending" if pend else "transit")
        recon.append({"store": code, "name": names[code], "emitted_nb": e["nb"], "emitted_total": e["tot"],
                      "received_nb": rn, "received_total": rt, "pending": pend, "ecart": ecart,
                      "link": links.get(code, True), "status": status, "last_sale": last_sale.get(code)})

    try:
        q = get_broker().stats()
        q.update(ok=True)
    except BrokerUnavailable as e:
        q = {"ok": False, "ready": 0, "unacked": 0, "dlq": 0, "error": str(e)[:120]}

    return jsonify(
        kpis={"ca": k["ca"], "nb": k["nb"], "panier": round(k["ca"] / k["nb"]) if k["nb"] else 0, "items": k["items"],
              "delay_avg": k["d_avg"], "delay_max": k["d_max"], "duplicates": dup, "dlq": q["dlq"],
              "pending_total": sum(pend_all.values())},
        series=series, mode=mode, by_store=by_store, top=top, alerts=alerts, queue=q, recon=recon, events=events,
        hq_down=D.get_setting("hq_down") == "1", broker=get_broker().name,
    )


@app.route("/api/siege/hq_down", methods=["POST"])
@login_required("admin")
def api_hq_down():
    down = bool((request.get_json(silent=True) or {}).get("down"))
    D.set_setting("hq_down", "1" if down else "0")
    D.log_event("panne", f"Base du siège {'indisponible (simulation)' if down else 'rétablie'} — par {current_user()['username']}")
    return jsonify(hq_down=down)


@app.route("/api/siege/simulate", methods=["POST"])
@login_required("admin")
def api_simulate():
    """Génère des ventes de démonstration à travers le vrai circuit (outbox -> broker -> siège)."""
    n = max(1, min(int((request.get_json(silent=True) or {}).get("n", 30)), 200))
    with D.db("siege") as c:
        stores = [dict(r) for r in c.execute("SELECT code, caisses FROM stores")]
        skus = [r["sku"] for r in c.execute("SELECT sku FROM products")]
    now = dt.datetime.now()
    midnight = now.replace(hour=0, minute=0, second=0, microsecond=0)
    sent = pend = 0
    for _ in range(n):
        s = random.choice(stores)
        user = {"username": "simulation", "store": s["code"], "caisse": random.randint(1, s["caisses"])}
        ts = max(midnight, now - dt.timedelta(minutes=random.randint(0, 600))).replace(microsecond=0).isoformat()
        lines = [{"sku": sk, "qty": random.randint(1, 4)} for sk in random.sample(skus, random.randint(1, 5))]
        _, ok = K.make_sale(user, lines, ts=ts)
        sent += ok
        pend += (not ok)
    D.log_event("simulation", f"{n} ventes de démonstration générées ({pend} en attente de liaison)")
    return jsonify(n=n, sent=sent, pending=pend)


# ---------------------------------------------------------------- siège : DLQ
@app.route("/siege/dlq")
@login_required("siege", "admin")
def dlq_page():
    try:
        items, err = get_broker().list_dlq(), None
    except BrokerUnavailable as e:
        items, err = [], str(e)
    return render_template("dlq.html", items=items, err=err)


@app.route("/siege/dlq/<action>", methods=["POST"])
@login_required("admin")
def dlq_action(action):
    key = request.form.get("key", "")
    if action not in ("requeue", "discard") or not key:
        abort(400)
    try:
        getattr(get_broker(), action)(key)
        D.log_event("dlq", f"Message {key} : {'rejoué' if action == 'requeue' else 'supprimé'} par {current_user()['username']}")
        flash("Message rejoué : il repart dans la file principale." if action == "requeue" else "Message supprimé.", "ok")
    except BrokerUnavailable as e:
        flash(f"Broker injoignable : {e}", "error")
    return redirect(url_for("dlq_page"))


# ---------------------------------------------------------------- admin : utilisateurs
@app.route("/siege/utilisateurs")
@login_required("admin")
def users_page():
    with D.db("siege") as c:
        users = c.execute("SELECT * FROM users ORDER BY role, username").fetchall()
        stores = c.execute("SELECT code,name,caisses FROM stores ORDER BY code").fetchall()
    return render_template("users.html", users=users, stores=stores, password_min=config.MIN_PASSWORD_LENGTH)


# ---------------------------------------------------------------- admin : catalogue et stock
@app.route("/siege/catalogue")
@login_required("admin")
def catalogue_page():
    with D.db("siege") as c:
        products = c.execute("""SELECT p.sku,p.name,p.category,p.price,p.threshold,
          COALESCE(SUM(s.qty),0) total_stock FROM products p
          LEFT JOIN stock s ON s.sku=p.sku GROUP BY p.sku ORDER BY p.category,p.name""").fetchall()
        stores = c.execute("SELECT code,name FROM stores ORDER BY code").fetchall()
        stock = c.execute("""SELECT s.store,s.sku,s.qty,st.name store_name,p.name product_name
          FROM stock s JOIN stores st ON st.code=s.store JOIN products p ON p.sku=s.sku
          ORDER BY s.store,p.name""").fetchall()
    return render_template("catalogue.html", products=products, stores=stores, stock=stock)


@app.route("/siege/catalogue/produit", methods=["POST"])
@login_required("admin")
def product_create():
    f = request.form
    sku, name, category = f.get("sku", "").strip().upper(), f.get("name", "").strip(), f.get("category", "").strip()
    try: price, threshold = int(f.get("price", "")), int(f.get("threshold", "40"))
    except ValueError: price = threshold = -1
    error = None
    if not sku or not sku.replace("-", "").replace("_", "").isalnum(): error = "Le code produit est invalide."
    elif not name or not category: error = "Le nom et la catégorie sont obligatoires."
    elif price <= 0: error = "Le prix doit être supérieur à zéro."
    elif threshold < 0: error = "Le seuil ne peut pas être négatif."
    if error:
        flash(error, "error")
        return redirect(url_for("catalogue_page"))
    try:
        with D.db("siege") as c:
            c.execute("INSERT INTO products(sku,name,category,price,threshold) VALUES(?,?,?,?,?)", (sku, name, category, price, threshold))
            stores = c.execute("SELECT code FROM stores").fetchall()
            c.executemany("INSERT INTO stock(store,sku,qty) VALUES(?,?,0)", [(s["code"], sku) for s in stores])
        D.log_event("catalogue", f"Produit {sku} créé par {current_user()['username']}")
        flash(f"Produit « {name} » ajouté au catalogue avec un stock initial nul.", "ok")
    except Exception:
        flash("Ce code produit existe déjà ou les données sont invalides.", "error")
    return redirect(url_for("catalogue_page"))


@app.route("/siege/catalogue/produit/<sku>", methods=["POST"])
@login_required("admin")
def product_update(sku):
    f = request.form
    name, category = f.get("name", "").strip(), f.get("category", "").strip()
    try: price, threshold = int(f.get("price", "")), int(f.get("threshold", "40"))
    except ValueError: price = threshold = -1
    if not name or not category or price <= 0 or threshold < 0:
        flash("Nom, catégorie, prix ou seuil invalide.", "error")
    else:
        with D.db("siege") as c:
            cur = c.execute("UPDATE products SET name=?,category=?,price=?,threshold=? WHERE sku=?", (name, category, price, threshold, sku))
        if cur.rowcount:
            D.log_event("catalogue", f"Produit {sku} modifié par {current_user()['username']}")
            flash("Produit mis à jour.", "ok")
        else: flash("Produit introuvable.", "error")
    return redirect(url_for("catalogue_page"))


@app.route("/siege/catalogue/stock", methods=["POST"])
@login_required("admin")
def stock_adjust():
    store, sku = request.form.get("store", ""), request.form.get("sku", "")
    try: quantity = int(request.form.get("quantity", ""))
    except ValueError: quantity = -1
    if quantity < 0:
        flash("La quantité doit être positive ou nulle.", "error")
        return redirect(url_for("catalogue_page"))
    with D.db("siege") as c:
        cur = c.execute("UPDATE stock SET qty=? WHERE store=? AND sku=?", (quantity, store, sku))
    if cur.rowcount:
        D.log_event("stock", f"Stock {sku} / {store} fixé à {quantity} par {current_user()['username']}")
        flash("Stock mis à jour.", "ok")
    else: flash("Magasin ou produit introuvable.", "error")
    return redirect(url_for("catalogue_page"))


@app.route("/siege/journal")
@login_required("admin")
def audit_page():
    with D.db("siege") as c:
        events = c.execute("SELECT ts,kind,detail FROM events ORDER BY id DESC LIMIT 250").fetchall()
    return render_template("audit.html", events=events)


@app.route("/siege/journal/export")
@login_required("admin")
def audit_export():
    with D.db("siege") as c:
        events = c.execute("SELECT ts,kind,detail FROM events ORDER BY id DESC LIMIT 5000").fetchall()
    stream = io.StringIO()
    writer = csv.writer(stream)
    writer.writerow(["date", "type", "detail"])
    writer.writerows((e["ts"], e["kind"], e["detail"]) for e in events)
    response = app.response_class(stream.getvalue(), mimetype="text/csv; charset=utf-8")
    response.headers["Content-Disposition"] = "attachment; filename=cocomarket-audit.csv"
    return response


@app.route("/siege/utilisateurs/creer", methods=["POST"])
@login_required("admin")
def user_create():
    f = request.form
    username = f.get("username", "").strip().lower()
    role, pw = f.get("role", ""), f.get("password", "")
    store, caisse = f.get("store") or None, f.get("caisse") or None
    err = None
    if not username.replace(".", "").replace("_", "").isalnum():
        err = "Identifiant invalide (lettres, chiffres, point ou tiret bas)."
    elif not f.get("full_name", "").strip():
        err = "Le nom complet est obligatoire."
    elif role not in ROLE_LABELS:
        err = "Rôle invalide."
    elif len(pw) < config.MIN_PASSWORD_LENGTH:
        err = f"Le mot de passe doit contenir au moins {config.MIN_PASSWORD_LENGTH} caractères."
    elif role == "caissier":
        with D.db("siege") as c:
            s = c.execute("SELECT caisses FROM stores WHERE code=?", (store,)).fetchone()
        if not s:
            err = "Un caissier doit être rattaché à un magasin."
        elif not (caisse and caisse.isdigit() and 1 <= int(caisse) <= s["caisses"]):
            err = f"Numéro de caisse invalide (ce magasin a {s['caisses']} caisses)."
    if err:
        flash(err, "error")
        return redirect(url_for("users_page"))
    if role != "caissier":
        store = caisse = None
    try:
        with D.db("siege") as c:
            c.execute("INSERT INTO users(username,full_name,role,store,caisse,pw_hash,created) VALUES(?,?,?,?,?,?,?)",
                      (username, f["full_name"].strip(), role, store, int(caisse) if caisse else None,
                       generate_password_hash(pw), D.now_iso()))
        D.log_event("admin", f"Compte « {username} » ({role}) créé par {current_user()['username']}")
        flash(f"Compte « {username} » créé.", "ok")
    except Exception:
        flash("Cet identifiant existe déjà.", "error")
    return redirect(url_for("users_page"))


@app.route("/siege/utilisateurs/<int:uid>/<action>", methods=["POST"])
@login_required("admin")
def user_action(uid, action):
    me = current_user()
    audit_detail = None
    with D.db("siege") as c:
        u = c.execute("SELECT * FROM users WHERE id=?", (uid,)).fetchone()
        if not u:
            abort(404)
        if action == "toggle":
            if u["id"] == me["id"]:
                flash("Vous ne pouvez pas désactiver votre propre compte.", "error")
            else:
                active = 1 - u["active"]
                c.execute("UPDATE users SET active=? WHERE id=?", (active, uid))
                action_label = "désactivé" if not active else "réactivé"
                audit_detail = f"Compte « {u['username']} » {action_label} par {me['username']}"
                flash(f"Compte « {u['username']} » {action_label}.", "ok")
        elif action == "reset":
            temp = secrets.token_urlsafe(14)
            c.execute("UPDATE users SET pw_hash=? WHERE id=?", (generate_password_hash(temp), uid))
            audit_detail = f"Mot de passe du compte « {u['username']} » réinitialisé par {me['username']}"
            flash(f"Nouveau mot de passe temporaire pour « {u['username']} » : {temp} (à communiquer à la personne, qui peut le changer dans « Mon compte »).", "ok")
        else:
            abort(400)
    if audit_detail:
        D.log_event("admin", audit_detail)
    return redirect(url_for("users_page"))


# ---------------------------------------------------------------- démarrage
def start_workers():
    HQ.start_consumer()
    K.start_flusher()


D.init()
if config.START_WORKERS and config.COMPONENT == "all":
    start_workers()

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=config.PORT, debug=False, use_reloader=False, threaded=True)
