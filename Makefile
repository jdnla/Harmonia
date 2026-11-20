# avr-tv-bridge — developer / deploy convenience targets
#
#   make deploy   Pull latest from git, sync code into the installed location,
#                 refresh Python deps if requirements changed, restart service.
#   make install  Run the full idempotent installer (first-time setup).
#   make restart  Restart the running bridge service.
#   make status   Show service status.
#   make logs     Follow the service journal.
#   make test     Syntax-check all Python modules + the install script, then run
#                 the unit tests in tests/ (stdlib unittest, no deps needed).
#
# `deploy` is safe to run repeatedly. It updates the deployed service in
# /opt/avr-tv-bridge from whatever is committed on the current branch's remote.

INSTALL_DIR := /opt/avr-tv-bridge
SERVICE     := avr-tv-bridge.service
VENV        := $(INSTALL_DIR)/venv
PYTHON      := $(VENV)/bin/python
PIP         := $(VENV)/bin/pip

.PHONY: deploy install restart status logs test unit rebuild-venv _sync _deps

rebuild-venv: ## Recreate the venv (needed after an OS Python-version upgrade)
	@echo "==> Rebuilding venv at $(VENV) (fixes ModuleNotFoundError after a"
	@echo "    Python major-version upgrade, e.g. an Ubuntu release upgrade)"
	rm -rf $(VENV)
	python3 -m venv $(VENV)
	$(PIP) -q install --upgrade pip
	$(PIP) -q install -r requirements.txt
	@echo "==> Restarting services that use the venv"
	-sudo systemctl restart $(SERVICE)
	-systemctl --user restart avr-tv-bridge-launcher.service avr-tv-bridge-cecnav.service
	@echo "==> Done. venv python: $$($(PYTHON) --version)"

deploy: ## Pull latest and update the running service
	@echo "==> Pulling latest from git"
	git pull --ff-only
	@$(MAKE) --no-print-directory _sync
	@$(MAKE) --no-print-directory _deps
	@echo "==> Installing harmonia wrapper"
	sudo install -m 755 setup/harmonia /usr/local/bin/harmonia
	@echo "==> Restarting $(SERVICE)"
	sudo systemctl restart $(SERVICE)
	@sleep 2
	@systemctl is-active --quiet $(SERVICE) \
		&& echo "==> Deploy OK: $(SERVICE) is active" \
		|| { echo "!! $(SERVICE) is not active — check: journalctl -u $(SERVICE) -n 30"; exit 1; }

_sync: ## Copy source into the installed location
	@echo "==> Syncing src/ and setup/ into $(INSTALL_DIR)"
	@# Use rsync --delete so removed/renamed brand modules do not linger in
	@# /opt and get auto-loaded alongside their replacements.
	mkdir -p $(INSTALL_DIR)/src $(INSTALL_DIR)/setup
	rsync -a --delete --exclude '__pycache__' src/ $(INSTALL_DIR)/src/
	rsync -a --delete --exclude '__pycache__' setup/ $(INSTALL_DIR)/setup/

_deps: ## Update venv deps if requirements.txt changed
	@echo "==> Ensuring Python deps are current"
	$(PIP) -q install -r requirements.txt

install: ## First-time full install (packages, services, CEC, IR)
	sudo ./install.sh

restart: ## Restart the bridge service
	sudo systemctl restart $(SERVICE)

status: ## Show service status
	systemctl status $(SERVICE) --no-pager

logs: ## Follow the service log
	journalctl -u $(SERVICE) -f

test: ## Syntax-check Python + install.sh, then run the unit tests
	@ok=1; \
	for f in src/*.py setup/*.py; do \
		python3 -m py_compile "$$f" && echo "  OK   $$f" || { echo "  FAIL $$f"; ok=0; }; \
	done; \
	bash -n install.sh && echo "  OK   install.sh" || { echo "  FAIL install.sh"; ok=0; }; \
	[ $$ok -eq 1 ] && echo "All checks passed" || { echo "Some checks failed"; exit 1; }
	@$(MAKE) --no-print-directory unit

unit: ## Run the unit tests (stdlib unittest; no runtime deps needed)
	@echo "==> Unit tests"
	@python3 -m unittest discover -s tests -t tests
