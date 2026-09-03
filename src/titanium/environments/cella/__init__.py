"""Cella (from-scratch KVM microVM) environment: ``--env cella``.

Nothing is exported here. The environment is imported lazily by the factory,
so selecting any other environment never loads Cella driving code -- the same
rule :mod:`titanium.environments.krun` follows.

Cella is not an OCI runtime. It has no image, no layers, no build context, no
bind mounts, and no ``exec``: a machine is staged from a Cella-owned *golden
flavor* and observed through files and verbs. The consequences for this package
are recorded in ``docs/environments/CELLA.md`` as they are built.
"""
