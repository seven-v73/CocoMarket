"""Accès SQLite. Trois bases séparées pour refléter l'architecture réelle :

- siege.db  : base centrale du siège (utilisateurs, stocks, ventes reçues)
- caisse.db : tampon local des caisses (outbox) + état de la liaison Internet
- broker.db : files du broker local (utilisé seulement si COCO_BROKER=local)
"""
import os
import sqlite3
import datetime as dt
from contextlib import contextmanager

import config
from werkzeug.security import generate_password_hash

DATA_DIR = os.environ.get("COCO_DATA", os.path.join(os.path.dirname(os.path.abspath(__file__)), "data"))


class _PgConnection:
    def __init__(self, conn):
        self._conn = conn

    @staticmethod
    def _sql(sql):
        return sql.replace("?", "%s")

    def execute(self, sql, params=()):
        return self._conn.execute(self._sql(sql), params)

    def executemany(self, sql, params):
        return self._conn.executemany(self._sql(sql), params)

    def commit(self):
        self._conn.commit()

    def rollback(self):
        self._conn.rollback()

    def close(self):
        self._conn.close()


def now_iso():
    return dt.datetime.now().replace(microsecond=0).isoformat()


@contextmanager
def db(name):
    if name == "siege" and config.DATABASE_URL:
        import psycopg
        from psycopg.rows import dict_row
        c = psycopg.connect(config.DATABASE_URL, connect_timeout=5, row_factory=dict_row)
        try:
            yield _PgConnection(c)
            c.commit()
        except Exception:
            c.rollback()
            raise
        finally:
            c.close()
        return
    os.makedirs(DATA_DIR, exist_ok=True)
    c = sqlite3.connect(os.path.join(DATA_DIR, name + ".db"), timeout=30)
    c.row_factory = sqlite3.Row
    c.execute("PRAGMA busy_timeout=30000")
    c.execute("PRAGMA journal_mode=WAL")
    c.execute("PRAGMA synchronous=FULL")
    try:
        yield c
        c.commit()
    except Exception:
        c.rollback()
        raise
    finally:
        c.close()


SIEGE_SCHEMA = """
CREATE TABLE IF NOT EXISTS users(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  username TEXT UNIQUE NOT NULL,
  full_name TEXT NOT NULL,
  role TEXT NOT NULL CHECK(role IN ('caissier','siege','admin')),
  store TEXT, caisse INTEGER,
  pw_hash TEXT NOT NULL,
  active INTEGER NOT NULL DEFAULT 1,
  created TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS stores(code TEXT PRIMARY KEY, name TEXT NOT NULL, city TEXT NOT NULL, caisses INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS products(
  sku TEXT PRIMARY KEY, name TEXT NOT NULL, category TEXT NOT NULL, price INTEGER NOT NULL CHECK(price > 0), threshold INTEGER NOT NULL DEFAULT 40 CHECK(threshold >= 0)
);
CREATE TABLE IF NOT EXISTS stock(store TEXT NOT NULL, sku TEXT NOT NULL, qty INTEGER NOT NULL CHECK(qty >= 0), PRIMARY KEY(store, sku));
CREATE TABLE IF NOT EXISTS sales(
  id_vente TEXT PRIMARY KEY, store TEXT NOT NULL, caisse INTEGER NOT NULL, cashier TEXT,
  ts TEXT NOT NULL, received_at TEXT NOT NULL, total INTEGER NOT NULL, nb_items INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_sales_ts ON sales(ts);
CREATE TABLE IF NOT EXISTS sale_lines(
  id INTEGER PRIMARY KEY AUTOINCREMENT, id_vente TEXT NOT NULL, sku TEXT NOT NULL, qty INTEGER NOT NULL CHECK(qty > 0), price INTEGER NOT NULL CHECK(price >= 0)
);
CREATE INDEX IF NOT EXISTS ix_lines_sale ON sale_lines(id_vente);
CREATE TABLE IF NOT EXISTS processed(id_vente TEXT PRIMARY KEY, at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS events(id INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT NOT NULL, kind TEXT NOT NULL, detail TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS ix_events_kind_ts ON events(kind, ts);
CREATE TABLE IF NOT EXISTS settings(key TEXT PRIMARY KEY, value TEXT NOT NULL);
"""

CAISSE_SCHEMA = """
CREATE TABLE IF NOT EXISTS outbox(
  id_vente TEXT PRIMARY KEY, store TEXT NOT NULL, caisse INTEGER NOT NULL, cashier TEXT,
  ts TEXT NOT NULL, total INTEGER NOT NULL, payload TEXT NOT NULL,
  status TEXT NOT NULL DEFAULT 'pending', attempts INTEGER NOT NULL DEFAULT 0,
  last_error TEXT, sent_at TEXT, created REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_outbox_status ON outbox(status, created);
CREATE TABLE IF NOT EXISTS link(store TEXT PRIMARY KEY, up INTEGER NOT NULL DEFAULT 1);
"""

STORES = [
    ("ABJ01", "CocoMarket Cocody", "Abidjan", 8),
    ("ABJ02", "CocoMarket Plateau", "Abidjan", 6),
    ("ABJ03", "CocoMarket Yopougon", "Abidjan", 8),
    ("YAM01", "CocoMarket Yamoussoukro", "Yamoussoukro", 5),
    ("BKE01", "CocoMarket Bouaké", "Bouaké", 5),
    ("SPD01", "CocoMarket San-Pédro", "San-Pédro", 4),
    ("KOR01", "CocoMarket Korhogo", "Korhogo", 3),
]

