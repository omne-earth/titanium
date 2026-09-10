"""Unit tests for the cella environment plumbing.

First resident: the ``allow_internet`` -> network topology mapping.
The flag is harbor's knob and defines which machine gets created, not
a posture inside one; these tests pin the mapping as total and closed.
"""

from titanium.environments.cella.environment import (
    NetworkTopology,
    network_topology,
)


def test_allow_internet_false_is_no_nic_at_all():
    topology = network_topology(False)
    assert topology == NetworkTopology(net="none", open_gateway=False, judged=False)


def test_allow_internet_true_is_a_judged_world_nic():
    topology = network_topology(True)
    assert topology == NetworkTopology(net="world", open_gateway=True, judged=True)


def test_the_judgment_machinery_exists_exactly_when_a_nic_does():
    # No third topology: a machine either has no network and no judge,
    # or a world nic whose every crossing is judged. Never a nic with
    # no judge, never a judge with nothing to judge.
    for allow_internet in (False, True):
        topology = network_topology(allow_internet)
        assert topology.judged == (topology.net != "none")
        assert topology.open_gateway == topology.judged
