"""Restauration contrôlée des bases SQLite CocoMarket."""
import argparse
import os
import re
import sqlite3
import tempfile


def valid_sqlite(path):
    con = sqlite3.connect(path)
    try:
        return con.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    finally:
        con.close()


def main():
    parser = argparse.ArgumentParser(description="Restaure un jeu de sauvegardes SQLite CocoMarket")
    parser.add_argument("backup_dir")
    parser.add_argument("target_dir")
    parser.add_argument("--force", action="store_true", help="autorise le remplacement des bases cibles")
    args = parser.parse_args()
    if not os.path.isdir(args.backup_dir):
        raise SystemExit("Dossier de sauvegarde introuvable")
    os.makedirs(args.target_dir, exist_ok=True)
    files = os.listdir(args.backup_dir)
    by_stamp = {}
    for filename in files:
        match = re.fullmatch(r"(siege|caisse|broker)-(\d{8}-\d{6})\.db", filename)
        if match:
            by_stamp.setdefault(match.group(2), {})[match.group(1)] = os.path.join(args.backup_dir, filename)
    complete = {stamp: group for stamp, group in by_stamp.items() if len(group) == 3}
    if not complete:
        raise SystemExit("Aucun jeu SQLite complet (siege/caisse/broker) trouvé")
    stamp = sorted(complete)[-1]
    selected = complete[stamp]
    print(f"Jeu de sauvegarde sélectionné : {stamp}")
    for name, source in selected.items():
        if not valid_sqlite(source):
            raise SystemExit(f"Sauvegarde invalide : {source}")
        target = os.path.join(args.target_dir, name + ".db")
        if os.path.exists(target) and not args.force:
            raise SystemExit(f"La cible existe déjà : {target} (utiliser --force)")
        fd, temporary = tempfile.mkstemp(prefix=name + "-", suffix=".db", dir=args.target_dir)
        os.close(fd)
        try:
            src, dst = sqlite3.connect(source), sqlite3.connect(temporary)
            try:
                src.backup(dst)
                dst.commit()
            finally:
                dst.close()
                src.close()
            os.replace(temporary, target)
            print(f"Restauration effectuée : {target}")
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
