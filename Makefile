UUID := $(shell sed -n 's/^EXTENSION_UUID="\(.*\)"/\1/p' project.env)
ZIP := dist/$(UUID).shell-extension.zip

.PHONY: all zip test check sync-ids install uninstall clean

all: zip

# extensions.gnome.org upload
zip: check
	@mkdir -p dist
	gnome-extensions pack --force --extra-source=ids.js --out-dir=dist extension
	@echo "Built $(ZIP)"

check:
	python3 tools/sync_ids.py --check
	glib-compile-schemas --strict --dry-run extension/schemas

sync-ids:
	python3 tools/sync_ids.py

test: check
	python3 -m pytest -q tests mapper/tests

install:
	./install.sh

uninstall:
	./uninstall.sh

clean:
	rm -rf dist extension/schemas/gschemas.compiled
	find . -name __pycache__ -prune -exec rm -rf {} +
