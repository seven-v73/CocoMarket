"""Copie les données du siège SQLite vers PostgreSQL.

Usage: DATABASE_URL=postgresql://... python migrate_sqlite_to_postgres.py
"""
import os
import sqlite3

import psycopg
from psycopg.rows import dict_row

import db


TABLES = ("stores", "products", "users", "stock", "sales", "sale_lines", "processed", "events", "settings")


def main():
    url = os.environ.get("DATABASE_URL")
    if not url:
        raise SystemExit("DATABASE_URL est obligatoire")
    with open(os.path.join(os.path.dirname(__file__), "postgres_schema.sql"), encoding="utf-8") as f:
        schema = f.read()
    source = sqlite3.connect(os.path.join(db.DATA_DIR, "siege.db"))
    source.row_factory = sqlite3.Row
    with psycopg.connect(url) as target:
        target.execute(schema)
        for table in TABLES:
            rows = source.execute(f"SELECT * FROM {table}").fetchall()
            if not rows:
                continue
            columns = rows[0].keys()
            names = ",".join(columns)
            placeholders = ",".join(["%s"] * len(columns))
            for row in rows:
                target.execute(f"INSERT INTO {table} ({names}) VALUES ({placeholders}) ON CONFLICT DO NOTHING", tuple(row))
        target.commit()
    source.close()
    print("Migration SQLite → PostgreSQL terminée")


if __name__ == "__main__":
    main()
