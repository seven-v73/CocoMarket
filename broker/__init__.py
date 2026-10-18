"""Fabrique de broker. Les deux implémentations exposent la même interface :

publish(body)                      -> publie une vente (lève BrokerUnavailable si injoignable)
start_consumer(handler, on_event)  -> consomme en arrière-plan (ACK, retry, DLQ gérés ici)
stats()                            -> {"ready": n, "unacked": n, "dlq": n}
list_dlq() / requeue(key) / discard(key)
"""
import config

_broker = None


def get_broker():
    global _broker
    if _broker is None:
        if config.BROKER == "rabbit":
            from .rabbit import RabbitBroker
            _broker = RabbitBroker()
        else:
            from .local import LocalBroker
            _broker = LocalBroker()
    return _broker


def reset_broker():
    global _broker
    if _broker is not None:
        try:
            _broker.stop()
        except Exception:
            pass
        if config.BROKER == "rabbit":
            try:
                _broker.purge()
            except Exception:
                pass
    _broker = None
