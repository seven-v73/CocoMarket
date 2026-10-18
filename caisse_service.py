"""Agent d'envoi autonome pour une caisse.

L'interface web peut continuer à créer les ventes dans l'outbox SQLite ; cet
agent est le processus qui les transmet réellement au broker.
"""
import logging
import os
import signal
import time

import caisse as K
import config
import db as D

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
stop = False


def shutdown(*_):
    global stop
    stop = True


signal.signal(signal.SIGTERM, shutdown)
signal.signal(signal.SIGINT, shutdown)
D.init()
store = os.environ.get("COCO_STORE")
if not store:
    raise SystemExit("COCO_STORE est obligatoire pour un agent caisse séparé")
with D.db("siege") as c:
    if c.execute("SELECT 1 FROM stores WHERE code=?", (store,)).fetchone() is None:
        raise SystemExit(f"COCO_STORE inconnu : {store}")
logging.info("Agent caisse démarré pour %s, intervalle %.2fs", store, config.FLUSH_INTERVAL)
while not stop:
    try:
        K.flush_pending(store)
    except Exception:
        logging.exception("Erreur pendant la vidange de l'outbox")
    time.sleep(config.FLUSH_INTERVAL)
