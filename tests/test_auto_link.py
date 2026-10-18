import caisse as K
import config


class BrokerOK:
    def stats(self):
        return {"ready": 0, "unacked": 0, "dlq": 0}


class BrokerDown:
    def stats(self):
        raise RuntimeError("broker indisponible")


def test_detection_automatique_broker_disponible(monkeypatch):
    monkeypatch.setattr(config, "LINK_MODE", "auto")
    monkeypatch.setattr(K, "get_broker", lambda: BrokerOK())
    assert K.link_up("ABJ01") is True


def test_detection_automatique_broker_indisponible(monkeypatch):
    monkeypatch.setattr(config, "LINK_MODE", "auto")
    monkeypatch.setattr(K, "get_broker", lambda: BrokerDown())
    assert K.link_up("ABJ01") is False
