class BrokerUnavailable(Exception):
    """Le broker (ou la liaison Internet) est injoignable : la vente reste en attente côté caisse."""


class TransientError(Exception):
    """Erreur temporaire côté siège : le message sera retenté (retry)."""


class PermanentError(Exception):
    """Message invalide : inutile de réessayer, direction la Dead Letter Queue."""
