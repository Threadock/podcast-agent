.PHONY: install run test lint docker-build docker-run clean

PYTHON := .venv/bin/python
UVICORN := .venv/bin/uvicorn
PYTEST := .venv/bin/pytest

install:
	uv venv .venv --python 3.11
	uv pip install -r requirements.txt

run:
	$(UVICORN) app.main:app --host 127.0.0.1 --port 8765 --reload

test:
	$(PYTEST) tests/ -v

test-cov:
	$(PYTEST) tests/ --cov=app --cov-report=term-missing

docker-build:
	docker build -t podcast-agent .

docker-run:
	docker run -p 8765:8000 --env-file .env -v $(PWD)/data:/app/data -v $(PWD)/output:/app/output podcast-agent

clean:
	find . -type d -name __pycache__ -exec rm -rf {} + 2>/dev/null || true
	find . -type d -name .pytest_cache -exec rm -rf {} + 2>/dev/null || true
	rm -rf data/*.db data/*.db-journal output/tts_segments output/*.mp3 2>/dev/null || true