"""Broker local persistant (SQLite) : même comportement que RabbitMQ pour la démo.

- messages durables (survivent à un redémarrage, les "unacked" sont remis en file au démarrage)
- ACK explicite après traitement
- reprises avec délai exponentiel, puis Dead Letter Queue
"""
import json
import logging
import threading
import time

import config
from db import db
from errors import PermanentError, TransientError

logger = logging.getLogger(__name__)


class LocalBroker:
    name = "Broker local (SQLite persistant)"

    def __init__(self):
        with db("broker") as c:
            c.executescript(
                """CREATE TABLE IF NOT EXISTS messages(
                  id INTEGER PRIMARY KEY AUTOINCREMENT,
                  queue TEXT NOT NULL,
                  body TEXT NOT NULL,
                  retries INTEGER NOT NULL DEFAULT 0,
                  state TEXT NOT NULL DEFAULT 'ready',
                  available_at REAL NOT NULL DEFAULT 0,
                  reason TEXT,
                  created REAL NOT NULL);
                CREATE INDEX IF NOT EXISTS ix_msg ON messages(queue, state, available_at);"""
            )
            # reprise après crash : ce qui n'a pas été acquitté redevient disponible
            c.execute("UPDATE messages SET state='ready' WHERE state='unacked'")
        self._stop = threading.Event()
        self._thread = None

    # ---------- producteur ----------
    def publish(self, body):
        with db("broker") as c:
            c.execute(
                "INSERT INTO messages(queue, body, created) VALUES('ventes', ?, ?)",
                (json.dumps(body, ensure_ascii=False), time.time()),
            )

    # ---------- consommateur ----------
    def start_consumer(self, handler, on_event=None):
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, args=(handler, on_event), daemon=True, name="hq-consumer")
        self._thread.start()

    def stop(self):
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=3)

    def _next(self):
        with db("broker") as c:
            r = c.execute(
                "SELECT * FROM messages WHERE queue='ventes' AND state='ready' AND available_at<=? ORDER BY id LIMIT 1",
                (time.time(),),
            ).fetchone()
            if r:
                c.execute("UPDATE messages SET state='unacked' WHERE id=?", (r["id"],))
            return r

    def _loop(self, handler, on_event):
        emit = on_event or (lambda kind, detail: None)
        while not self._stop.is_set():
            try:
                msg = self._next()
                if msg is None:
                    time.sleep(0.2)
                    continue
                self._process(msg, handler, emit)
            except Exception:
                logger.exception("Le consommateur local a planté ; reprise automatique")
                time.sleep(1)

    def _process(self, msg, handler, emit):
        mid, retries = msg["id"], msg["retries"]
        try:
            body = json.loads(msg["body"])
            handler(body)
        except PermanentError as e:
            self._to_dlq(mid, f"Message invalide : {e}")
            emit("dlq", f"{self._label(msg)} → DLQ ({e})")
        except ValueError as e:  # JSON illisible
            self._to_dlq(mid, f"JSON illisible : {e}")
            emit("dlq", f"Message illisible → DLQ ({e})")
        except Exception as e:  # TransientError ou imprévu : on retente
            retries += 1
            if retries > config.MAX_RETRIES:
                self._to_dlq(mid, f"Échec après {config.MAX_RETRIES} reprises : {e}")
                emit("dlq", f"{self._label(msg)} → DLQ après {config.MAX_RETRIES} reprises ({e})")
            else:
                delay = config.RETRY_DELAY * (2 ** (retries - 1))
                with db("broker") as c:
                    c.execute(
                        "UPDATE messages SET state='ready', retries=?, available_at=? WHERE id=?",
                        (retries, time.time() + delay, mid),
                    )
                emit("retry", f"{self._label(msg)} : reprise {retries}/{config.MAX_RETRIES} dans {delay:.0f}s ({e})")
        else:
            with db("broker") as c:  # ACK
                c.execute("DELETE FROM messages WHERE id=?", (mid,))

    @staticmethod
    def _label(msg):
        try:
            return json.loads(msg["body"]).get("id_vente", f"msg#{msg['id']}")
        except Exception:
            return f"msg#{msg['id']}"

    def _to_dlq(self, mid, reason):
        with db("broker") as c:
            c.execute("UPDATE messages SET queue='dlq', state='ready', reason=? WHERE id=?", (reason, mid))

    # ---------- supervision ----------
    def stats(self):
        with db("broker") as c:
            def n(q, s=None):
                sql = "SELECT COUNT(*) FROM messages WHERE queue=?" + (" AND state=?" if s else "")
                return c.execute(sql, (q, s) if s else (q,)).fetchone()[0]
            return {"ready": n("ventes", "ready"), "unacked": n("ventes", "unacked"), "dlq": n("dlq")}

    def list_dlq(self):
        with db("broker") as c:
            rows = c.execute("SELECT * FROM messages WHERE queue='dlq' ORDER BY id").fetchall()
        out = []
        for r in rows:
            try:
                body = json.loads(r["body"])
            except ValueError:
                body = {"raw": r["body"]}
            out.append({"key": str(r["id"]), "body": body, "reason": r["reason"], "retries": r["retries"], "created": r["created"]})
        return out

    def requeue(self, key):
        with db("broker") as c:
            c.execute(
                "UPDATE messages SET queue='ventes', state='ready', retries=0, available_at=0, reason=NULL WHERE id=? AND queue='dlq'",
                (int(key),),
            )

    def discard(self, key):
        with db("broker") as c:
            c.execute("DELETE FROM messages WHERE id=? AND queue='dlq'", (int(key),))
