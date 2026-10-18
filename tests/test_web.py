"""Tests de l'application web : rôles, CSRF, circuit complet caisse -> broker -> siège -> dashboard."""
import re
import time

import pytest
from conftest import fresh_env

import app as A


def csrf_of(client):
    with client.session_transaction() as s:
        return s["csrf"]


def login(client, user, pw):
    client.get("/login")
    return client.post("/login", data={"username": user, "password": pw, "csrf": csrf_of(client)}, follow_redirects=False)


def jpost(client, url, body=None):
    return client.post(url, json=body or {}, headers={"X-CSRF": csrf_of(client)})


def wait(cond, timeout=10):
    end = time.time() + timeout
    while time.time() < end:
        if cond():
            return True
        time.sleep(0.1)
    return False


@pytest.fixture(scope="module")
def clients():
    fresh_env()
    out = {}
    for name, (u, p) in {"caisse": ("awa", "caisse123"), "siege": ("siege", "siege123"), "admin": ("admin", "admin123")}.items():
        c = A.app.test_client()
        r = login(c, u, p)
        assert r.status_code == 302, name
        out[name] = c
    return out


def test_pages_rendent(clients):
    assert clients["caisse"].get("/caisse").status_code == 200
    assert clients["siege"].get("/siege").status_code == 200
    assert clients["siege"].get("/siege/dlq").status_code == 200
    assert clients["admin"].get("/siege/utilisateurs").status_code == 200
    assert clients["admin"].get("/siege/catalogue").status_code == 200
    assert clients["admin"].get("/siege/journal").status_code == 200
    assert clients["admin"].get("/siege/journal/export").status_code == 200
    assert clients["admin"].get("/compte").status_code == 200
    assert A.app.test_client().get("/login").status_code == 200
    assert A.app.test_client().get("/inexistant").status_code == 404


def test_health_metrics_et_en_tetes_de_securite():
    c = A.app.test_client()
    r = c.get("/health/live", headers={"X-Request-ID": "audit-123"})
    assert r.status_code == 200 and r.json["status"] == "ok"
    assert r.headers["X-Request-ID"] == "audit-123"
    assert r.headers["X-Content-Type-Options"] == "nosniff"
    assert r.headers["X-Frame-Options"] == "DENY"
    m = c.get("/metrics")
    assert m.status_code == 200 and "cocomarket_outbox_pending" in m.get_data(as_text=True)


def test_controle_des_roles(clients):
    assert clients["caisse"].get("/siege").status_code == 403
    assert clients["caisse"].get("/siege/utilisateurs").status_code == 403
    assert clients["siege"].get("/caisse").status_code == 403
    assert clients["siege"].get("/siege/utilisateurs").status_code == 403
    assert clients["siege"].get("/siege/catalogue").status_code == 403
    assert clients["siege"].get("/siege/journal").status_code == 403
    assert clients["siege"].get("/siege/journal/export").status_code == 403
    assert jpost(clients["siege"], "/api/siege/hq_down", {"down": True}).status_code == 403
    anon = A.app.test_client()
    assert anon.get("/siege").status_code == 302
    assert anon.get("/api/siege/overview").status_code == 401


def test_csrf_obligatoire(clients):
    r = clients["caisse"].post("/api/caisse/sale", json={"lines": [{"sku": "RIZ-5KG", "qty": 1}]})
    assert r.status_code == 400


def test_mauvais_mot_de_passe():
    c = A.app.test_client()
    r = login(c, "awa", "faux")
    assert r.status_code == 200 and "incorrect" in r.get_data(as_text=True)


