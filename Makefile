.PHONY: host-env host-worker help frontend frontend-test frontend-build install dev test test-fast test-integration e2e verify-deploy run worker docker-up docker-down clean

help:
	@echo "Meeting Intelligence Agent - Make commands"
	@echo ""
	@echo "frontend    - Start the web app (http://localhost:5173)"
	@echo "frontend-test - Frontend unit/component tests"
	@echo "frontend-build - Production build of the web app"
	@echo "install     - Install production dependencies"
	@echo "dev         - Install development dependencies"
	@echo "test        - Run the full suite in the api container (coverage gate 80%)"
	@echo "test-fast   - Unit/API tests only (no real services needed)"
	@echo "test-integration - Real PostgreSQL / Ollama / Whisper tests"
	@echo "e2e         - End-to-end workflow against E2E_BASE_URL"
	@echo "verify-deploy - Build + run a disposable production stack and test it"
	@echo "run         - Run API server"
	@echo "worker      - Run Celery worker"
	@echo "docker-up   - Start Docker containers"
	@echo "docker-down - Stop Docker containers"
	@echo "clean       - Clean cache and build files"

frontend:
	cd frontend && npm install && npm run dev

frontend-test:
	cd frontend && npm test

frontend-build:
	cd frontend && npm run build

install:
	pip install -r requirements.txt

dev:
	pip install -r requirements.txt
	pip install pytest pytest-asyncio pytest-cov black flake8

test:
	docker compose exec -T api python -m pytest tests -p no:cacheprovider

test-fast:
	docker compose exec -T api python -m pytest tests -m "not integration and not e2e" -p no:cacheprovider --no-cov

test-integration:
	docker compose exec -T api python -m pytest tests -m integration -p no:cacheprovider --no-cov

e2e:
	@test -n "$(E2E_BASE_URL)" || (echo "Set E2E_BASE_URL, e.g. make e2e E2E_BASE_URL=http://host.docker.internal:8000"; exit 1)
	docker compose exec -T -e E2E_BASE_URL=$(E2E_BASE_URL) api python -m pytest tests/e2e -v -p no:cacheprovider --no-cov

verify-deploy:
	scripts/verify_deploy.sh

run:
	uvicorn src.api.main:app --reload --host 0.0.0.0 --port 8000

worker:
	celery -A src.workers.tasks worker --loglevel=info

docker-up:
	docker-compose up -d

docker-down:
	docker-compose down

clean:
	find . -type d -name "__pycache__" -exec rm -rf {} +
	find . -type f -name "*.pyc" -delete
	find . -type d -name ".pytest_cache" -exec rm -rf {} +
	rm -rf htmlcov/ .coverage

# Worker on this machine, so speaker analysis can use the Mac GPU (Docker cannot).
host-env:
	uv venv --python 3.11 .venv-host
	VIRTUAL_ENV=$(CURDIR)/.venv-host uv pip install -r requirements.txt "setuptools<81" "kombu==5.6.2" "amqp==5.4.1"
	VIRTUAL_ENV=$(CURDIR)/.venv-host uv pip install --no-build-isolation "openai-whisper==20231117"

host-worker:
	docker compose stop celery-worker
	.venv-host/bin/python scripts/host_worker.py
