# CocoMarket

Système de remontée asynchrone des ventes. Une caisse enregistre d'abord la
vente localement, puis la publie vers RabbitMQ. Le siège consomme les messages,
ignore les doublons et met à jour le stock et le chiffre d'affaires.

La garantie applicative est une livraison au moins une fois. Une coupure peut
provoquer une republication, mais l'idempotence empêche une double comptabilisation.

## Fonctionnalités

- interface caisse responsive ;
- dashboard siège et rapprochement par magasin ;
- outbox SQLite locale ;
- simulateur Python de caisse indépendant de Flask ;
- RabbitMQ durable, publisher confirms, retry et DLQ ;
- idempotence des ventes ;
- gestion des utilisateurs et rôles ;
- catalogue et stocks administrables ;
- simulation de coupure ;
- healthchecks et métriques ;
- sauvegarde et vérification SQLite/PostgreSQL ;
- mode local pour les tests sans RabbitMQ ;
- en-têtes HTTP de sécurité et cookies de session durcis ;
- identifiant X-Request-ID pour corréler les appels et incidents.
- journal HTTP avec statut et durée de chaque requête.
- initialisation PostgreSQL protégée contre les courses entre workers.
- limitation temporaire des tentatives de connexion.
- schéma et outil de migration PostgreSQL pour le siège.

## Architecture

    Interface Flask
          |
    Outbox SQLite de la caisse
          |
    Agent caisse indépendant
          |
    RabbitMQ : ventes, retry, DLQ
          |
    Consommateur siège indépendant
          |
    Base siège : ventes, lignes, stock, événements

En production, le web, le consommateur et chaque agent caisse sont des
processus séparés. La caisse applique le pattern *store and forward* : la
vente est validée, fusionnée et écrite dans l'outbox avant toute publication.
Le siège traite les messages dans une transaction atomique (vente, lignes,
stock et idempotence).

## Prérequis

- Python 3.11 ou supérieur ;
- Docker et Docker Compose ;
- pip ;
- 2 Go de mémoire recommandés.

## Installation

    python -m venv venv
    source venv/bin/activate
    pip install -r requirements.txt

Les commandes Makefile disponibles sont make install, make check, make test, make up,
make down, make logs, make backup et make verify.

La composition Docker lance également Prometheus sur http://localhost:9090.
Sa configuration se trouve dans monitoring/prometheus.yml et collecte
automatiquement /metrics du service web.
Les règles de monitoring dans monitoring/alerts.yml signalent une DLQ non
vide, une outbox qui s'accumule, une hausse anormale des tentatives d'envoi
ou un web indisponible.

Sous PowerShell :

    venv\Scripts\Activate.ps1
    pip install -r requirements.txt

## Démarrage avec RabbitMQ

Créer un secret :

    export COCO_SECRET="$(python -c 'import secrets; print(secrets.token_urlsafe(32))')"
    export COCO_BOOTSTRAP_ADMIN_PASSWORD="$(python -c 'import secrets; print(secrets.token_urlsafe(18))')"

Démarrer RabbitMQ :

    docker compose up -d --wait

RabbitMQ est disponible sur le port AMQP 5672. Avec la composition de
services, la console est sur http://localhost:15672 et utilise
`RABBITMQ_USER`/`RABBITMQ_PASSWORD`.

Lancer l'application :

    COCO_BROKER=rabbit python app.py

Ouvrir http://localhost:5000.

## Déploiement séparé

    COCO_COMPONENT=web COCO_WORKERS=0 python app.py
    COCO_COMPONENT=consumer python consumer_service.py
    COCO_STORE=ABJ01 python caisse_service.py
    COCO_STORE=BKE01 python caisse_service.py
    COCO_STORE=KOR01 python caisse_service.py

Chaque agent caisse exige COCO_STORE et ne traite que l'outbox de son magasin.
Dans Docker, SQLite reste local à l'outbox de caisse tandis que PostgreSQL
fournit le catalogue partagé et la validation des magasins.

Le producteur peut également fonctionner sans interface web :

    COCO_STORE=ABJ01 python caisse_simulator.py --caisse 1 --count 10
    COCO_STORE=ABJ01 python caisse_simulator.py --caisse 1 --continuous --interval 5

## Déploiement Docker

