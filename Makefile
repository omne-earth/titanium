.ONESHELL:
.SHELLFLAGS := -euo pipefail -c
.PHONY: .uv .tmux .deps .podman .docker .runsc .runsc-podman .titanium init unit-podman-env unit-podman unit-all pier-run smoke-podman smoke-gvisor smoke-gvisor-podman smoke-env bench-ds bench-tb2 bench-all run-session run-attach run-list run-close sync upgrade FORCE images-vendor images-restore

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
PIER_MODEL ?= $(OPENROUTER_MODEL)
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
PIER_N ?= 1
# Trial execution runs as the dedicated runner user whenever that user has
# been provisioned (scripts/init/titanium.sh) — secure by default, opt-out
# with RUNNER= (empty). Unprovisioned hosts run as the invoking user.
RUNNER ?= $(shell test -f /usr/local/share/pier/titanium.provisioned && echo titanium)
PIER_RUN ?= $(if $(RUNNER),RUNNER=$(RUNNER) bash scripts/titanium-run.sh )$(PIER) run --agent=$(PIER_AGENT) --model $(PIER_MODEL) --env $(PIER_ENV) --path=$(PIER_TASK) --jobs-dir=$(PIER_JOBS_DIR) -n $(PIER_N)

RUN_DIR ?= ./.run
RUN_TASKS := $(RUN_DIR)/tasks
REPORTS_DIR := $(RUN_DIR)/reports

# fix-git-offline: air-gapped, build-pmars: needs egress
SMOKE_TASKS ?= examples/smoke/fix-git-offline $(TASKS_DIR)/terminal-bench-2/build-pmars
BENCH_N ?= 8

# run-session plumbing: one tmux session per RUN_DIR, one window per target
RUN_SESSION := pier-$(subst .,,$(notdir $(RUN_DIR)))
RUN_TMUX := tmux -L pier
SESSION_TARGETS ?= smoke-podman smoke-gvisor smoke-gvisor-podman

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
	bash scripts/doctor/podman.sh --bootstrap

# guards keep re-runs sudo-free; the scripts themselves are also idempotent.
.docker:
	@{ command -v docker && systemctl is-active -q docker; } >/dev/null 2>&1 \
		|| bash scripts/init/docker.sh

.runsc: .docker
	@{ command -v runsc && grep -qs '"runsc"' /etc/docker/daemon.json; } >/dev/null 2>&1 \
		|| bash scripts/init/runsc.sh

# no daemon registration for podman: the binary at a default search path is
# the whole requirement, and the script probes resolution through podman itself.
.runsc-podman: .podman
	@{ command -v runsc; } >/dev/null 2>&1 \
		|| bash scripts/init/runsc-podman.sh

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

# provisioning the runner user makes RUNNER=titanium the default from here on.
# stamp-guarded, not user-guarded: a partially provisioned host must re-run
# the (idempotent) script, and the stamp is only written after its probe.
.titanium: .podman
	@test -f /usr/local/share/pier/titanium.provisioned || bash scripts/init/titanium.sh

init: sync .tmux .podman .runsc .runsc-podman .titanium | .sentinel/tasks
	@bash scripts/init/docker-group.sh

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
	$(MAKE) pier-run PIER_ENV=gvisor PIER_TASK=$(RUN_TASKS)/$(BACKEND)/$@ PIER_JOBS_DIR=$(PIER_JOBS_DIR)/$(BACKEND)/$@

smoke-gvisor-podman: sync .runsc-podman $(RUN_TASKS)/$(BACKEND)/smoke-gvisor-podman
	mkdir -p "$(REPORTS_DIR)/$(BACKEND)/$@"
	COVERAGE_FILE=$(REPORTS_DIR)/$(BACKEND)/$@/.coverage $(PYTEST) \
		tests/test_gvisor_podman_environment.py tests/test_environment_factory.py \
		--html=$(REPORTS_DIR)/$(BACKEND)/$@/unit.html \
		--self-contained-html --cov=pier.environments.gvisor \
		--cov-report=html:$(REPORTS_DIR)/$(BACKEND)/$@/coverage
	$(MAKE) pier-run PIER_ENV=gvisor-podman PIER_TASK=$(RUN_TASKS)/$(BACKEND)/$@ PIER_JOBS_DIR=$(PIER_JOBS_DIR)/$(BACKEND)/$@

