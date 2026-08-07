.ONESHELL:
.SHELLFLAGS := -euo pipefail -c
.PHONY: .uv .tmux .deps .podman .docker .runsc init unit-podman-env unit-podman unit-all pier-run smoke-podman smoke-gvisor smoke-env smoke-attach sync upgrade FORCE

-include .secrets

MAKE = $(shell command -v make)
# Recursive = so it re-resolves after .uv installs uv (~/.local/bin may be off PATH)
UV = $(shell command -v uv || echo $(HOME)/.local/bin/uv)

PY := .venv/bin/python
PYTEST := .venv/bin/pytest
PIER := .venv/bin/pier
BACKEND ?= openrouter

TASKS_SOURCE_DS := https://github.com/datacurve-ai/deep-swe.git
TASKS_SOURCE_TB2 := https://github.com/harbor-framework/terminal-bench-2.git
TASKS_DIR := ./.tasks
TASKS_PATH_DS := $(TASKS_DIR)/deep-swe
TASKS_PATH_TB2 := $(TASKS_DIR)/terminal-bench-2
TASKS_DEFAULT := $(TASKS_PATH_DS)/tasks/anko-default-function-arguments

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

PIER_JOBS_DIR ?= $(RUN_DIR)/jobs
PIER_ENV ?= podman
PIER_TASK ?= $(TASKS_DEFAULT)
PIER_RUN ?= $(PIER) run --agent=$(PIER_AGENT) --model $(PIER_MODEL) --env $(PIER_ENV) --path=$(PIER_TASK) --jobs-dir=$(PIER_JOBS_DIR)

RUN_DIR ?= ./.run
RUN_TASKS := $(RUN_DIR)/tasks
REPORTS_DIR := $(RUN_DIR)/reports

# fix-git-offline: air-gapped, build-pmars: needs egress
SMOKE_TASKS ?= examples/smoke/fix-git-offline $(TASKS_DIR)/terminal-bench-2/build-pmars
SMOKE_SESSION := pier-$(subst .,,$(notdir $(RUN_DIR)))
SMOKE_TMUX := tmux -L pier

