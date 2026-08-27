.ONESHELL:
.SHELLFLAGS := -euo pipefail -c
.PHONY: .uv .tmux .deps .podman .docker .runsc .runsc-podman .krun-podman _probe-krun-podman .titanium init unit-podman-env unit-krun-podman-env unit-podman unit-all titanium-run smoke-podman smoke-gvisor smoke-gvisor-podman smoke-krun-podman smoke-env bench-ds bench-tb2 bench-all run-session run-attach run-list run-close sync upgrade FORCE images-vendor images-restore collect reset clean doctor-libvirt bootstrap

-include .secrets

MAKE = $(shell command -v make)
# Recursive = so it re-resolves after .uv installs uv (~/.local/bin may be off PATH)
UV = $(shell command -v uv || echo $(HOME)/.local/bin/uv)

PY := .venv/bin/python
PYTEST := .venv/bin/pytest
TITANIUM := .venv/bin/titanium
BACKEND ?= openrouter

TASKS_SOURCE_DS := https://github.com/datacurve-ai/deep-swe.git
TASKS_SOURCE_TB2 := https://github.com/harbor-framework/terminal-bench-2.git
TASKS_DIR := ./.tasks
TASKS_PATH_DS := $(TASKS_DIR)/deep-swe
TASKS_PATH_TB2 := $(TASKS_DIR)/terminal-bench-2
TASKS_DEFAULT := $(TASKS_PATH_DS)/tasks/anko-default-function-arguments

ifeq ($(BACKEND),openrouter)
TITANIUM_AGENT ?= mini-swe-agent
TITANIUM_MODEL ?= $(OPENROUTER_MODEL)
export TITANIUM_API_BASE = https://openrouter.ai
else ifeq ($(BACKEND),claude)
TITANIUM_AGENT ?= claude-code
TITANIUM_MODEL ?= opus
else
$(error BACKEND must be 'openrouter' or 'claude', got '$(BACKEND)')
endif

TITANIUM_JOBS_DIR ?= $(RUN_DIR)/jobs
TITANIUM_ENV ?= gvisor-podman
TITANIUM_TASK ?= $(TASKS_DEFAULT)
TITANIUM_N ?= 1
# Trial execution runs as the dedicated runner user whenever that user has
# been provisioned (scripts/init/titanium.sh) — secure by default, opt-out
# with RUNNER= (empty). Unprovisioned hosts run as the invoking user.
RUNNER ?= $(shell test -f /usr/local/share/titanium/titanium.provisioned && echo titanium)
# Wrapping is scoped to the podman family: the docker-daemon environments
# need socket access, and the runner must never join the docker group — that
# group is root-equivalent and would nullify the separation.
RUNNER_ENVS := podman gvisor-podman krun-podman
TITANIUM_WRAP := $(if $(and $(RUNNER),$(filter $(TITANIUM_ENV),$(RUNNER_ENVS))),RUNNER=$(RUNNER) bash scripts/titanium-run.sh )
TITANIUM_RUN ?= $(TITANIUM_WRAP)$(TITANIUM) run --agent=$(TITANIUM_AGENT) --model $(TITANIUM_MODEL) --env $(TITANIUM_ENV) --path=$(TITANIUM_TASK) --jobs-dir=$(TITANIUM_JOBS_DIR) -n $(TITANIUM_N)

RUN_DIR ?= ./.run
RUN_TASKS := $(RUN_DIR)/tasks
REPORTS_DIR := $(RUN_DIR)/reports

# fix-git-offline: air-gapped, build-pmars: needs egress
SMOKE_TASKS ?= examples/smoke/fix-git-offline $(TASKS_DIR)/terminal-bench-2/build-pmars
BENCH_N ?= 8

# run-session plumbing: one tmux session per RUN_DIR, one window per target
RUN_SESSION := titanium-$(subst .,,$(notdir $(RUN_DIR)))
RUN_TMUX := tmux -L titanium
SESSION_TARGETS ?= smoke-podman smoke-gvisor smoke-gvisor-podman smoke-krun-podman

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