# full-dataset benchmarks (default env podman; run `make init` to provision).
# BENCH_N concurrent trials each — bench-all fans out two, so 2*BENCH_N total.
bench-ds: sync | .sentinel/tasks
	$(MAKE) pier-run PIER_TASK=$(TASKS_PATH_DS)/tasks PIER_JOBS_DIR=$(PIER_JOBS_DIR)/$(BACKEND)/$@ PIER_N=$(BENCH_N)

bench-tb2: sync | .sentinel/tasks
	$(MAKE) pier-run PIER_TASK=$(TASKS_PATH_TB2) PIER_JOBS_DIR=$(PIER_JOBS_DIR)/$(BACKEND)/$@ PIER_N=$(BENCH_N)

# fan out $(SESSION_TARGETS), one tmux window each, in the RUN_DIR-scoped session
run-session: sync .tmux | .sentinel/tasks
	@if $(RUN_TMUX) has-session -t $(RUN_SESSION) 2>/dev/null; then
		# a pane's shell has children iff its target is still running
		if $(RUN_TMUX) list-panes -s -t $(RUN_SESSION) -F '#{pane_pid}' | xargs -I{} ps -o pid= --ppid {} | grep -q .; then
			echo "run still in progress in tmux session '$(RUN_SESSION)'"
			echo "  attach: make run-attach RUN_DIR=$(RUN_DIR)"
			echo "  kill:   $(RUN_TMUX) kill-session -t $(RUN_SESSION)"
			exit 1
		fi
		echo "previous run finished — recycling session '$(RUN_SESSION)'"
		$(RUN_TMUX) kill-session -t $(RUN_SESSION)
	fi
	first=1
	for t in $(SESSION_TARGETS); do
		if [ "$$first" = 1 ]; then
			$(RUN_TMUX) new-session -d -s $(RUN_SESSION) -n "$$t" -e MAKEFLAGS='$(MAKEFLAGS)' "$(MAKE) $$t; exec bash"
			first=0
		else
			$(RUN_TMUX) new-window -t $(RUN_SESSION) -n "$$t" -e MAKEFLAGS='$(MAKEFLAGS)' "$(MAKE) $$t; exec bash"
		fi
	done
	echo ""
	echo "Started [$(SESSION_TARGETS)] in tmux session '$(RUN_SESSION)':"
	echo "  attach:  make run-attach RUN_DIR=$(RUN_DIR)"
	echo "  windows: Ctrl-b n / Ctrl-b p to cycle, Ctrl-b w to list"
	echo "  detach:  Ctrl-b d (runs keep going)"

smoke-env: SESSION_TARGETS = smoke-podman smoke-gvisor smoke-gvisor-podman
smoke-env: run-session

bench-all: SESSION_TARGETS = bench-ds bench-tb2
bench-all: run-session

run-attach: .tmux
	@$(RUN_TMUX) has-session -t $(RUN_SESSION) 2>/dev/null || { echo "no session '$(RUN_SESSION)' — run: make smoke-env or make bench-all"; exit 1; }
	$(RUN_TMUX) attach -t $(RUN_SESSION)

run-list: .tmux
	@$(RUN_TMUX) list-sessions 2>/dev/null || echo "no runs active"

run-close: .tmux
	@$(RUN_TMUX) kill-session -t $(RUN_SESSION) 2>/dev/null && echo "closed '$(RUN_SESSION)'" || echo "no session '$(RUN_SESSION)'"

# Vendor every image a task set references into one archive; restore it on an
# airgapped host so nothing is ever pulled. --prebuilt matches
# PIER_IMAGE_SOURCE=prebuilt deployments.
IMAGES_TASKS ?= examples/smoke
IMAGES_ARCHIVE ?= $(RUN_DIR)/images.tar
images-vendor: sync .podman
	bash scripts/images/vendor.sh $(IMAGES_TASKS) $(IMAGES_ARCHIVE) $(IMAGES_VENDOR_ARGS)

images-restore: .podman
	bash scripts/images/restore.sh $(IMAGES_ARCHIVE)

sync: .deps .uv
	$(UV) sync

upgrade: .uv
	$(UV) lock --upgrade
 