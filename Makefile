.PHONY: help bootstrap build check clean format lint test test-unit test-integration typecheck

PYTHON ?= python3
VENV ?= .venv
BIN := $(VENV)/bin

help:
	@echo "bootstrap         Create a local virtual environment and install development tools"
	@echo "build             Build source and wheel distributions"
	@echo "check             Run formatting, lint, types, and all tests"
	@echo "format            Format source and tests"
	@echo "lint              Run static lint checks"
	@echo "typecheck         Run strict mypy checks"
	@echo "test              Run the complete test suite with coverage"
	@echo "test-unit         Run unit tests only"
	@echo "test-integration  Run integration tests only"
	@echo "clean             Remove generated files"

bootstrap:
	$(PYTHON) -m venv $(VENV)
	$(BIN)/python -m pip install --upgrade pip
	$(BIN)/python -m pip install -e '.[dev]'

build:
	$(BIN)/python -m build

format:
	$(BIN)/ruff format src tests
	$(BIN)/ruff check --fix src tests

lint:
	$(BIN)/ruff format --check src tests
	$(BIN)/ruff check src tests

typecheck:
	$(BIN)/mypy

test:
	$(BIN)/pytest --cov=forex_monitor --cov-report=term-missing

test-unit:
	$(BIN)/pytest tests/unit

test-integration:
	$(BIN)/pytest tests/integration

check: lint typecheck test build

clean:
	rm -rf build dist .coverage .mypy_cache .pytest_cache .ruff_cache htmlcov
	find src tests -type d -name __pycache__ -prune -exec rm -rf {} +