PRODUCTS = [
    ("RIZ-5KG", "Riz parfumé 5 kg", "Épicerie", 4500),
    ("HUILE-1L", "Huile végétale 1 L", "Épicerie", 1500),
    ("SUCRE-1KG", "Sucre 1 kg", "Épicerie", 800),
    ("SARD-125", "Sardines 125 g", "Épicerie", 500),
    ("TOM-400", "Tomate concentrée 400 g", "Épicerie", 650),
    ("LAIT-1L", "Lait UHT 1 L", "Produits frais", 900),
    ("OEUF-30", "Plateau d'œufs (30)", "Produits frais", 2800),
    ("ATT-1KG", "Attiéké 1 kg", "Produits frais", 1000),
    ("POUL-1KG", "Poulet entier 1 kg", "Viandes", 3200),
    ("EAU-15L", "Eau minérale 1,5 L", "Boissons", 400),
    ("JUS-1L", "Jus d'orange 1 L", "Boissons", 1100),
    ("BRAS-65", "Bière locale 65 cl", "Boissons", 700),
    ("PAIN-BAG", "Baguette", "Boulangerie", 250),
    ("BAN-1KG", "Banane plantain 1 kg", "Fruits & légumes", 900),
    ("SAVON-2", "Savon de ménage", "Hygiène", 300),
    ("PAPIER-4", "Papier toilette x4", "Hygiène", 1200),
]

# (username, nom complet, rôle, magasin, caisse, mot de passe)
DEFAULT_USERS = [
    ("admin", "Administrateur Siège", "admin", None, None, config.BOOTSTRAP_ADMIN_PASSWORD),
    ("siege", "Direction Commerciale", "siege", None, None, "siege123"),
    ("awa", "Awa Traoré", "caissier", "ABJ01", 1, "caisse123"),
    ("kouame", "Kouamé Yao", "caissier", "BKE01", 2, "caisse123"),
    ("fatou", "Fatou Koné", "caissier", "KOR01", 1, "caisse123"),
]


def seed_users():
    """Comptes de démarrage : démo complète en dev, admin seul en production."""
    return DEFAULT_USERS if config.SEED_DEMO_USERS else [DEFAULT_USERS[0]]


def init():
    if config.DATABASE_URL:
        import psycopg
        with psycopg.connect(config.DATABASE_URL, connect_timeout=5) as c:
            # Un seul worker initialise le schéma et les données de démonstration.
            c.execute("SELECT pg_advisory_xact_lock(481927)")
            with open(os.path.join(os.path.dirname(__file__), "postgres_schema.sql"), encoding="utf-8") as f:
                c.execute(f.read())
            if c.execute("SELECT COUNT(*) FROM users").fetchone()[0] == 0:
                c.executemany("INSERT INTO stores VALUES(%s,%s,%s,%s)", STORES)
                c.executemany("INSERT INTO products(sku,name,category,price) VALUES(%s,%s,%s,%s)", PRODUCTS)
                for s in STORES:
                    for p in PRODUCTS:
                        c.execute("INSERT INTO stock VALUES(%s,%s,%s)", (s[0], p[0], 300))
                for u in seed_users():
                    c.execute("INSERT INTO users(username,full_name,role,store,caisse,pw_hash,created) VALUES(%s,%s,%s,%s,%s,%s,%s)", (u[0], u[1], u[2], u[3], u[4], generate_password_hash(u[5]), now_iso()))
                c.execute("INSERT INTO settings VALUES('hq_down','0') ON CONFLICT(key) DO NOTHING")
        with db("caisse") as c:
            c.executescript(CAISSE_SCHEMA)
        return
    with db("siege") as c:
        c.executescript(SIEGE_SCHEMA)
        if c.execute("SELECT COUNT(*) FROM users").fetchone()[0] == 0:
            c.executemany("INSERT INTO stores VALUES(?,?,?,?)", STORES)
            c.executemany("INSERT INTO products(sku,name,category,price) VALUES(?,?,?,?)", PRODUCTS)
            for s in STORES:
                for p in PRODUCTS:
                    c.execute("INSERT INTO stock VALUES(?,?,?)", (s[0], p[0], 300))
            for u in seed_users():
                c.execute(
                    "INSERT INTO users(username,full_name,role,store,caisse,pw_hash,created) VALUES(?,?,?,?,?,?,?)",
                    (u[0], u[1], u[2], u[3], u[4], generate_password_hash(u[5]), now_iso()),
                )
            c.execute("INSERT INTO settings VALUES('hq_down','0')")
    with db("caisse") as c:
        c.executescript(CAISSE_SCHEMA)


def get_setting(key, default="0"):
    with db("siege") as c:
        r = c.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
        return r["value"] if r else default


def set_setting(key, value):
    with db("siege") as c:
        c.execute("INSERT INTO settings VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, str(value)))


def log_event(kind, detail):
    with db("siege") as c:
        c.execute("INSERT INTO events(ts,kind,detail) VALUES(?,?,?)", (now_iso(), kind, detail))
