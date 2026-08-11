"""gVisor (runsc) environments: ``--env gvisor`` (Docker-driven) and
``--env gvisor-podman`` (Podman-driven).

Only the Docker flavor is exported here; the Podman flavor lives in
:mod:`titanium.environments.gvisor.podman` and is imported lazily by the factory,
so selecting ``--env gvisor`` never loads Podman driving code.
"""

from titanium.environments.gvisor.environment import GVisorEnvironment

__all__ = ["GVisorEnvironment"]
