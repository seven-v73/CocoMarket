"""Broker RabbitMQ (pika).

Topologie (toutes les files sont durables, messages persistants delivery_mode=2) :

  ventes        file principale ; DLX -> ventes.dlq (filet de sécurité en cas de basic_nack requeue=False)
  ventes.retry.* files de reprise : TTL progressif, puis retour automatique vers "ventes"
  ventes.dlq    Dead Letter Queue : messages invalides ou épuisés après MAX_RETRIES reprises

Garanties :
- côté producteur : publisher confirms (basic_publish lève une exception si le broker ne confirme pas)
- côté consommateur : ACK manuel, uniquement après traitement réussi
- un crash entre "republier en retry" et "ACK" peut créer un doublon : neutralisé par l'idempotence du siège
"""
import json
import logging
import threading
import time

import pika

import config
from errors import BrokerUnavailable, PermanentError

logger = logging.getLogger(__name__)


class RabbitBroker:
    name = "RabbitMQ"
    # Les arguments d'une queue RabbitMQ sont immuables. Le suffixe de version
    # évite qu'un ancien TTL bloque un déploiement après changement de config.
    PREFIX = config.RABBIT_QUEUE_PREFIX
    Q = f"{PREFIX}.ventes"
    RETRY = f"{PREFIX}.ventes.retry"
    RETRIES = tuple(f"{PREFIX}.ventes.retry.{n}" for n in range(1, config.MAX_RETRIES + 1))
    DLQ = f"{PREFIX}.ventes.dlq"

    def __init__(self):
        self.params = pika.URLParameters(config.RABBIT_URL)
        self.params.socket_timeout = 3
        self.params.blocked_connection_timeout = 3
        self.params.heartbeat = 30
        self._stop = threading.Event()
        self._thread = None
        self._conn = None
        self._ch = None
        self._lock = threading.Lock()

    # ---------- topologie ----------
    def _declare(self, ch):
        quorum = {"x-queue-type": "quorum"}
        ch.queue_declare(self.DLQ, durable=True, arguments=quorum)
        for attempt, queue in enumerate(self.RETRIES, 1):
            ch.queue_declare(
                queue,
                durable=True,
                arguments={
                    **quorum,
                    "x-message-ttl": int(config.RETRY_DELAY * (2 ** (attempt - 1)) * 1000),
                    "x-dead-letter-exchange": "",
                    "x-dead-letter-routing-key": self.Q,
                },
            )
        ch.queue_declare(
            self.Q,
            durable=True,
            arguments={**quorum, "x-dead-letter-exchange": "", "x-dead-letter-routing-key": self.DLQ},
        )

    def _connect(self):
        with self._lock:
            if self._conn and self._conn.is_open and self._ch and self._ch.is_open:
                return self._conn, self._ch
            if self._conn and self._conn.is_open:
                try:
                    self._conn.close()
                except Exception:
                    pass
            try:
                self._conn = pika.BlockingConnection(self.params)
                self._ch = self._conn.channel()
                self._declare(self._ch)
                return self._conn, self._ch
            except (pika.exceptions.AMQPError, OSError) as e:
                self._conn = None
                self._ch = None
                raise BrokerUnavailable(str(e) or f"RabbitMQ injoignable ({type(e).__name__})")

    # ---------- producteur ----------
    def publish(self, body):
        conn = None
        try:
            conn = pika.BlockingConnection(self.params)
            ch = conn.channel()
            self._declare(ch)
            ch.confirm_delivery()
            ch.basic_publish(
                "",
                self.Q,
                json.dumps(body, ensure_ascii=False).encode(),
                pika.BasicProperties(
                    delivery_mode=2, content_type="application/json", message_id=str(body.get("id_vente", ""))
                ),
                mandatory=True,
            )
        except BrokerUnavailable:
            raise
        except (pika.exceptions.AMQPError, OSError) as e:
            raise BrokerUnavailable(str(e) or f"RabbitMQ injoignable ({type(e).__name__})")
        finally:
            if conn and conn.is_open:
                conn.close()

    # ---------- consommateur ----------
    def start_consumer(self, handler, on_event=None):
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, args=(handler, on_event), daemon=True, name="hq-consumer")
        self._thread.start()

    def purge(self):
        try:
            conn = pika.BlockingConnection(self.params)
            ch = conn.channel()
            for q in (self.Q, *self.RETRIES, self.DLQ):
                try:
                    ch.queue_purge(queue=q)
                except Exception:
                    pass
            conn.close()
        except Exception:
            pass

    def stop(self):
        self._stop.set()
        if self._conn and self._conn.is_open:
            try:
                self._conn.close()
            except Exception:
                pass
        if self._thread:
            self._thread.join(timeout=5)
        self._conn = None
        self._ch = None

    def _run(self, handler, on_event):
        emit = on_event or (lambda k, d: None)
        reconnect_delay = 1.0
        while not self._stop.is_set():
            try:
                conn, ch = self._connect()
                reconnect_delay = 1.0
                ch.confirm_delivery()
                ch.basic_qos(prefetch_count=10)
                while not self._stop.is_set():
                    method, props, body = ch.basic_get(self.Q, auto_ack=False)
                    if method is None:
                        time.sleep(0.2)
                        continue
                    self._handle(ch, method, props, body, handler, emit)
                if conn.is_open:
                    conn.close()
            except (BrokerUnavailable, pika.exceptions.AMQPError, OSError):
                self._stop.wait(reconnect_delay)
                reconnect_delay = min(reconnect_delay * 2, 10.0)
            except Exception:
                logger.exception("Le consommateur RabbitMQ a planté ; reprise automatique")
                self._stop.wait(reconnect_delay)
                reconnect_delay = min(reconnect_delay * 2, 10.0)
            finally:
                try:
                    if self._conn is conn and self._conn and self._conn.is_open:
                        self._conn.close()
                except Exception:
                    pass
                self._conn = None
                self._ch = None

    def _handle(self, ch, method, props, body, handler, emit):
        retries = int((props.headers or {}).get("x-retries", 0))
        label = props.message_id or f"tag#{method.delivery_tag}"
        try:
            handler(json.loads(body))
        except PermanentError as e:
            self._dead(ch, body, props, f"Message invalide : {e}")
            emit("dlq", f"{label} → DLQ ({e})")
        except ValueError as e:
            self._dead(ch, body, props, f"JSON illisible : {e}")
            emit("dlq", f"{label} illisible → DLQ ({e})")
        except Exception as e:
            retries += 1
            if retries > config.MAX_RETRIES:
                self._dead(ch, body, props, f"Échec après {config.MAX_RETRIES} reprises : {e}")
                emit("dlq", f"{label} → DLQ après {config.MAX_RETRIES} reprises ({e})")
            else:
                retry_queue = self.RETRIES[min(retries - 1, len(self.RETRIES) - 1)]
                ch.basic_publish(
                    "",
                    retry_queue,
                    body,
                    pika.BasicProperties(
                        delivery_mode=2, content_type="application/json", message_id=props.message_id,
                        headers={"x-retries": retries},
                    ),
                )
                emit("retry", f"{label} : reprise {retries}/{config.MAX_RETRIES} ({e})")
        ch.basic_ack(method.delivery_tag)  # ACK final dans tous les cas (le message a été re-routé si besoin)

    def _dead(self, ch, body, props, reason):
        headers = dict(props.headers or {})
        headers["x-reason"] = reason
        ch.basic_publish(
            "",
            self.DLQ,
            body,
            pika.BasicProperties(delivery_mode=2, content_type="application/json", message_id=props.message_id, headers=headers),
        )

    # ---------- supervision ----------
    def stats(self):
        conn, ch = self._connect()
        try:
            q = ch.queue_declare(self.Q, passive=True, durable=True,
                                 arguments={"x-queue-type": "quorum", "x-dead-letter-exchange": "", "x-dead-letter-routing-key": self.DLQ})
            d = ch.queue_declare(self.DLQ, passive=True)
            return {"ready": q.method.message_count, "unacked": 0, "dlq": d.method.message_count}
        finally:
            if conn and conn.is_open:
                conn.close()

    def _scan_dlq(self, visit):
        """Parcourt la DLQ. visit(method, props, body, ch) -> True si le message doit être acquitté (retiré)."""
        conn, ch = self._connect()
        try:
            # Le rejeu manuel est une publication critique : attendre la
            # confirmation RabbitMQ avant d'acquitter le message de la DLQ.
            ch.confirm_delivery()
            held, last = [], None
            while True:
                m, p, b = ch.basic_get(self.DLQ, auto_ack=False)
                if m is None:
                    break
                last = m.delivery_tag
                if visit(m, p, b, ch):
                    ch.basic_ack(m.delivery_tag)
                else:
                    held.append(m.delivery_tag)
            if held:
                ch.basic_nack(delivery_tag=last, multiple=True, requeue=True)  # remet le reste en file
        finally:
            if conn and conn.is_open:
                conn.close()

    def list_dlq(self):
        out = []

        def visit(m, p, b, ch):
            try:
                body = json.loads(b)
            except ValueError:
                body = {"raw": b.decode(errors="replace")}
            h = p.headers or {}
            out.append({"key": p.message_id or str(m.delivery_tag), "body": body, "reason": h.get("x-reason", ""),
                        "retries": h.get("x-retries", 0), "created": None})
            return False

        self._scan_dlq(visit)
        return out

    def requeue(self, key):
        done = {"v": False}

        def visit(m, p, b, ch):
            if not done["v"] and (p.message_id or str(m.delivery_tag)) == key:
                ch.basic_publish("", self.Q, b, pika.BasicProperties(delivery_mode=2, content_type="application/json", message_id=p.message_id))
                done["v"] = True
                return True
            return False

        self._scan_dlq(visit)

    def discard(self, key):
        done = {"v": False}

        def visit(m, p, b, ch):
            if not done["v"] and (p.message_id or str(m.delivery_tag)) == key:
                done["v"] = True
                return True
            return False

        self._scan_dlq(visit)
