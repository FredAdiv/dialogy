SHELL := /bin/bash
python_version = 3.8.5

test: lint ## Run the tests.
	@echo -e "🦎🐊🐢🐸"
	@pytest --cov=dialogy --cov-report html --cov-report term:skip-covered tests/
	@echo -e "The tests pass! ✨ 🍰 ✨"

lint:
	@echo -e "🐟🐳🐠🦐"
	@echo -e "Running linter 📃"
	@black dialogy
	@echo -e "🐡🐲🦑🐙"
	@echo -e "Running type checker 🔍"
	@mypy dialogy

all: test
	@echo -e "🦆🦕🐬🐶"
	@pycco ./**/*.py -p
	@pycco tests/**/*.py -p
	@echo "🌟🌟🌟"
