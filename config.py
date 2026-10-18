"""Configuration CocoMarket (surchargeable par variables d'environnement)."""
import os

ENV = os.environ.get("COCO_ENV", "development").lower()

# "local"  : broker persistant embarqué (SQLite), pratique pour la démo sans installation
# "rabbit" : RabbitMQ (production)
BROKER = os.environ.get("COCO_BROKER", "rabbit").lower()
if BROKER not in {"local", "rabbit"}:
    raise RuntimeError("COCO_BROKER doit valoir 'rabbit' ou 'local'")
RABBIT_URL = os.environ.get("RABBIT_URL", "amqp://guest:guest@localhost:5672/%2F")
RABBIT_QUEUE_PREFIX = os.environ.get("COCO_RABBIT_QUEUE_PREFIX", "cocomarket.v3")
DATABASE_URL = os.environ.get("DATABASE_URL", "")
if ENV == "production" and ("guest:guest" in RABBIT_URL or not DATABASE_URL):
    raise RuntimeError("RABBIT_URL personnalisé et DATABASE_URL sont obligatoires en production")

# Nombre maximal de reprises avant envoi en DLQ
MAX_RETRIES = int(os.environ.get("COCO_MAX_RETRIES", 3))
# Délai de base entre deux reprises (secondes). Local : exponentiel (x2). RabbitMQ : fixe (TTL de la file retry).
RETRY_DELAY = float(os.environ.get("COCO_RETRY_DELAY", 3))
# Fréquence de relance des ventes en attente côté caisse (secondes)
FLUSH_INTERVAL = float(os.environ.get("COCO_FLUSH_INTERVAL", 2))
LINK_MODE = os.environ.get("COCO_LINK_MODE", "simulated").lower()
if LINK_MODE not in {"simulated", "auto"}:
    raise RuntimeError("COCO_LINK_MODE doit valoir 'simulated' ou 'auto'")

SECRET_KEY = os.environ.get("COCO_SECRET")
if ENV == "production" and not SECRET_KEY:
    raise RuntimeError("COCO_SECRET est obligatoire en production")
SECRET_KEY = SECRET_KEY or os.urandom(32).hex()
BOOTSTRAP_ADMIN_PASSWORD = os.environ.get("COCO_BOOTSTRAP_ADMIN_PASSWORD", "admin123")
if ENV == "production" and len(BOOTSTRAP_ADMIN_PASSWORD) < 12:
    raise RuntimeError("COCO_BOOTSTRAP_ADMIN_PASSWORD doit contenir au moins 12 caractères en production")
SEED_DEMO_USERS = os.environ.get("COCO_SEED_DEMO_USERS", "1" if ENV != "production" else "0") == "1"
MIN_PASSWORD_LENGTH = 12 if ENV == "production" else 8
START_WORKERS = os.environ.get("COCO_WORKERS", "1") == "1"
COMPONENT = os.environ.get("COCO_COMPONENT", "all").lower()
PORT = int(os.environ.get("PORT", 5000))

if not 1 <= MAX_RETRIES <= 10:
    raise RuntimeError("COCO_MAX_RETRIES doit être compris entre 1 et 10")
if RETRY_DELAY <= 0:
    raise RuntimeError("COCO_RETRY_DELAY doit être strictement positif")
if FLUSH_INTERVAL <= 0:
    raise RuntimeError("COCO_FLUSH_INTERVAL doit être strictement positif")
if not 1 <= PORT <= 65535:
    raise RuntimeError("PORT doit être compris entre 1 et 65535")
