"""krun (libkrun microVM) environment: ``--env krun-podman`` (Podman-driven).

Nothing is exported here: the environment lives in
:mod:`titanium.environments.krun.podman` and is imported lazily by the factory,
so selecting other environments never loads it.
"""
