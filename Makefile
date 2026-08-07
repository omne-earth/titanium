.ONESHELL:
.SHELLFLAGS := -euo pipefail -c
.PHONY: .uv unit-podman-env unit-podman unit-all pier-run smoke-podman smoke-gvisor smoke-env sync upgrade

-include .secrets

MAKE = $(shell command -v make)
# Recursive on purpose: re-resolved after the .uv target installs uv. The
# curl installer lands in ~/.local/bin, which may not be on PATH yet.
UV = $(shell command -v uv || echo $(HOME)/.local/bin/uv)

PY := .venv/bin/python
PYTEST := .venv/bin/pytest
PIER := .venv/bin/pier
BACKEND ?= openrouter

TASKS_SOURCE_DS := "https://github.com/datacurve-ai/deep-swe.git"
TASKS_SOURCE_TB2 := "https://github.com/harbor-framework/terminal-bench-2.git"
TASKS_DIR := ./.tasks
TASKS_PATH_DS := "$(TASKS_DIR)/deep-swe"
TASKS_PATH_TB2 := "$(TASKS_DIR)/terminal-bench-2"
TASKS_DEFAULT := $(TASKS_PATH_DS)/tasks/anko-default-function-arguments

REPORTS_DIR ?= ./.reports

ifeq ($(BACKEND),openrouter)
PIER_AGENT ?= mini-swe-agent
PIER_MODEL ?= openrouter/deepseek/deepseek-v4-flash-0731
export PIER_API_BASE = https://openrouter.ai
else ifeq ($(BACKEND),claude)
PIER_AGENT ?= claude-code
PIER_MODEL ?= opus
else
$(error BACKEND must be 'openrouter' or 'claude', got '$(BACKEND)')
endif

PIER_JOBS_DIR ?= ./.jobs
PIER_ENV ?= podman
PIER_TASK ?= $(TASKS_DEFAULT)
PIER_RUN ?= $(PIER) run --agent=$(PIER_AGENT) --model $(PIER_MODEL) --env $(PIER_ENV) --path=$(PIER_TASK) --jobs-dir=$(PIER_JOBS_DIR)

.uv:
	@command -v uv >/dev/null || { \
		if command -v apt-get >/dev/null; then sudo apt-get update && sudo apt-get install -y uv; \
		elif command -v dnf >/dev/null; then sudo dnf install -y uv; \
		else curl -LsSf https://astral.sh/uv/install.sh | sh; fi; }

$(TASKS_PATH_DS):
	@test -d $(TASKS_PATH_DS) || git clone $(TASKS_SOURCE_DS) $(TASKS_PATH_DS)

$(TASKS_PATH_TB2):
	@test -d $(TASKS_PATH_TB2) || git clone $(TASKS_SOURCE_TB2) $(TASKS_PATH_TB2)

.sentinel/tasks: $(TASKS_PATH_DS) $(TASKS_PATH_TB2)

unit-podman-env:
	bash scripts/podman-doctor.sh
	$(PYTEST) tests/test_podman_environment.py

unit-podman: unit-podman-env

unit-all: unit-podman

pier-run: | .sentinel/tasks
	mkdir -p "$(PIER_JOBS_DIR)"
	$(PIER_RUN)

smoke-podman: sync
	bash scripts/podman-doctor.sh --fix
	mkdir -p "$(REPORTS_DIR)/$(BACKEND)/$@"
	COVERAGE_FILE=$(REPORTS_DIR)/$(BACKEND)/$@/.coverage $(PYTEST) tests/test_podman_environment.py --html=$(REPORTS_DIR)/$(BACKEND)/$@/unit.html \
		--self-contained-html --cov=pier.environments.podman --cov-report=html:$(REPORTS_DIR)/$(BACKEND)/$@/coverage
	$(MAKE) pier-run PIER_ENV=podman PIER_TASK=$(TASKS_PATH_TB2)/fix-git PIER_JOBS_DIR=$(PIER_JOBS_DIR)/$(BACKEND)/$@

smoke-gvisor: sync
	mkdir -p "$(REPORTS_DIR)/$(BACKEND)/$@"
	COVERAGE_FILE=$(REPORTS_DIR)/$(BACKEND)/$@/.coverage $(PYTEST) tests/test_gvisor_environment.py tests/test_gvisor_network_policy.py --html=$(REPORTS_DIR)/$(BACKEND)/$@/unit.html \
		--self-contained-html --cov=pier.environments.gvisor --cov-report=html:$(REPORTS_DIR)/$(BACKEND)/$@/coverage
	$(MAKE) pier-run PIER_ENV=gvisor PIER_TASK=$(TASKS_PATH_TB2)/fix-git PIER_JOBS_DIR=$(PIER_JOBS_DIR)/$(BACKEND)/$@

# Sync the venv and clone the task repos up front so the parallel fan-out
# starts from a ready checkout instead of racing to build it.
smoke-env: sync | .sentinel/tasks
	$(MAKE) -j2 smoke-podman smoke-gvisor

sync: .uv
	$(UV) sync

upgrade: .uv
	$(UV) lock --upgrade
 