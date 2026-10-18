"""Tests d'intégration du backend PostgreSQL."""
import os

import pytest

if not os.environ.get("DATABASE_URL"):
    pytest.skip("DATABASE_URL absent : test PostgreSQL ignoré", allow_module_level=True)

import consumer
import db as D


@pytest.fixture(scope="module", autouse=True)
def initialized():
    D.init()


def test_postgres_initialise_et_traite_une_vente():
    body = {"id_vente": "PG-TEST-0001", "magasin": "ABJ01", "caisse": 1,
            "caissier": "awa", "date": D.now_iso(),
            "lignes": [{"produit": "EAU-15L", "qte": 2, "prix": 400}], "total": 800}
    with D.db("siege") as c:
        before = c.execute("SELECT qty FROM stock WHERE store=? AND sku=?", ("ABJ01", "EAU-15L")).fetchone()[0]
    consumer.handle(body)
    with D.db("siege") as c:
        sale = c.execute("SELECT total FROM sales WHERE id_vente=?", (body["id_vente"],)).fetchone()
        after = c.execute("SELECT qty FROM stock WHERE store=? AND sku=?", ("ABJ01", "EAU-15L")).fetchone()[0]
    assert sale["total"] == 800
    assert after == before - 2


def test_postgres_idempotence():
    body = {"id_vente": "PG-TEST-0002", "magasin": "ABJ01", "caisse": 1,
            "date": D.now_iso(), "lignes": [{"produit": "EAU-15L", "qte": 1, "prix": 400}], "total": 400}
    consumer.handle(body)
    consumer.handle(body)
    with D.db("siege") as c:
        assert c.execute("SELECT COUNT(*) AS n FROM sales WHERE id_vente=?", (body["id_vente"],)).fetchone()["n"] == 1