.uv:
	@command -v uv >/dev/null || { \
		if command -v apt-get >/dev/null; then sudo apt-get update && sudo apt-get install -y uv; \
		elif command -v dnf >/dev/null; then sudo dnf install -y uv; \
		else curl -LsSf https://astral.sh/uv/install.sh | sh; fi; }

.tmux:
	@command -v tmux >/dev/null || { \
		if command -v apt-get >/dev/null; then sudo apt-get update && sudo apt-get install -y tmux; \
		elif command -v dnf >/dev/null; then sudo dnf install -y tmux; \
		else echo "no apt-get/dnf found — install tmux manually"; exit 1; fi; }

# needs the venv — keep after `sync` in prerequisite lists
.podman:
	bash scripts/doctor/podman.sh --fix

# guards keep re-runs sudo-free; the scripts themselves are also idempotent.
.docker:
	@{ command -v docker && systemctl is-active -q docker; } >/dev/null 2>&1 \
		|| bash scripts/init/docker.sh

.runsc: .docker
	@{ command -v runsc && grep -qs '"runsc"' /etc/docker/daemon.json; } >/dev/null 2>&1 \
		|| bash scripts/init/runsc.sh

# toolchain for building wheels that ship no binary for this platform/python.
.deps:
	@{ command -v gcc && command -v make && command -v python3 && \
		test -e "$$(python3 -c 'import sysconfig; print(sysconfig.get_path("include"))')/Python.h"; } >/dev/null 2>&1 || { \
			if command -v apt-get >/dev/null; then sudo apt-get update && sudo apt-get install -y gcc make python3 python3-dev; \
			elif command -v dnf >/dev/null; then sudo dnf install -y gcc make python3 python3-devel; \
			else echo "no apt-get/dnf found — install gcc make python3 python3-devel manually"; exit 1; fi; }

$(TASKS_PATH_DS):
	@test -d $(TASKS_PATH_DS) || git clone $(TASKS_SOURCE_DS) $(TASKS_PATH_DS)

$(TASKS_PATH_TB2):
	@test -d $(TASKS_PATH_TB2) || git clone $(TASKS_SOURCE_TB2) $(TASKS_PATH_TB2)

.sentinel/tasks: $(TASKS_PATH_DS) $(TASKS_PATH_TB2)

# re-staged fresh every run; per-target dirs keep parallel smoke windows isolated
FORCE:
$(RUN_TASKS)/%: FORCE | .sentinel/tasks
	@rm -rf $@ && mkdir -p $@
	cp -r $(SMOKE_TASKS) $(wildcard examples/smoke/$(patsubst smoke-%,verify-%-env,$(notdir $@))) $@/

init: sync .tmux .podman .runsc | .sentinel/tasks
	@echo ""
	echo "init complete — try: make smoke-env BACKEND=openrouter"

unit-podman-env: .podman
	$(PYTEST) tests/test_podman_environment.py

unit-podman: unit-podman-env

unit-all: unit-podman

pier-run: | .sentinel/tasks
	mkdir -p "$(PIER_JOBS_DIR)"
	$(PIER_RUN)

smoke-podman: sync .podman $(RUN_TASKS)/$(BACKEND)/smoke-podman
	mkdir -p "$(REPORTS_DIR)/$(BACKEND)/$@"
	COVERAGE_FILE=$(REPORTS_DIR)/$(BACKEND)/$@/.coverage $(PYTEST) \
		tests/test_podman_environment.py \
		--html=$(REPORTS_DIR)/$(BACKEND)/$@/unit.html \
		--self-contained-html --cov=pier.environments.podman \
		--cov-report=html:$(REPORTS_DIR)/$(BACKEND)/$@/coverage
	$(MAKE) pier-run PIER_ENV=podman PIER_TASK=$(RUN_TASKS)/$(BACKEND)/$@ PIER_JOBS_DIR=$(PIER_JOBS_DIR)/$(BACKEND)/$@

smoke-gvisor: sync .runsc $(RUN_TASKS)/$(BACKEND)/smoke-gvisor
	mkdir -p "$(REPORTS_DIR)/$(BACKEND)/$@"
	COVERAGE_FILE=$(REPORTS_DIR)/$(BACKEND)/$@/.coverage $(PYTEST) \
		tests/test_gvisor_environment.py tests/test_gvisor_network_policy.py \
		--html=$(REPORTS_DIR)/$(BACKEND)/$@/unit.html \
		--self-contained-html --cov=pier.environments.gvisor \
		--cov-report=html:$(REPORTS_DIR)/$(BACKEND)/$@/coverage
	sg docker -c "$(MAKE) pier-run PIER_ENV=gvisor PIER_TASK=$(RUN_TASKS)/$(BACKEND)/$@ PIER_JOBS_DIR=$(PIER_JOBS_DIR)/$(BACKEND)/$@"

smoke-env: sync .tmux | .sentinel/tasks
	@if $(SMOKE_TMUX) has-session -t $(SMOKE_SESSION) 2>/dev/null; then
		# a pane's shell has children iff its smoke is still running
		if $(SMOKE_TMUX) list-panes -s -t $(SMOKE_SESSION) -F '#{pane_pid}' | xargs -I{} ps -o pid= --ppid {} | grep -q .; then
			echo "smoke run still in progress in tmux session '$(SMOKE_SESSION)'"
			echo "  attach: $(SMOKE_TMUX) attach -t $(SMOKE_SESSION)"
			echo "  kill:   $(SMOKE_TMUX) kill-session -t $(SMOKE_SESSION)"
			exit 1
		fi
		echo "previous smoke run finished — recycling session '$(SMOKE_SESSION)'"
		$(SMOKE_TMUX) kill-session -t $(SMOKE_SESSION)
	fi
	$(SMOKE_TMUX) new-session -d -s $(SMOKE_SESSION) -n smoke-podman -e MAKEFLAGS='$(MAKEFLAGS)' \
		"$(MAKE) smoke-podman; exec bash"
	$(SMOKE_TMUX) new-window -t $(SMOKE_SESSION) -n smoke-gvisor -e MAKEFLAGS='$(MAKEFLAGS)' \
		"$(MAKE) smoke-gvisor; exec bash"
	echo ""
	echo "Smoke runs started in tmux session '$(SMOKE_SESSION)':"
	echo "  attach:  make smoke-attach"
	echo "  windows: Ctrl-b n / Ctrl-b p to cycle, Ctrl-b w to list"
	echo "  detach:  Ctrl-b d (runs keep going)"

smoke-attach: .tmux
	@$(SMOKE_TMUX) has-session -t $(SMOKE_SESSION) 2>/dev/null || { echo "no smoke session — run: make smoke-env"; exit 1; }
	$(SMOKE_TMUX) attach -t $(SMOKE_SESSION)

sync: .deps .uv
	$(UV) sync

upgrade: .uv
	$(UV) lock --upgrade
 