# hosting libvirt guests next to the Docker daemon titanium installs breaks
# their forwarding (Docker's FORWARD drop) and can lose their firewalld zone;
# report-only by default, ARGS=--fix repairs atomically (running guests are
# reattached across the network restart). Not part of init: only hypervisor
# hosts need it.
doctor-libvirt:
	@bash scripts/doctor/libvirt-docker.sh $(ARGS)

# guards keep re-runs sudo-free; the scripts themselves are also idempotent.
# The group check matters after `make reset`: packages and the service
# survive a reset, but the operator's docker-group grant does not.
.docker:
	@{ command -v docker && systemctl is-active -q docker \
		&& id -nG "$$USER" | grep -qw docker; } >/dev/null 2>&1 \
		|| bash scripts/init/docker.sh

.runsc: .docker
	@{ command -v runsc && grep -qs '"runsc"' /etc/docker/daemon.json; } >/dev/null 2>&1 \
		|| bash scripts/init/runsc.sh

# no daemon registration for podman: the binary at a default search path is
# the whole requirement, and the script probes resolution through podman itself.
.runsc-podman: .podman
	@{ command -v runsc && test -x /usr/local/bin/runsc-ignorecg \
		&& test -f /usr/local/share/titanium/runsc.sha3-512 \
		&& test -f /etc/containers/containers.conf.d/titanium-runsc.conf; } >/dev/null 2>&1 \
		|| bash scripts/init/runsc-podman.sh

# internal: evidence probes for the relaxation table in
# docs/environments/KRUN-PODMAN.md §5. Each run prints per-probe facts that
# convert that table's pending rows into decision records. Drives podman and
# the krun runtime directly — no trials, no runner shim.
_probe-krun-podman: .krun-podman
	@bash scripts/doctor/probe-krun-podman.sh

# krun (crun + libkrun): dnf-installed, digest-pinned, registered in the same
# root-gated drop-in directory as runsc. The script also checks /dev/kvm.
.krun-podman: .podman
	@{ command -v krun && test -f /usr/local/share/titanium/krun.sha3-512 \
		&& test -f /etc/containers/containers.conf.d/titanium-krun.conf; } >/dev/null 2>&1 \
		|| bash scripts/init/krun-podman.sh

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
	@test -f /usr/local/share/titanium/titanium.provisioned || bash scripts/init/titanium.sh

# fresh-host entry point: installs make/podman via dnf, then chains into init.
# (If make itself is missing, run: bash scripts/init/bootstrap.sh)
bootstrap:
	bash scripts/init/bootstrap.sh

init: sync .tmux .podman .runsc .runsc-podman .krun-podman .titanium | .sentinel/tasks
	@bash scripts/init/docker-group.sh

# utility: run any podman command in the runner's context — trial containers
# and images live in titanium's storage, invisible to the operator's podman.
#   make podman-ps ARGS=--all
#   make podman-images
#   make podman-logs ARGS=<container>
# Unprovisioned hosts fall through to the operator's own podman.
podman-%:
	@$(if $(RUNNER),RUNNER=$(RUNNER) bash scripts/titanium-run.sh )podman $* $(ARGS)

unit-podman-env: .podman
	$(PYTEST) tests/test_podman_environment.py

# The parent suites ride along: the krun seams live in the gvisor files,
# and those suites pin the runsc-flavor defaults the seams must not move.
unit-krun-podman-env: .krun-podman
	$(PYTEST) tests/test_krun_podman_environment.py tests/test_environment_factory.py \
		tests/test_gvisor_podman_environment.py tests/test_gvisor_environment.py

unit-podman: unit-podman-env

unit-all: unit-podman unit-krun-podman-env

titanium-run: | .sentinel/tasks
	mkdir -p "$(TITANIUM_JOBS_DIR)"
	$(TITANIUM_RUN)

