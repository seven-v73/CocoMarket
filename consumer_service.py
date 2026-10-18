"""Processus indépendant du consommateur siège.

Production : COCO_COMPONENT=consumer python consumer_service.py
"""
import logging
import signal
import time

import config
import consumer as HQ
import db as D
from broker import get_broker

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
stop = False


def shutdown(*_):
    global stop
    stop = True
    get_broker().stop()


signal.signal(signal.SIGTERM, shutdown)
signal.signal(signal.SIGINT, shutdown)
D.init()
broker = get_broker()
HQ.start_consumer()
logging.info("Consommateur siège démarré avec %s", broker.name)
while not stop:
    time.sleep(1)
