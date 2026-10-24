PY := backend/.venv/bin
SW := $(PY)/swarmos

.PHONY: install init train build run run-api dev-web worker test lint e2e clean-db

install:
	python3 -m venv backend/.venv
	$(PY)/pip install --upgrade pip
	$(PY)/pip install -e "backend[dev,ml]"
	npm --prefix frontend install

init:
	$(SW) init

train:
	$(SW) ml train-all --promote-if-better

build:
	npm --prefix frontend run build

run: build
	$(SW) serve --port 8000

run-api:
	$(SW) serve --port 8000

dev-web:
	npm --prefix frontend run dev

worker:
	SWARMOS_WORKER_EMBEDDED=false $(SW) worker

test:
	cd backend && .venv/bin/pytest -q

lint:
	cd backend && .venv/bin/ruff check swarmos tests && .venv/bin/mypy swarmos
	npm --prefix frontend run lint
	npm --prefix frontend run typecheck

e2e: build
	npm --prefix frontend run e2e

clean-db:
	@echo "This deletes var/swarmos.db (local development data)."; read -p "Type 'yes' to continue: " ans; [ "$$ans" = "yes" ] && rm -f var/swarmos.db var/swarmos.db-wal var/swarmos.db-shm