def test_circuit_complet_avec_coupure(clients):
    caisse, siege = clients["caisse"], clients["siege"]
    r = jpost(caisse, "/api/caisse/sale", {"lines": [{"sku": "RIZ-5KG", "qty": 2}]})
    assert r.status_code == 200 and r.json["sent"] is True
    assert wait(lambda: siege.get("/api/siege/overview").json["kpis"]["nb"] == 1)

    assert jpost(caisse, "/api/caisse/link", {"up": False}).json["link"] is False
    for _ in range(3):
        assert jpost(caisse, "/api/caisse/sale", {"lines": [{"sku": "EAU-15L", "qty": 5}]}).json["sent"] is False
    d = siege.get("/api/siege/overview").json
    assert d["kpis"]["pending_total"] == 3
    ABJ = next(x for x in d["recon"] if x["store"] == "ABJ01")
    assert ABJ["status"] == "pending" and ABJ["ecart"] == 3 * 5 * 400 and ABJ["link"] is False

    assert jpost(caisse, "/api/caisse/link", {"up": True}).json["link"] is True
    assert wait(lambda: siege.get("/api/siege/overview").json["kpis"]["nb"] == 4)
    d = siege.get("/api/siege/overview").json
    assert d["kpis"]["pending_total"] == 0
    assert all(x["ecart"] == 0 for x in d["recon"])
    assert d["kpis"]["ca"] == 2 * 4500 + 3 * 5 * 400


def test_vente_invalide_refusee_cote_caisse(clients):
    assert jpost(clients["caisse"], "/api/caisse/sale", {"lines": []}).status_code == 400
    assert jpost(clients["caisse"], "/api/caisse/sale", {"lines": [{"sku": "X", "qty": 1}]}).status_code == 400
    assert jpost(clients["caisse"], "/api/caisse/sale", {"lines": [{"sku": "RIZ-5KG", "qty": -3}]}).status_code == 400


def test_doublon_et_dlq_visibles(clients):
    caisse, siege, admin = clients["caisse"], clients["siege"], clients["admin"]
    tid = caisse.get("/api/caisse/state").json["tickets"][0]["id_vente"]
    jpost(caisse, "/api/caisse/test/duplicate", {"id_vente": tid})
    assert wait(lambda: siege.get("/api/siege/overview").json["kpis"]["duplicates"] >= 1)
    jpost(caisse, "/api/caisse/test/invalid")
    assert wait(lambda: siege.get("/api/siege/overview").json["kpis"]["dlq"] == 1)
    html = siege.get("/siege/dlq").get_data(as_text=True)
    assert "produit inconnu" in html.lower()
    key = re.search(r'name="key" value="([^"]+)"', admin.get("/siege/dlq").get_data(as_text=True)).group(1)
    r = admin.post("/siege/dlq/discard", data={"key": key, "csrf": csrf_of(admin)})
    assert r.status_code == 302
    assert siege.get("/api/siege/overview").json["kpis"]["dlq"] == 0


def test_gestion_utilisateurs(clients):
    admin = clients["admin"]
    f = {"csrf": csrf_of(admin), "full_name": "Mariam Diallo", "username": "mariam", "role": "caissier",
         "store": "KOR01", "caisse": "2", "password": "motdepasse1"}
    admin.post("/siege/utilisateurs/creer", data=f)
    c = A.app.test_client()
    assert login(c, "mariam", "motdepasse1").status_code == 302
    assert c.get("/caisse").status_code == 200
    # numéro de caisse hors limite (KOR01 a 3 caisses) et doublon d'identifiant
    admin.post("/siege/utilisateurs/creer", data=dict(f, username="mariam2", caisse="9"))
    admin.post("/siege/utilisateurs/creer", data=f)
    page = admin.get("/siege/utilisateurs").get_data(as_text=True)
    assert "mariam2" not in page
    # désactivation => la session existante est invalidée, connexion impossible
    uid = int(re.search(r"/siege/utilisateurs/(\d+)/toggle", page.split("<b>mariam</b>")[1]).group(1))
    admin.post(f"/siege/utilisateurs/{uid}/toggle", data={"csrf": csrf_of(admin)})
    assert c.get("/caisse").status_code == 302
    assert login(A.app.test_client(), "mariam", "motdepasse1").status_code == 200


def test_simulation_admin(clients):
    r = jpost(clients["admin"], "/api/siege/simulate", {"n": 20})
    assert r.status_code == 200 and r.json["n"] == 20
    assert wait(lambda: clients["siege"].get("/api/siege/overview").json["kpis"]["nb"] >= 24)
    d = clients["siege"].get("/api/siege/overview?period=7d&store=").json
    assert len(d["series"]["labels"]) == 7 and d["top"] and d["by_store"]