Le fichier docker-compose.services.yml lance RabbitMQ, le web, le
consommateur et trois agents caisse (ABJ01, BKE01 et KOR01).

    export COCO_SECRET="$(python -c 'import secrets; print(secrets.token_urlsafe(32))')"
    export COCO_BOOTSTRAP_ADMIN_PASSWORD="$(python -c 'import secrets; print(secrets.token_urlsafe(18))')"
    export RABBITMQ_USER=cocomarket
    export RABBITMQ_PASSWORD="$(python -c 'import secrets; print(secrets.token_urlsafe(24))')"
    docker compose -f docker-compose.services.yml up -d --build

Arrêt :

    docker compose -f docker-compose.services.yml down

La composition lance PostgreSQL pour la base centrale, RabbitMQ, Prometheus,
le web, le consommateur et trois agents caisse. Elle applique des
healthchecks, des redémarrages automatiques et des délais d'arrêt propres.
SQLite reste utilisé pour les outbox locales. Définir aussi
`POSTGRES_PASSWORD` avant le démarrage.

Migration PostgreSQL : démarrer PostgreSQL, définir DATABASE_URL, puis lancer
la commande suivante depuis une copie de sauvegarde :

    python migrate_sqlite_to_postgres.py

Le schéma cible est dans postgres_schema.sql. Lorsque DATABASE_URL est défini,
db.py utilise PostgreSQL pour siege et garde SQLite pour caisse et broker local.
SQLite reste la valeur par défaut pour les tests sans DATABASE_URL.

## Mode local

    COCO_BROKER=local python app.py

Les messages sont alors stockés dans data/broker.db.

## Comptes de démonstration

| Rôle | Identifiant | Mot de passe | Accès |
|---|---|---|---|
| Administrateur | admin | admin123 | Administration complète |
| Siège | siege | siege123 | Dashboard et DLQ |
| Caissier Abidjan | awa | caisse123 | Caisse ABJ01 |
| Caissier Bouaké | kouame | caisse123 | Caisse BKE01 |
| Caissier Korhogo | fatou | caisse123 | Caisse KOR01 |

En développement, ces comptes facilitent la démonstration. En production,
seul l'administrateur bootstrap est créé ; il doit ensuite créer les comptes
siège et caissiers depuis « Utilisateurs ».

## Scénario de démonstration

1. Se connecter avec admin.
2. Générer des ventes de démonstration.
3. Se connecter avec fatou dans une autre fenêtre.
4. Activer Mode simulation dans la sidebar.
5. Encaisser des ventes : elles restent dans l'outbox.
6. Désactiver le toggle : elles sont renvoyées automatiquement.
7. Renvoyer un ticket traité : il est ignoré comme doublon.
8. Envoyer une vente invalide : elle arrive dans la DLQ.
9. Utiliser Catalogue & stock pour ajouter un produit ou ajuster une quantité.

## Administration

Catalogue & stock permet d'ajouter des produits, modifier les prix et
catégories, définir les seuils, ajuster le stock par magasin et consulter les
ruptures.

Utilisateurs permet de créer, désactiver et réinitialiser les comptes.

## Supervision et observabilité

    GET /health/live
    GET /health/ready
    GET /metrics

`/health/live` indique que le processus répond ; `/health/ready` vérifie la
base et le broker ; `/metrics` expose les ventes, l'outbox, les tentatives
d'envoi, le nombre d'attentes par magasin, la DLQ et les files RabbitMQ.
Chaque réponse possède un `X-Request-ID` et les requêtes sont journalisées
avec leur durée. Prometheus alerte sur la DLQ, l'accumulation d'outbox, les
retry storms et l'indisponibilité du web.

## Sauvegardes

    python backup.py /var/backups/cocomarket
    python backup.py /var/backups/cocomarket --verify

Sans `DATABASE_URL`, le script sauvegarde et vérifie `siege.db`, `caisse.db`
et `broker.db` sans arrêter l'application. Avec PostgreSQL, il ajoute une
sauvegarde native `siege-postgres-*.dump` via `pg_dump`; `--verify` contrôle
le dernier dump avec `pg_restore`. Les outils PostgreSQL doivent être
installés sur la machine qui lance la sauvegarde.

## Tests

    python -m pytest tests -q

Les tests couvrent les ventes, la concurrence, les coupures, l'outbox, les
doublons, les retries, la DLQ, les rôles, le CSRF, les utilisateurs et
l'interface web. Les tests PostgreSQL sont exécutés automatiquement lorsqu'une
variable DATABASE_URL est définie ; sinon ils sont ignorés.
Les healthchecks, métriques, en-têtes de sécurité et identifiants de requête
sont également vérifiés.
Le mode automatique de détection RabbitMQ possède aussi des tests dédiés.

