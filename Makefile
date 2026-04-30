.PHONY: help install dev lint test build run clean format

help:
	@echo "FairFare Notifier Service"
	@echo ""
	@echo "Available commands:"
	@echo "  make install          - Install dependencies"
	@echo "  make dev              - Run in development mode"
	@echo "  make lint             - Run code linting"
	@echo "  make test             - Run pytest"
	@echo "  make coverage         - Run tests with coverage"
	@echo "  make integration-test - Run integration tests"
	@echo "  make build            - Build Docker image"
	@echo "  make run              - Run in production mode"
	@echo "  make clean            - Clean build artifacts"

install:
	pip install -e .
	pip install -e ".[dev]"

dev:
	ENVIRONMENT=dev uvicorn main:app --app-dir src --host 0.0.0.0 --port 8000 --reload

lint:
	ruff check src tests
	ruff format --check src tests

format:
	ruff format src tests

test:
	pytest tests/unit -v

integration-test:
	pytest tests/integration -v

coverage:
	pytest tests/ --cov=src --cov-report=html --cov-report=term
	@echo "Coverage report generated in htmlcov/index.html"

build:
	docker build -t fairfare/notifier:latest .

run:
	ENVIRONMENT=prod python -m uvicorn main:app --app-dir src --host 0.0.0.0 --port 8000

clean:
	rm -rf build dist .pytest_cache .coverage htmlcov