smoke-podman: sync .podman $(RUN_TASKS)/$(BACKEND)/smoke-podman
	mkdir -p "$(REPORTS_DIR)/$(BACKEND)/$@"
	COVERAGE_FILE=$(REPORTS_DIR)/$(BACKEND)/$@/.coverage $(PYTEST) \
		tests/test_podman_environment.py \
		--html=$(REPORTS_DIR)/$(BACKEND)/$@/unit.html \
		--self-contained-html --cov=titanium.environments.podman \
		--cov-report=html:$(REPORTS_DIR)/$(BACKEND)/$@/coverage
	$(MAKE) titanium-run TITANIUM_ENV=podman TITANIUM_TASK=$(RUN_TASKS)/$(BACKEND)/$@ TITANIUM_JOBS_DIR=$(TITANIUM_JOBS_DIR)/$(BACKEND)/$@

smoke-gvisor: sync .runsc $(RUN_TASKS)/$(BACKEND)/smoke-gvisor
	mkdir -p "$(REPORTS_DIR)/$(BACKEND)/$@"
	COVERAGE_FILE=$(REPORTS_DIR)/$(BACKEND)/$@/.coverage $(PYTEST) \
		tests/test_gvisor_environment.py tests/test_gvisor_network_policy.py \
		--html=$(REPORTS_DIR)/$(BACKEND)/$@/unit.html \
		--self-contained-html --cov=titanium.environments.gvisor \
		--cov-report=html:$(REPORTS_DIR)/$(BACKEND)/$@/coverage
	$(MAKE) titanium-run TITANIUM_ENV=gvisor TITANIUM_TASK=$(RUN_TASKS)/$(BACKEND)/$@ TITANIUM_JOBS_DIR=$(TITANIUM_JOBS_DIR)/$(BACKEND)/$@

smoke-gvisor-podman: sync .runsc-podman $(RUN_TASKS)/$(BACKEND)/smoke-gvisor-podman
	mkdir -p "$(REPORTS_DIR)/$(BACKEND)/$@"
	COVERAGE_FILE=$(REPORTS_DIR)/$(BACKEND)/$@/.coverage $(PYTEST) \
		tests/test_gvisor_podman_environment.py tests/test_environment_factory.py \
		--html=$(REPORTS_DIR)/$(BACKEND)/$@/unit.html \
		--self-contained-html --cov=titanium.environments.gvisor \
		--cov-report=html:$(REPORTS_DIR)/$(BACKEND)/$@/coverage
	$(MAKE) titanium-run TITANIUM_ENV=gvisor-podman TITANIUM_TASK=$(RUN_TASKS)/$(BACKEND)/$@ TITANIUM_JOBS_DIR=$(TITANIUM_JOBS_DIR)/$(BACKEND)/$@

smoke-krun-podman: sync .krun-podman $(RUN_TASKS)/$(BACKEND)/smoke-krun-podman
	mkdir -p "$(REPORTS_DIR)/$(BACKEND)/$@"
	COVERAGE_FILE=$(REPORTS_DIR)/$(BACKEND)/$@/.coverage $(PYTEST) \
		tests/test_krun_podman_environment.py tests/test_environment_factory.py \
		--html=$(REPORTS_DIR)/$(BACKEND)/$@/unit.html \
		--self-contained-html --cov=titanium.environments.krun \
		--cov-report=html:$(REPORTS_DIR)/$(BACKEND)/$@/coverage
	$(MAKE) titanium-run TITANIUM_ENV=krun-podman TITANIUM_TASK=$(RUN_TASKS)/$(BACKEND)/$@ TITANIUM_JOBS_DIR=$(TITANIUM_JOBS_DIR)/$(BACKEND)/$@

