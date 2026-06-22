.PHONY: install test lint format run

# Runtime deps plus the two extras the test suite needs (sqlite driver + redis client).
install:
	pip install -r requirements.txt
	pip install aiosqlite redis ruff black

test:
	python -m pytest -q

lint:
	ruff check app tests

format:
	ruff format app tests
	black app tests

run:
	python -m app.main --port 8002
