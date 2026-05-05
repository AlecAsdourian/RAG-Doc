.PHONY: install lint fmt test

install:
	python -m venv venv
	./venv/bin/pip install --upgrade pip
	./venv/bin/pip install -r requirements.txt

lint:
	flake8 workers/
	mypy workers/

fmt:
	black workers/

test:
	pytest
