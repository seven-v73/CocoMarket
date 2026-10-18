"""Producteur de ventes indépendant de Flask.

Exemples:
  COCO_STORE=ABJ01 python caisse_simulator.py --caisse 1 --count 10
  COCO_STORE=ABJ01 python caisse_simulator.py --caisse 1 --continuous --interval 5
"""
import argparse
import logging
import os
import random
import signal
import time

import caisse as K
import db as D

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
stop = False


def shutdown(*_):
    global stop
    stop = True


def main():
    global stop
    parser = argparse.ArgumentParser(description="Simulateur de producteur caisse")
    parser.add_argument("--store", default=None)
    parser.add_argument("--caisse", type=int, required=True)
    parser.add_argument("--count", type=int, default=1)
    parser.add_argument("--interval", type=float, default=2)
    parser.add_argument("--continuous", action="store_true")
    args = parser.parse_args()
    if args.count < 0:
        parser.error("--count doit être positif ou nul")
    if args.interval < 0:
        parser.error("--interval doit être positif ou nul")
    store = args.store or os.environ.get("COCO_STORE")
    if not store:
        raise SystemExit("--store ou COCO_STORE est obligatoire")
    D.init()
    with D.db("siege") as c:
        shop = c.execute("SELECT caisses FROM stores WHERE code=?", (store,)).fetchone()
        skus = [r["sku"] for r in c.execute("SELECT sku FROM products").fetchall()]
    if not shop:
        raise SystemExit(f"Magasin inconnu : {store}")
    if not 1 <= args.caisse <= shop["caisses"]:
        raise SystemExit("Numéro de caisse invalide pour ce magasin")
    signal.signal(signal.SIGINT, shutdown)
    signal.signal(signal.SIGTERM, shutdown)
    remaining = None if args.continuous else args.count
    while not stop and (remaining is None or remaining > 0):
        lines = [{"sku": sku, "qty": random.randint(1, 3)} for sku in random.sample(skus, min(random.randint(1, 3), len(skus)))]
        sale_id, sent = K.make_sale({"username": "simulator", "store": store, "caisse": args.caisse}, lines)
        logging.info("vente=%s store=%s sent=%s", sale_id, store, sent)
        if remaining is not None:
            remaining -= 1
        if not stop and (remaining is None or remaining > 0):
            time.sleep(args.interval)


if __name__ == "__main__":
    main()
