# EdgeLite Gateway — common development and operations commands
# Usage: make <target>

PYTHON ?= python
PORT ?= 8080

.PHONY: help install dev-install test test-cov lint format typecheck build docker docker-up docker-down k8s-apply helm-install clean lockfile

help: ## Show this help
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) | sort | \
		awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-15s\033[0m %s\n", $$1, $$2}'

install: ## Install package (production deps only)
	$(PYTHON) -m pip install -e .

dev-install: ## Install package with dev dependencies
	$(PYTHON) -m pip install -e ".[dev]"

test: ## Run test suite
	$(PYTHON) -m pytest tests/ --asyncio-mode=auto -q --timeout=30 -p no:warnings

test-cov: ## Run tests with coverage report
	$(PYTHON) -m pytest tests/ --asyncio-mode=auto --cov=edgelite --cov-report=term-missing --cov-report=html --timeout=30 -p no:warnings

lint: ## Run ruff linter
	$(PYTHON) -m ruff check src/ tests/

format: ## Format code with ruff
	$(PYTHON) -m ruff format src/ tests/
	$(PYTHON) -m ruff check --fix src/ tests/

typecheck: ## Run pyright type checker
	$(PYTHON) -m pyright

build: ## Build sdist + wheel
	$(PYTHON) -m build

docker: ## Build Docker image
	docker build -f docker/Dockerfile -t edgelite:latest .

docker-up: ## Start full stack with Docker Compose (includes monitoring)
	docker compose -f docker/docker-compose.yml --profile monitoring up -d

docker-down: ## Stop Docker Compose stack
	docker compose -f docker/docker-compose.yml --profile monitoring down

k8s-apply: ## Deploy to Kubernetes
	kubectl apply -f k8s/

helm-install: ## Install Helm chart
	helm install edgelite helm/edgelite/

clean: ## Clean build artifacts
	$(PYTHON) -m pip cache purge 2>/dev/null || true
	rm -rf build/ dist/ *.egg-info src/*.egg-info .pytest_cache .ruff_cache .mypy_cache htmlcov coverage.xml
	find . -type d -name __pycache__ -exec rm -rf {} + 2>/dev/null || true

lockfile: ## Generate requirements.lock with hashes (供应链安全)
	@echo "生成依赖锁定文件..."
	@if command -v pip-compile >/dev/null 2>&1; then \
		pip-compile --generate-hashes --output-file=requirements.lock requirements.txt; \
		pip-compile --generate-hashes --output-file=requirements-dev.lock requirements.txt --extra dev; \
		echo "✅ 锁定文件生成完成: requirements.lock, requirements-dev.lock"; \
	else \
		echo "❌ pip-compile 未安装，请运行: pip install pip-tools"; \
		exit 1; \
	fi
