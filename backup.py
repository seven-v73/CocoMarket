"""Sauvegarde cohérente des bases SQLite et PostgreSQL CocoMarket."""
import argparse
import datetime as dt
import os
import sqlite3
import subprocess

import db
import config


def backup_one(source, target):
    os.makedirs(os.path.dirname(target), exist_ok=True)
    src, dst = sqlite3.connect(source), sqlite3.connect(target)
    try:
        src.backup(dst)
        result = dst.execute("PRAGMA integrity_check").fetchone()[0]
        if result != "ok":
            raise RuntimeError(f"Base sauvegardée invalide : {result}")
        dst.commit()
    finally:
        dst.close()
        src.close()


def backup_postgres(target, verify=False):
    """Sauvegarde la base centrale avec les outils PostgreSQL officiels."""
    if not config.DATABASE_URL:
        return False
    os.makedirs(os.path.dirname(target), exist_ok=True)
    if verify:
        subprocess.run(["pg_restore", "--list", target], check=True, stdout=subprocess.DEVNULL)
        print(f"OK {target}")
    else:
        subprocess.run(
            ["pg_dump", "--dbname", config.DATABASE_URL, "--format", "custom", "--file", target],
            check=True,
        )
        print(f"Sauvegarde créée : {target}")
    return True


def main():
    parser = argparse.ArgumentParser(description="Sauvegarde des bases CocoMarket")
    parser.add_argument("directory")
    parser.add_argument("--verify", action="store_true")
    args = parser.parse_args()
    stamp = dt.datetime.now().strftime("%Y%m%d-%H%M%S")
    postgres_target = os.path.join(args.directory, "siege-postgres-" + stamp + ".dump")
    if config.DATABASE_URL and args.verify:
        candidates = sorted(
            os.path.join(args.directory, name)
            for name in os.listdir(args.directory)
            if name.startswith("siege-postgres-") and name.endswith(".dump")
        ) if os.path.isdir(args.directory) else []
        if not candidates:
            raise SystemExit("Aucune sauvegarde PostgreSQL à vérifier")
        backup_postgres(candidates[-1], verify=True)
    elif config.DATABASE_URL:
        backup_postgres(postgres_target)
    for name in ("siege", "caisse", "broker"):
        source = os.path.join(db.DATA_DIR, name + ".db")
        if not os.path.exists(source):
            continue
        if args.verify:
            con = sqlite3.connect(source)
            result = con.execute("PRAGMA integrity_check").fetchone()[0]
            con.close()
            if result != "ok":
                raise SystemExit(f"Base corrompue : {source} ({result})")
            print(f"OK {source}")
        else:
            target = os.path.join(args.directory, name + "-" + stamp + ".db")
            backup_one(source, target)
            print(f"Sauvegarde créée : {target}")


if __name__ == "__main__":
    main()
