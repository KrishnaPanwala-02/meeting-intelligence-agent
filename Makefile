.PHONY: help install dev test run worker docker-up docker-down clean

help:
	@echo "Meeting Intelligence Agent - Make commands"
	@echo ""
	@echo "install     - Install production dependencies"
	@echo "dev         - Install development dependencies"
	@echo "test        - Run tests"
	@echo "run         - Run API server"
	@echo "worker      - Run Celery worker"
	@echo "docker-up   - Start Docker containers"
	@echo "docker-down - Stop Docker containers"
	@echo "clean       - Clean cache and build files"

install:
	pip install -r requirements.txt

dev:
	pip install -r requirements.txt
	pip install pytest pytest-asyncio pytest-cov black flake8

test:
	pytest tests/ --cov=src --cov-report=html --cov-report=term-missing

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
