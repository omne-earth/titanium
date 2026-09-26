"""Capability flags describing what an environment type can do.

One ``EnvironmentCapabilities`` instance per environment, computed at
construction time and stored as ``self.capabilities``. Validators and
call sites read from it instead of from individual properties.
"""

from pydantic import BaseModel


class EnvironmentCapabilities(BaseModel):
    gpus: bool = False
    """Whether the environment can allocate GPUs to containers."""

    disable_internet: bool = False
    """Whether the environment can run containers without internet access."""

    filtered_egress: bool = False
    """Whether the environment can allow only declared outbound inference hosts."""

    preinstall_agents: bool = False
    """Whether the environment can install selected agents at image build time."""

    windows: bool = False
    """Whether the environment can run Windows containers."""

    mounted: bool = False
    """Whether the environment mounts log directories as host filesystems."""

    docker_compose: bool = False
    """Whether the environment can run Docker Compose task environments."""

    sealed_oneshot: bool = False
    """Whether the environment runs the whole trial as one sealed boot.

    A sealed-oneshot environment takes every input at bake time (the
    agent's command spec, the tests, the uploads) and runs the phases
    in-guest under its own orchestrator; the trial flow must not issue
    per-phase execs. Requires an agent that exposes ``command_spec()``.
    """


class EnvironmentResourceCapabilities(BaseModel):
    cpu_limit: bool = False
    """Whether CPU resources can be applied as a hard ceiling."""

    cpu_request: bool = False
    """Whether CPU resources can be applied as a resource request/reservation."""

    memory_limit: bool = False
    """Whether memory resources can be applied as a hard ceiling."""

    memory_request: bool = False
    """Whether memory resources can be applied as a resource request/reservation."""
