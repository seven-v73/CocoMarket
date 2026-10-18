.PHONY: install check test test-postgres up down logs backup verify

install:
	python -m pip install -r requirements.txt

check:
	python -m py_compile app.py db.py caisse.py caisse_service.py caisse_simulator.py consumer.py consumer_service.py backup.py migrate_sqlite_to_postgres.py broker/*.py
	COCO_BROKER=local python -m pytest tests -q
	@echo "Contrôles locaux terminés"

test:
	COCO_BROKER=local python -m pytest tests -q

test-postgres:
	DATABASE_URL=$${DATABASE_URL:?Définir DATABASE_URL} python -m pytest tests/test_postgres_backend.py -q

up:
	docker compose -f docker-compose.services.yml up -d --build

down:
	docker compose -f docker-compose.services.yml down

logs:
	docker compose -f docker-compose.services.yml logs -f --tail=100

backup:
	python backup.py $${BACKUP_DIR:-./backups}

verify:
	python backup.py $${BACKUP_DIR:-./backups} --verify