# full-dataset benchmarks (default env gvisor-podman; run `make init` to provision).
# BENCH_N concurrent trials each — bench-all fans out two, so 2*BENCH_N total.
bench-ds: sync | .sentinel/tasks
	$(MAKE) titanium-run TITANIUM_TASK=$(TASKS_PATH_DS)/tasks TITANIUM_JOBS_DIR=$(TITANIUM_JOBS_DIR)/$(BACKEND)/$@ TITANIUM_N=$(BENCH_N)

bench-tb2: sync | .sentinel/tasks
	$(MAKE) titanium-run TITANIUM_TASK=$(TASKS_PATH_TB2) TITANIUM_JOBS_DIR=$(TITANIUM_JOBS_DIR)/$(BACKEND)/$@ TITANIUM_N=$(BENCH_N)

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

smoke-env: SESSION_TARGETS = smoke-podman smoke-gvisor smoke-gvisor-podman smoke-krun-podman
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
# TITANIUM_IMAGE_SOURCE=prebuilt deployments.
IMAGES_TASKS ?= examples/smoke
IMAGES_ARCHIVE ?= $(RUN_DIR)/images.tar
images-vendor: sync .podman
	bash scripts/images/vendor.sh $(IMAGES_TASKS) $(IMAGES_ARCHIVE) $(IMAGES_VENDOR_ARGS)

images-restore: .podman
	bash scripts/images/restore.sh $(IMAGES_ARCHIVE)

# ----------------------------------------------------------------- teardown
ARCHIVE_DIR ?= ./.archive

# clean: drop repo-local caches (test, lint, bytecode). Leaves .venv (sync's
# domain), artifacts (.run — collect's domain), and global caches (~/.cache/uv)
# alone. reset needs no clean: its git clean subsumes this.
clean:
	@rm -rf .pytest_cache .ruff_cache .mypy_cache .coverage .coverage.*
	find . \( -path ./.venv -o -path ./.git -o -path ./.archive \) -prune \
		-o -type d -name __pycache__ -print0 | xargs -0r rm -rf
	echo "caches removed"

# collect: sweep every repo-local artifact dot-folder (.run, .tasks, …) into
# a timestamped archive instead of deleting it — the collection maneuver
# before a reset, or on its own to shelve a finished campaign. Only
# *untracked* dot-folders qualify: anything holding tracked files (.vscode,
# .github, …) is source, not artifact. Also skipped: .git and .archive are
# structural; .venv is rebuilt byte-equivalent by `make sync` and carries
# nothing worth keeping (reset removes it).
collect:
	@stamp=$$(date +%Y-%m-%d__%H-%M-%S)
	dest="$(ARCHIVE_DIR)/$$stamp"
	moved=0
	for d in .*/; do
		case "$$d" in ./|../|.git/|.archive/|.venv/) continue ;; esac
		test -d "$$d" || continue
		if git ls-files --error-unmatch "$$d" >/dev/null 2>&1 || [ -n "$$(git ls-files "$$d" | head -1)" ]; then
			echo "skipping $$d (tracked)"
			continue
		fi
		mkdir -p "$$dest"
		mv "$$d" "$$dest/"
		echo "archived $$d -> $$dest/"
		moved=1
	done
	test "$$moved" = 1 || echo "nothing to collect"

# reset: undo `make init` transitively and return the checkout to
# fresh-clone equivalence, keeping only .secrets and .archive (collect runs
# first, so artifacts are shelved, not lost). Host side, the inverse of the
# init chain: runner user + ACLs + linger, delegation drop-in, runsc binaries
# and both engine registrations, digest pin, provisioned stamp, operator's
# docker group grant. Distro packages (podman, docker, tmux, uv, gcc) stay —
# reset owns titanium's state, not the machine's package set. Tracked-file
# edits are never touched; only untracked/ignored state is cleaned. Ends by
# asserting the slate is actually clean.
reset: collect
	bash scripts/reset/deprovision.sh
	git clean -xdf -e .secrets -e .archive
	bash scripts/reset/assert-clean-slate.sh

sync: .deps .uv
	$(UV) sync

upgrade: .uv
	$(UV) lock --upgrade
 