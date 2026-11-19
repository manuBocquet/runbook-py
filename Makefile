# runbook-py — tâches courantes. `make` ou `make help` liste les cibles.
PYTHON ?= python3
VERSION := $(shell $(PYTHON) -c "import sys; sys.path.insert(0, \"src\"); import runbook_py; print(runbook_py.__version__)")

.DEFAULT_GOAL := help
.PHONY: help version dev test schema check-schema build clean changelog-check bump-patch bump-minor bump-major bump-dry release

help: ## Liste des cibles
	@grep -E "^[a-z-]+:.*## " $(MAKEFILE_LIST) | awk -F ":.*## " '{printf "  \033[36m%-13s\033[0m %s\n", $$1, $$2}'

version: ## Version courante
	@echo $(VERSION)

dev: ## Installe les outils de développement dans le Python courant
	$(PYTHON) -m pip install --upgrade pytest jsonschema build twine bump-my-version

test: ## Tests (pytest)
	$(PYTHON) -m pytest -q

schema: ## Régénère schema/runbook-plan.schema.json
	$(PYTHON) scripts/gen_schema.py

check-schema: schema ## Échoue si le schéma publié n'est pas à jour
	git diff --exit-code -- schema/

build: clean check-schema test ## Construit sdist + wheel et vérifie les métadonnées
	$(PYTHON) -m build
	$(PYTHON) -m twine check --strict dist/*

clean: ## Supprime les fichiers générés
	rm -rf build dist src/*.egg-info .pytest_cache
	find . -name __pycache__ -type d -prune -exec rm -rf {} +

changelog-check: ## Échoue si rien n'est noté sous « ## Unreleased » dans CHANGELOG.md
	@awk '/^## Unreleased/{f=1;next} /^## /{f=0} f && /^- /{n++} END{exit n==0}' CHANGELOG.md \
	  || { echo "CHANGELOG.md : ajoute au moins une ligne « - ... » sous « ## Unreleased »"; exit 1; }

bump-dry: ## Montre ce que ferait un bump patch
	bump-my-version bump --dry-run -v patch

bump-patch: changelog-check test ## Version x.y.Z+1 : commit + tag v…
	bump-my-version bump patch

bump-minor: changelog-check test ## Version x.Y+1.0 : commit + tag v…
	bump-my-version bump minor

bump-major: changelog-check test ## Version X+1.0.0 : commit + tag v…
	bump-my-version bump major

release: ## Pousse commit + tag et crée la release GitHub (publie sur PyPI)
	git push --follow-tags
	gh release create v$(VERSION) --title "v$(VERSION)" \
	  --notes "$$(awk '/^## $(VERSION) /{f=1;next} /^## /{f=0} f' CHANGELOG.md)"
