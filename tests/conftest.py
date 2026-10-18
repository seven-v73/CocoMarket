"""Configuration commune des tests : paramètres rapides + environnement vierge par fichier de test."""
import os
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

# Doit être défini AVANT l'import de config / db / app
os.environ["COCO_DATA"] = tempfile.mkdtemp(prefix="coco_boot_")
os.environ["COCO_BROKER"] = "local"
os.environ.setdefault("COCO_RETRY_DELAY", "0.2")
os.environ.setdefault("COCO_MAX_RETRIES", "3")
os.environ.setdefault("COCO_FLUSH_INTERVAL", "0.3")


def fresh_env():
    """Nouveau dossier de données + nouveau broker + nouveau consommateur : aucun état partagé entre fichiers."""
    import broker
    import consumer as HQ
    import db as D

    if broker._broker is not None:
        broker._broker.stop()
    broker.reset_broker()
    D.DATA_DIR = tempfile.mkdtemp(prefix="coco_test_")
    D.init()
    HQ.start_consumer()