## Configuration

| Variable | Défaut | Description |
|---|---|---|
| COCO_BROKER | rabbit | rabbit ou local |
| RABBIT_URL | URL locale | URL AMQP |
| COCO_RABBIT_QUEUE_PREFIX | cocomarket.v2 | Version des files |
| COCO_SECRET | aléatoire | Clé Flask |
| COCO_BOOTSTRAP_ADMIN_PASSWORD | admin123 en développement | Mot de passe admin créé lors du premier démarrage |
| COCO_SEED_DEMO_USERS | 1 en développement, 0 en production | Crée les comptes de démonstration supplémentaires |
| COCO_ENV | development | production impose secret, RabbitMQ, PostgreSQL et bootstrap admin |
| DATABASE_URL | vide | PostgreSQL du siège ; SQLite si vide |
| COCO_DATA | ./data | Dossier des bases |
| COCO_COMPONENT | all | all, web ou consumer |
| COCO_WORKERS | 1 | Workers intégrés si 1 |
| COCO_STORE | vide | Magasin d'un agent caisse |
| COCO_LINK_MODE | simulated | simulated pour la démo, auto pour tester RabbitMQ réellement |
| COCO_MAX_RETRIES | 3 | Reprises avant DLQ |
| COCO_RETRY_DELAY | 3 | Délai initial en secondes ; RabbitMQ applique un délai exponentiel et crée une file par reprise |
| COCO_FLUSH_INTERVAL | 2 | Intervalle de vidange |
| COCO_HTTPS | 0 | Cookies Secure si 1 |
| PORT | 5000 | Port HTTP |

## Dépannage

    docker compose ps
    docker compose logs rabbitmq
    curl http://localhost:5000/health/ready
    python backup.py /tmp/cocomarket-backup --verify

Pour une DLQ, ouvrir Messages en échec, analyser la cause, corriger le
catalogue ou le siège, puis utiliser Rejouer.

## Sécurité de production

- définir `COCO_ENV=production`, `COCO_SECRET`, `DATABASE_URL` et une
  `RABBIT_URL` qui n'utilise pas `guest/guest` ;
- définir un `COCO_BOOTSTRAP_ADMIN_PASSWORD` d'au moins 12 caractères ;
- conserver `COCO_SEED_DEMO_USERS=0` ; seul l'administrateur bootstrap est
  créé automatiquement ;
- activer HTTPS et `COCO_HTTPS=1` derrière le reverse proxy ;
- remplacer les secrets d'exemple et limiter l'accès aux ports RabbitMQ,
  PostgreSQL et Prometheus ;
- sauvegarder régulièrement et tester la restauration, pas uniquement la
  création du fichier de backup.

## Garanties actuelles

- écriture locale avant publication ;
- files RabbitMQ persistantes ;
- publisher confirms ;
- ACK après transaction siège ;
- idempotence ;
- retries et DLQ ;
- reprise de l'outbox après coupure ;
- sauvegardes SQLite ou PostgreSQL vérifiables ;
- validation des magasins, caisses, prix, quantités et tailles de messages ;
- comptes de démonstration désactivés par défaut en production.

## Limites restantes et choix d'exploitation

- PostgreSQL est recommandé en production, mais reste optionnel pour le mode
  local ;
- la composition de démonstration monte un volume SQLite partagé : pour un
  déploiement multi-caisses strictement isolé, utiliser un volume ou un
  service producteur par caisse ;
- pas encore de cluster RabbitMQ ;
- la DLQ nécessite encore une analyse ou un rejeu humain ;
- les tests de chaos et une restauration complète sur infrastructure réelle
  doivent être planifiés ;
- coupure réseau encore simulée par l'interface.

## Structure

    app.py                 interface Flask et API
    caisse.py              outbox et publication
    caisse_service.py      agent caisse indépendant
    consumer.py            traitement métier siège
    consumer_service.py    consommateur indépendant
    db.py                  schémas et accès SQLite
    broker/                RabbitMQ et broker local
    backup.py              sauvegarde et intégrité
    templates/             interfaces HTML
    static/                CSS, JavaScript, graphiques
    tests/                 tests fonctionnels
    .github/workflows/ci.yml CI Python, PostgreSQL et Compose
    Dockerfile             image des services
    docker-compose.services.yml  déploiement séparé
