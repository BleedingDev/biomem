# biomem memory engine — local cross-platform build helpers
PY ?= python3

.PHONY: install test wheel build-linux build-windows clean

install:
	$(PY) -m pip install -e src[all]

test:
	$(PY) tests/test_smoke.py

wheel:
	$(PY) -m build src --wheel --outdir dist

build-linux:  # cross-build Linux wheel via docker (amd64)
	docker run --rm --platform linux/amd64 -v $$PWD:/w -w /w python:3.11-slim \
	  sh -c "pip install build && python -m build src --wheel --outdir dist"

build-windows: # the project wheel is pure Python and platform-independent
	$(PY) -m build src --wheel --outdir dist

clean:
	rm -rf dist build *.egg-info
	find src tests -name __pycache__ -type d -exec rm -rf {} + 2>/dev/null || true
