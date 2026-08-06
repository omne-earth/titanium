.PHONY: proof-gvisor-env

# Agent/model used for the real, model-backed trajectory. Override on the
# command line or in the environment, e.g.:
#   make proof-gvisor-env PROOF_MODEL=claude-sonnet-4-5
PROOF_AGENT ?= claude-code
PROOF_MODEL ?= claude-haiku-4-5-20251001

# Tasks: the new behavior-only probe task, and the existing deterministic
# no-network task reused as a cheap pre-flight gate.
PROOF_TASK ?= examples/tasks/agent-behavior-probe
PROOF_GATE_TASK ?= examples/tasks/hello-world-no-internet

PROOF_JOBS_DIR ?= jobs
PROOF_ARTIFACTS_ROOT ?= proof-artifacts/gvisor-env
PROOF_ENGINE_CLI ?= docker
PROOF_TIMEOUT_MULTIPLIER ?=

# Auditable, reproducible evidence that the first-class gVisor environment
# (--env gvisor) does what it claims: a real model-backed agent trajectory
# recorded by Pier, plus trusted host-side proof that runsc is used for the
# workload, Docker's own defaults are untouched, and cleanup leaves nothing
# behind.
proof-gvisor-env:
	@PROOF_AGENT="$(PROOF_AGENT)" \
	PROOF_MODEL="$(PROOF_MODEL)" \
	PROOF_TASK="$(PROOF_TASK)" \
	PROOF_GATE_TASK="$(PROOF_GATE_TASK)" \
	PROOF_JOBS_DIR="$(PROOF_JOBS_DIR)" \
	PROOF_ARTIFACTS_ROOT="$(PROOF_ARTIFACTS_ROOT)" \
	PROOF_ENGINE_CLI="$(PROOF_ENGINE_CLI)" \
	PROOF_TIMEOUT_MULTIPLIER="$(PROOF_TIMEOUT_MULTIPLIER)" \
	bash scripts/proof-gvisor-env.sh
