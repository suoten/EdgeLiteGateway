# EdgeLite Gateway Makefile
# Common development and operations commands

.PHONY: help install dev test lint format typecheck smoke-test acceptance docker-build docker-up docker-down

help: ## Show this help
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) | sort | awk 'BEGIN {FS = ":.*?## "}; {printf "\033[36m%-20s\033[0m %s\n", $$1, $$2}'

install: ## Install package in development mode
	pip install -e ".[dev]"

dev: ## Start development server
	DEV_MODE=true python -m edgelite

test: ## Run all tests with coverage
	pytest --cov=edgelite --cov-report=term-missing --cov-fail-under=80 --timeout=60 -q

smoke-test: ## Run smoke tests
	pytest tests/test_smoke.py -v -m smoke --junitxml=smoke_test_results.xml --timeout=30

acceptance: ## Run acceptance gate (API + frontend route checks)
	DEV_MODE=true python scripts/acceptance_check.py --base-url http://127.0.0.1:8080 --frontend-url http://127.0.0.1:5173

lint: ## Run ruff linter
	ruff check .

format: ## Format code with ruff
	ruff format .
	ruff check --fix .

typecheck: ## Run pyright type checker
	pyright

docker-build: ## Build Docker image
	docker build -f docker/Dockerfile -t edgelite:latest .

docker-up: ## Start all services with Docker Compose
	docker compose --profile monitoring up -d

docker-down: ## Stop all Docker Compose services
	docker compose --profile monitoring down
