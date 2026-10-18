# Runbook haute disponibilité CocoMarket

Ce document complète `docker-compose.services.yml`. La composition fournie
sert au développement et à la démonstration ; elle ne constitue pas à elle
seule un cluster haute disponibilité.

## RabbitMQ

Déployer au minimum trois nœuds RabbitMQ sur des hôtes ou zones distincts,
avec :

- le même `RABBITMQ_ERLANG_COOKIE` secret sur les trois nœuds ;
- des noms DNS stables et une résolution réciproque ;
- des volumes persistants indépendants ;
- les mêmes utilisateurs, permissions et paramètres TLS ;
- les files `cocomarket.v3.*` en quorum ;
- un équilibrage AMQP devant les nœuds, avec retrait automatique d’un nœud
  non sain.

Vérifications minimales :

    rabbitmq-diagnostics cluster_status
    rabbitmq-diagnostics check_running
    rabbitmq-diagnostics check_local_alarms

Tester ensuite l’arrêt d’un nœud pendant une vente et vérifier que :

1. la caisse conserve la vente si la publication échoue ;
2. la publication reprend après bascule ;
3. le siège ne comptabilise la vente qu’une seule fois ;
4. aucune vente ne reste sans trace dans l’outbox ou la DLQ.

## PostgreSQL

Utiliser un service PostgreSQL avec réplication et bascule gérée (par exemple
un opérateur PostgreSQL ou un service managé). Le primaire et le réplica
doivent avoir :

- sauvegardes PITR et rétention définie ;
- stockage persistant chiffré ;
- supervision de la réplication ;
- endpoint de connexion qui suit le primaire ;
- test périodique de restauration sur une base isolée.

Après bascule, vérifier :

    curl http://localhost:5000/health/ready
    python -m pytest tests/test_postgres_backend.py -q

Ne pas utiliser un simple volume partagé entre deux instances PostgreSQL.

## Isolation des caisses

Chaque caisse physique doit disposer de son propre `COCO_DATA`, par exemple :

    /var/lib/cocomarket/ABJ01-caisse-01
    /var/lib/cocomarket/ABJ01-caisse-02

Le répertoire doit être sur un disque local persistant, sauvegardé
indépendamment. Un agent `caisse_service.py` doit être associé à un seul
magasin et utiliser `COCO_STORE` correspondant.

Éviter de monter le même fichier `caisse.db` en écriture sur plusieurs postes
physiques. Le volume partagé de `docker-compose.services.yml` est réservé à
la démonstration.

## Tests de reprise obligatoires

Avant mise en production, exécuter au minimum :

- coupure réseau pendant une vente ;
- arrêt brutal de l’agent après écriture de l’outbox ;
- arrêt du broker pendant une publication ;
- arrêt du consommateur après réception ;
- panne PostgreSQL pendant une transaction ;
- rejeu d’un message DLQ ;
- restauration d’une sauvegarde SQLite et PostgreSQL ;
- comparaison du nombre et du montant des tickets entre caisses, broker et
  siège.

Le déploiement est acceptable seulement si le rapprochement CSV ne présente
aucun écart inexpliqué après chaque scénario.

## Critères d’exploitation

Surveiller en permanence :

- `cocomarket_outbox_pending_by_store` ;
- `cocomarket_outbox_attempts` ;
- `cocomarket_dlq_total` ;
- l’état `/health/ready` ;
- l’état du cluster RabbitMQ ;
- le retard et l’état de réplication PostgreSQL.

Toute alerte DLQ ou accumulation persistante d’outbox doit être traitée et
documentée avant clôture de l’incident.
