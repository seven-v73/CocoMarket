"""Scénarios de panne : coupure Internet, doublons, message invalide (DLQ), panne du siège (retry)."""
import time
from concurrent.futures import ThreadPoolExecutor

import pytest
from conftest import fresh_env

import caisse as K
import db as D
from broker import get_broker

USER = {"username": "awa", "store": "ABJ01", "caisse": 1}
LINES = [{"sku": "RIZ-5KG", "qty": 2}, {"sku": "EAU-15L", "qty": 3}]  # 2*4500 + 3*400 = 10200


def wait(cond, timeout=10):
    end = time.time() + timeout
    while time.time() < end:
        if cond():
            return True
        time.sleep(0.1)
    return False


def sales_count():
    with D.db("siege") as c:
        return c.execute("SELECT COUNT(*) FROM sales").fetchone()[0]


def events(kind):
    with D.db("siege") as c:
        return c.execute("SELECT COUNT(*) FROM events WHERE kind=?", (kind,)).fetchone()[0]


@pytest.fixture(scope="function", autouse=True)
def setup():
    fresh_env()
    yield
    get_broker().stop()


def stock(store, sku):
    with D.db("siege") as c:
        return c.execute("SELECT qty FROM stock WHERE store=? AND sku=?", (store, sku)).fetchone()[0]


def test_vente_normale():
    before = stock("ABJ01", "RIZ-5KG")
    _, sent = K.make_sale(USER, LINES)
    assert sent is True
    assert wait(lambda: sales_count() == 1)
    assert stock("ABJ01", "RIZ-5KG") == before - 2


def test_lignes_produit_dupliquees_fusionnees():
    before = stock("ABJ01", "RIZ-5KG")
    sale_id, sent = K.make_sale(USER, [{"sku": "RIZ-5KG", "qty": 1}, {"sku": "RIZ-5KG", "qty": 2}])
    assert sent is True
    assert wait(lambda: sales_count() == 1)
    with D.db("siege") as c:
        lines = c.execute("SELECT sku, qty FROM sale_lines WHERE id_vente=?", (sale_id,)).fetchall()
    assert [(row["sku"], row["qty"]) for row in lines] == [("RIZ-5KG", 3)]
    assert stock("ABJ01", "RIZ-5KG") == before - 3


def test_id_vente_unique_sous_concurrence():
    fresh_env()
    with ThreadPoolExecutor(max_workers=8) as ex:
        ids = list(ex.map(lambda _: K.make_sale(USER, LINES)[0], range(20)))
    assert len(ids) == len(set(ids))


def test_producteur_refuse_magasin_ou_caisse_invalide():
    with pytest.raises(ValueError, match="Magasin ou numéro de caisse invalide"):
        K.make_sale({"username": "x", "store": "UNKNOWN", "caisse": 1}, LINES)
    with pytest.raises(ValueError, match="Magasin ou numéro de caisse invalide"):
        K.make_sale({"username": "x", "store": "KOR01", "caisse": 99}, LINES)
    with D.db("caisse") as c:
        assert c.execute("SELECT COUNT(*) FROM outbox").fetchone()[0] == 0


def test_coupure_internet_aucune_vente_perdue():
    K.set_link("ABJ01", False)
    ids = [K.make_sale(USER, LINES)[0] for _ in range(5)]
    _, pending = K.tickets("ABJ01", 1)
    assert pending == 5
    time.sleep(0.6)
    assert sales_count() == 0  # rien n'est arrivé au siège pendant la coupure
    assert K.flush_pending() == 0  # liaison toujours coupée

    K.set_link("ABJ01", True)  # la liaison revient
    assert K.flush_pending() == 5
    assert wait(lambda: sales_count() == 5)
    with D.db("siege") as c:
        got = {r[0] for r in c.execute("SELECT id_vente FROM sales")}
    assert set(ids) <= got
    assert K.tickets("ABJ01", 1)[1] == 0


def test_doublon_ignore():
    id_vente, _ = K.make_sale(USER, LINES)
    assert wait(lambda: sales_count() == 1)
    stock_after = stock("ABJ01", "RIZ-5KG")
    for _ in range(3):
        K.resend_duplicate(id_vente, "ABJ01")
    assert wait(lambda: events("doublon") == 3)
    assert sales_count() == 1
    assert stock("ABJ01", "RIZ-5KG") == stock_after  # stock non débité deux fois


def test_message_invalide_va_en_dlq_puis_requeue_et_discard():
    K.send_invalid(USER)
    assert wait(lambda: get_broker().stats()["dlq"] == 1)
    dlq = get_broker().list_dlq()
    assert "produit inconnu" in dlq[0]["reason"].lower()
    assert sales_count() == 0
    get_broker().discard(dlq[0]["key"])
    assert get_broker().stats()["dlq"] == 0


def test_message_tamper_prix_ou_caisse_va_en_dlq():
    body = {
        "id_vente": "ABJ01-C99-20260101-tamper",
        "magasin": "ABJ01",
        "caisse": 99,
        "date": "2026-01-01T10:00:00",
        "lignes": [{"produit": "RIZ-5KG", "qte": 1, "prix": 1}],
        "total": 1,
    }
    get_broker().publish(body)
    assert wait(lambda: get_broker().stats()["dlq"] == 1)
    assert sales_count() == 0


def test_panne_siege_reprises_puis_succes():
    D.set_setting("hq_down", "1")
    K.make_sale(USER, LINES)
    assert wait(lambda: events("retry") >= 1)
    assert sales_count() == 0
    D.set_setting("hq_down", "0")  # le siège revient avant épuisement des reprises
    assert wait(lambda: sales_count() == 1)
    assert get_broker().stats()["dlq"] == 0


def test_panne_siege_longue_epuise_les_reprises_vers_dlq():
    D.set_setting("hq_down", "1")
    K.make_sale(USER, LINES)
    assert wait(lambda: get_broker().stats()["dlq"] == 1, timeout=15)
    D.set_setting("hq_down", "0")
    key = get_broker().list_dlq()[0]["key"]
    get_broker().requeue(key)  # rejeu manuel depuis la DLQ
    assert wait(lambda: sales_count() == 1)
    assert get_broker().stats()["dlq"] == 0


def test_rapprochement_zero_ecart():
    with D.db("caisse") as c:
        emitted = c.execute("SELECT COALESCE(SUM(total),0) FROM outbox WHERE status='sent'").fetchone()[0]
    with D.db("siege") as c:
        received = c.execute("SELECT COALESCE(SUM(total),0) FROM sales").fetchone()[0]
    assert emitted == received
