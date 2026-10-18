"""Contrôle automatisé du rapprochement caisse/siège.

Code retour 0 : l'écart est exactement expliqué par l'outbox en attente.
Code retour 2 : un écart inexpliqué est détecté.
"""
import argparse
import json
import db as D


def main():
    parser = argparse.ArgumentParser(description="Contrôle du rapprochement CocoMarket")
    parser.add_argument("--strict", action="store_true", help="échoue aussi si des ventes sont encore en attente")
    parser.add_argument("--json", action="store_true", dest="as_json", help="produit un résultat JSON")
    args = parser.parse_args()

    with D.db("siege") as c:
        stores = {r["code"]: r["name"] for r in c.execute("SELECT code,name FROM stores")}
        received = {r["store"]: dict(r) for r in c.execute(
            "SELECT store, COUNT(*) tickets, COALESCE(SUM(total),0) total FROM sales GROUP BY store"
        )}
    with D.db("caisse") as c:
        emitted = {r["store"]: dict(r) for r in c.execute(
            "SELECT store, COUNT(*) tickets, COALESCE(SUM(total),0) total, "
            "SUM(status='pending') pending, COALESCE(SUM(CASE WHEN status='pending' THEN total ELSE 0 END),0) pending_total "
            "FROM outbox GROUP BY store"
        )}

    failed = False
    results = []
    for code in sorted(stores):
        e, r = emitted.get(code, {}), received.get(code, {})
        caisse_total = e.get("total", 0) or 0
        siege_total = r.get("total", 0) or 0
        pending_total = e.get("pending_total", 0) or 0
        unexplained = caisse_total - siege_total - pending_total
        results.append({"store": code, "name": stores[code], "caisse_total": caisse_total,
                        "siege_total": siege_total, "pending_total": pending_total,
                        "unexplained_gap": unexplained})
        failed |= unexplained != 0 or (args.strict and pending_total != 0)
    if args.as_json:
        print(json.dumps({"ok": not failed, "strict": args.strict, "stores": results}, ensure_ascii=False))
    else:
        print("magasin | caisse | siège | attente | écart inexpliqué")
        for row in results:
            print(f"{row['store']} | {row['caisse_total']} | {row['siege_total']} | "
                  f"{row['pending_total']} | {row['unexplained_gap']}")
    return 2 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
