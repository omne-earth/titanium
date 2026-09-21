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


# ---------------------------------------------------------------------------
# The environment class: pure parts, no cella and no podman
# ---------------------------------------------------------------------------

import tarfile
from pathlib import Path

import pytest

from titanium.environments.base import SealedPhaseSpec, SealedPhaseStep
from titanium.environments.cella.environment import (
    CellaEnvironment,
    _flavor_name,
)
from titanium.environments.cella.flavor import validate_flavor_name
from titanium.models.task.config import EnvironmentConfig as TaskEnvironmentConfig
from titanium.models.trial.paths import TrialPaths


def _make_env(tmp_path, allow_internet=False):
    environment_dir = tmp_path / "environment"
    environment_dir.mkdir(exist_ok=True)
    (environment_dir / "Dockerfile").write_text("FROM debian:12-slim\n")
    trial_paths = TrialPaths(trial_dir=tmp_path / "trial")
    trial_paths.mkdir()
    return CellaEnvironment(
        environment_dir=environment_dir,
        environment_name="cella-task",
        session_id="cella-task__abc123",
        trial_paths=trial_paths,
        task_env_config=TaskEnvironmentConfig(allow_internet=allow_internet),
    )


def test_the_environment_registers_and_declares_itself(tmp_path):
    env = _make_env(tmp_path)
    assert env.type() == "cella"
    assert env.capabilities.disable_internet
    assert env.capabilities.preinstall_agents
    assert not env.capabilities.mounted
    assert env.resource_capabilities().memory_limit
    assert not env.resource_capabilities().cpu_limit


def test_a_judged_environment_pairs_with_the_terminator(tmp_path):
    env = _make_env(tmp_path, allow_internet=True)
    assert env._topology.judged
    # An internet task stands the terminated pair even without an
    # agent: the member is wire-only (the appliance holds the world).
    assert env._paired
    assert env._task_net() == f"wire:{env._wire_name()}"


def test_dry_run_accepts_the_string_forms(tmp_path):
    assert not _make_env(tmp_path)._dry_run
    environment_dir = tmp_path / "environment"
    trial_paths = TrialPaths(trial_dir=tmp_path / "trial2")
    trial_paths.mkdir()
    env = CellaEnvironment(
        environment_dir=environment_dir,
        environment_name="cella-task",
        session_id="s",
        trial_paths=trial_paths,
        task_env_config=TaskEnvironmentConfig(allow_internet=True),
        dry_run="true",
    )
    assert env._dry_run
    assert env._policy_path() == environment_dir / "cella.policy"


def test_flavor_names_are_safe_and_within_the_extractor_budget():
    name = _flavor_name("Task__Trial!weird name")
    # The machine-name contract is the strict one: lowercase letters,
    # digits, and dashes only. No harness prefix, no cycle counter:
    # one boot runs the whole trial, so the session id alone names it.
    assert name == "task-trial-weird-name"
    assert validate_flavor_name(name) == name
    assert all(c.islower() or c.isdigit() or c == "-" for c in name)
    # cella caps machine names at 64 and `cella extract` appends
    # `-extractor` (10); the longest name this can produce -- the
    # 40-char session cap -- must fit.
    longest = _flavor_name("x" * 100)
    assert len(longest) + len("-extractor") <= 64


@pytest.mark.asyncio
async def test_uploads_queue_and_last_write_wins(tmp_path):
    env = _make_env(tmp_path)
    source = tmp_path / "solve.sh"
    source.write_text("echo one")
    source.chmod(0o755)
    await env.upload_file(source, "/solution/solve.sh")
    source.write_text("echo two")
    await env.upload_file(source, "/solution/solve.sh")
    assert len(env._pending) == 1
    entry = env._pending[0]
    assert entry.contents == b"echo two"
    assert entry.mode == 0o755


@pytest.mark.asyncio
async def test_upload_dir_walks_and_preserves_links(tmp_path):
    env = _make_env(tmp_path)
    tree = tmp_path / "tests-dir"
    (tree / "sub").mkdir(parents=True)
    (tree / "test.sh").write_text("#!/bin/bash\n")
    (tree / "sub" / "helper.py").write_text("x = 1\n")
    (tree / "link.sh").symlink_to("test.sh")
    await env.upload_dir(tree, "/tests")
    paths = {entry.path for entry in env._pending}
    assert paths == {"/tests/test.sh", "/tests/sub/helper.py", "/tests/link.sh"}
    link = next(e for e in env._pending if e.path == "/tests/link.sh")
    assert link.target == "test.sh"


def test_orchestrator_files_render_the_trial(tmp_path):
    env = _make_env(tmp_path)
    env._image_config = {"WorkingDir": "/app", "Env": ["PATH=/usr/bin"]}
    phases = [
        SealedPhaseSpec(
            name="agent",
            steps=[SealedPhaseStep(command="bash solve.sh", env={"K": "a b"})],
            timeout_sec=600,
        ),
        SealedPhaseSpec(
            name="verify",
            steps=[
                SealedPhaseStep(command="chmod +x /tests/test.sh", user="root"),
                SealedPhaseStep(command="bash /tests/test.sh"),
            ],
            timeout_sec=300,
        ),
    ]
    files = env._orchestrator_files(phases)
    by_path = {entry.path: entry for entry in files}
    agent_phase = by_path["/titanium/phases/agent.sh"].contents.decode()
    assert "cd '/app'" in agent_phase or "cd /app" in agent_phase
    assert "export PATH=/usr/bin" in agent_phase
    assert "export K='a b'" in agent_phase
    # The rung's law: an undeclared agent step never runs as root --
    # it falls to the baked standard user. Root is written down
    # (agent.user = "root"), never inherited.
    assert "runuser -u titanium -- bash /titanium/steps/agent-0.sh" in agent_phase
    assert by_path["/titanium/steps/agent-0.sh"].contents.decode() == "bash solve.sh\n"
    verify_phase = by_path["/titanium/phases/verify.sh"].contents.decode()
    # A failing step ends its phase with its rc on the record.
    assert "echo $rc > $R/verify/rc" in verify_phase
    orchestrator = by_path["/titanium/orchestrator.sh"].contents.decode()
    # The root-created writable surfaces are handed to the payload
    # user before any phase runs.
    assert "chown -R titanium: /logs/agent /app" in orchestrator
    # Phase budgets are baked and enforced in-guest.
    assert "timeout -k 10 600 bash /titanium/phases/agent.sh" in orchestrator
    assert "timeout -k 10 300 bash /titanium/phases/verify.sh" in orchestrator
    # A phase killed by its budget still leaves an rc.
    assert "[ -f $R/agent/rc ] || echo 124 > $R/agent/rc" in orchestrator
    # The forced reset is the completion signal, and a re-boot that
    # did not exit the VMM resets again off the done marker.
    assert "reboot -f" in orchestrator
    assert orchestrator.index('if [ -f "$R/done" ]') < orchestrator.index("mkdir -p $R ")
    unit = by_path["/etc/systemd/system/titanium-trial.service"].contents.decode()
    assert "ExecStart=/bin/bash /titanium/orchestrator.sh" in unit
    wants = by_path[
        "/etc/systemd/system/multi-user.target.wants/titanium-trial.service"
    ]
    assert wants.target == "../titanium-trial.service"


def test_verifier_orchestrator_folds_results_under_logs(tmp_path):
    env = _make_env(tmp_path)
    phases = [SealedPhaseSpec(name="verify", steps=[SealedPhaseStep(command="true")])]
    folded = next(
        e
        for e in env._orchestrator_files(phases, fold_results=True)
        if e.path == "/titanium/orchestrator.sh"
    ).contents.decode()
    # One extract of /logs must retrieve the phase results too.
    assert "cp -r $R/. /logs/titanium-result/" in folded
    assert folded.index("cp -r $R/.") < folded.index("touch $R/done")
    bare = next(
        e
        for e in env._orchestrator_files(phases)
        if e.path == "/titanium/orchestrator.sh"
    ).contents.decode()
    assert "titanium-result" not in bare


def test_task_owned_sudoers_bakes_verbatim_or_not_at_all(tmp_path):
    env = _make_env(tmp_path)
    assert env._sudoers_entries() == ()
    grant = "agent ALL=(root) NOPASSWD: /usr/bin/apt-get\n"
    (tmp_path / "environment" / "sudoers").write_text(grant)
    (entry,) = env._sudoers_entries()
    assert entry.path == "/etc/sudoers.d/titanium-agent"
    assert entry.contents == grant.encode()
    # sudo refuses a sudoers file that is not 0440 root:root.
    assert (entry.mode, entry.uid, entry.gid) == (0o440, 0, 0)


def test_verifier_result_root_avoids_the_members_marker(tmp_path):
    env = _make_env(tmp_path)
    phases = [SealedPhaseSpec(name="verify", steps=[SealedPhaseStep(command="true")])]
    files = env._orchestrator_files(
        phases, fold_results=True, result_dir="/titanium/result-verifier"
    )
    orch = next(
        e for e in files if e.path == "/titanium/orchestrator.sh"
    ).contents.decode()
    # Its own root and marker: the member's carried /titanium/result/done
    # must not fire the re-entry guard.
    assert "R=/titanium/result-verifier" in orch
    assert "/titanium/result/done" not in orch
    phase = next(
        e for e in files if e.path == "/titanium/phases/verify.sh"
    ).contents.decode()
    assert "R=/titanium/result-verifier\n" in phase


def test_evidence_cache_resolves_by_longest_root(tmp_path):
    env = _make_env(tmp_path)
    state = tmp_path / "state"
    (state / "logs" / "agent").mkdir(parents=True)
    (state / "logs" / "agent" / "old.txt").write_text("member era")
    verifier_logs = tmp_path / "vlogs"
    (verifier_logs / "verifier").mkdir(parents=True)
    (verifier_logs / "verifier" / "reward.txt").write_text("1")
    env._evidence_cache = {"/": state, "/logs": verifier_logs}
    # The verifier's /logs overlays the member's tree.
    assert (
        env._evidence_cache_dir("/logs/verifier") == verifier_logs / "verifier"
    )
    # Anything outside /logs still reads from the member's state.
    assert env._evidence_cache_dir("/app") == state / "app"
    with pytest.raises(FileNotFoundError):
        empty = _make_env(tmp_path / "e")
        empty._evidence_cache_dir("/app")


def test_orchestrator_steps_drop_privilege_when_asked(tmp_path):
    env = _make_env(tmp_path)
    phases = [
        SealedPhaseSpec(
            name="agent", steps=[SealedPhaseStep(command="id", user="agent")]
        )
    ]
    files = env._orchestrator_files(phases)
    phase = next(
        e for e in files if e.path == "/titanium/phases/agent.sh"
    ).contents.decode()
    assert "runuser -u agent -- bash /titanium/steps/agent-0.sh" in phase


def _state_tar(tmp_path, entries) -> Path:
    path = tmp_path / "state.tar"
    with tarfile.open(path, "w") as archive:
        for name, data in entries:
            info = tarfile.TarInfo("./" + name)
            info.size = len(data)
            import io

            archive.addfile(info, io.BytesIO(data))
    return path


@pytest.mark.asyncio
async def test_downloads_read_the_evidence_tree(tmp_path):
    env = _make_env(tmp_path)
    env._base_tar = _state_tar(
        tmp_path,
        [
            ("app/report.json", b"{}"),
            ("logs/verifier/reward.txt", b"1\n"),
            ("logs/verifier/sub/detail.txt", b"d"),
        ],
    )
    target = tmp_path / "out" / "report.json"
    await env.download_file("/app/report.json", target)
    assert target.read_bytes() == b"{}"
    with pytest.raises(FileNotFoundError):
        await env.download_file("/absent", tmp_path / "x")

    logs = tmp_path / "logs-out"
    await env.download_dir("/logs/verifier", logs)
    assert (logs / "reward.txt").read_bytes() == b"1\n"
    assert (logs / "sub" / "detail.txt").read_bytes() == b"d"
    # An absent directory downloads as empty, not as an error.
    empty = tmp_path / "empty-out"
    await env.download_dir("/nothing", empty)
    assert empty.is_dir() and not any(empty.iterdir())


# ---------------------------------------------------------------------------
# The terminated pair, wired into the environment
# ---------------------------------------------------------------------------

from titanium.environments.cella.policy import Policy


def _make_agent_env(tmp_path, allow_internet):
    environment_dir = tmp_path / "environment"
    environment_dir.mkdir(exist_ok=True)
    (environment_dir / "Dockerfile").write_text("FROM debian:12-slim\n")
    trial_paths = TrialPaths(trial_dir=tmp_path / "trial")
    trial_paths.mkdir()
    from titanium.models.agent.install import AgentInstallSpec, InstallStep

    return CellaEnvironment(
        environment_dir=environment_dir,
        environment_name="cella-task",
        session_id="pair-task__abc",
        trial_paths=trial_paths,
        task_env_config=TaskEnvironmentConfig(allow_internet=allow_internet),
        agent_install_spec=AgentInstallSpec(
            agent_name="mini-swe-agent",
            steps=[InstallStep(run="true", user="root")],
        ),
    )


def test_an_agent_stands_the_pair_even_airgapped(tmp_path):
    env = _make_agent_env(tmp_path, allow_internet=False)
    # An agent always needs its inference line, so the pair stands and
    # the member is wire-only -- egress only through the terminator.
    assert env._paired
    assert env._task_net() == f"wire:{env._wire_name()}"


def test_the_member_policy_is_fixed_wire_grants(tmp_path):
    env = _make_agent_env(tmp_path, allow_internet=False)
    # A task cella.policy naming world domains does not touch the member
    # border; the member reaches only the appliance.
    (tmp_path / "environment" / "cella.policy").write_text(
        "release outgoing deb.debian.org:80/tcp\n"
    )
    env._work = tmp_path / "work"
    env._work.mkdir()
    member = Policy.load(env._member_policy_path())
    assert all(g.host == "" for g in member.grants)
    assert any("10.77.0.1:443/tcp" in g.line() for g in member.grants)


def test_the_appliance_border_carries_the_task_domains(tmp_path):
    # allow_internet=true routes the task's declared domains to the
    # appliance border, judged by name; the agent's host rides too.
    env = _make_agent_env(tmp_path, allow_internet=True)
    env.network_allowlist.domains = ["openrouter.ai"]
    (tmp_path / "environment" / "cella.policy").write_text(
        "release outgoing deb.debian.org:80/tcp\n"
        "release outgoing astral.sh:443/tcp\n"
    )
    env._work = tmp_path / "work"
    env._work.mkdir()
    assert set(env._world_hosts()) == {"openrouter.ai", "deb.debian.org", "astral.sh"}
    appliance = Policy.load(env._appliance_policy_path())
    host_grants = {g.host for g in appliance.grants if g.host}
    assert {"openrouter.ai", "deb.debian.org", "astral.sh"} <= host_grants


def test_airgapped_keeps_task_domains_off_the_appliance(tmp_path):
    # allow_internet=false: the agent's inference host is granted, but
    # the task's own declared domains are not -- airgapped-except-line.
    env = _make_agent_env(tmp_path, allow_internet=False)
    env.network_allowlist.domains = ["openrouter.ai"]
    (tmp_path / "environment" / "cella.policy").write_text(
        "release outgoing deb.debian.org:80/tcp\n"
    )
    env._work = tmp_path / "work"
    env._work.mkdir()
    assert env._world_hosts() == ["openrouter.ai"]


def test_an_agentless_airgapped_trial_has_no_pair(tmp_path):
    env = _make_env(tmp_path)
    assert not env._paired
    assert env._task_net() == "none"


@pytest.mark.asyncio
async def test_chronicle_is_preserved_before_destroy(tmp_path, monkeypatch):
    env = _make_env(tmp_path)
    # A fake machine dir with the audit files a real run leaves.
    machine = tmp_path / "machines" / "m"
    (machine / "network").mkdir(parents=True)
    (machine / "network" / "ledger").write_bytes(b"LEDGER")
    (machine / "verdict").write_bytes(b"VERDICT")
    (machine / "audit").write_bytes(b"AUDIT")
    (machine / "membrane-memory").write_bytes(b"MEM")
    (machine / "manifest.json").write_text("{}")
    (machine / "disk.img").write_bytes(b"HUGE")  # never copied
    monkeypatch.setattr(env, "_machine_dir", lambda name: machine)
    # cella --dump is the authoritative decoder; stand in for it.
    monkeypatch.setattr(env, "_cella", lambda *a, **k: f"DUMP {a[-1]}\n")

    env._preserve_chronicle("m")

    out = env.trial_paths.trial_dir / "cella-chronicle" / "m"
    assert (out / "network" / "ledger").read_bytes() == b"LEDGER"
    assert (out / "verdict").read_bytes() == b"VERDICT"
    assert (out / "audit").read_bytes() == b"AUDIT"
    assert (out / "membrane-memory").read_bytes() == b"MEM"
    assert (out / "manifest.json").read_text() == "{}"
    # The disk is evidence, not audit record: never copied.
    assert not (out / "disk.img").exists()
    # Each framed book gets a .txt rendering; the JSON manifest does not.
    assert (out / "network" / "ledger.txt").exists()
    assert (out / "verdict.txt").exists()
    assert (out / "audit.txt").exists()
    assert (out / "membrane-memory.txt").exists()
    assert not (out / "manifest.json.txt").exists()


@pytest.mark.asyncio
async def test_chronicle_preservation_skips_absent_files(tmp_path, monkeypatch):
    # An airgapped machine (--net none) has an audit book but no
    # verdict or membrane-memory; preservation just skips them.
    env = _make_env(tmp_path)
    machine = tmp_path / "machines" / "air"
    machine.mkdir(parents=True)
    (machine / "audit").write_bytes(b"AUDIT")
    monkeypatch.setattr(env, "_machine_dir", lambda name: machine)
    monkeypatch.setattr(env, "_cella", lambda *a, **k: "DUMP\n")

    env._preserve_chronicle("air")  # must not raise

    out = env.trial_paths.trial_dir / "cella-chronicle" / "air"
    assert (out / "audit").read_bytes() == b"AUDIT"
    assert (out / "audit.txt").exists()
    assert not (out / "verdict").exists()


# ---------------------------------------------------------------------------
# The terminated pair (the terminator): the agent line as cella's own
# appliance, replacing the tinyproxy router.
# ---------------------------------------------------------------------------

from titanium.environments.cella import constants as C
from titanium.environments.cella import terminator as term


def test_the_constants_match_the_goldens_boot_defaults():
    # The appliance boots the terminator golden directly; its init
    # writes /etc/cella-terminator.conf at boot from these defaults
    # (cella scripts/build/rootfs-terminator.sh). Titanium injects
    # nothing -- these constants must equal the golden's defaults, or
    # the borders titanium composes judge a different appliance than
    # the one that boots.
    assert C.APPLIANCE_WIRE_ADDRESS == "10.77.0.1"
    assert C.UPSTREAM_DNS == "9.9.9.9"
    assert tuple(C.LISTEN_PORTS) == (443, 80)


def test_member_trust_bakes_the_ca_and_points_the_resolver():
    entries = {e.path: e for e in term.member_trust_entries(b"PAIRCA")}
    assert entries[C.MEMBER_CA_PATH].contents == b"PAIRCA"
    assert entries[C.MEMBER_CA_PATH].mode == 0o444
    resolv = entries["/etc/resolv.conf"].contents.decode()
    assert f"nameserver {C.APPLIANCE_WIRE_ADDRESS}\n" in resolv
    # Patience past the appliance's first-crossing freeze, or the lookup
    # times out before the frozen reply is thawed.
    assert "timeout:30" in resolv


def test_member_prelude_trusts_the_pair_and_pins_the_reply_window():
    prelude = term.member_prelude("eth0")
    assert f"ip addr replace {term.MEMBER_WIRE_ADDRESS}/24 dev eth0" in prelude
    # The pair CA is folded into the system bundle the native clients read.
    assert f"cat {C.MEMBER_CA_PATH} >> {C.SYSTEM_CA_BUNDLE}" in prelude
    # And Python's TLS is pointed at that bundle, so the agent's certifi-based
    # inference client trusts the appliance's minted leaf, not just curl/git.
    assert f"export SSL_CERT_FILE={C.SYSTEM_CA_BUNDLE}" in prelude
    assert f"export REQUESTS_CA_BUNDLE={C.SYSTEM_CA_BUNDLE}" in prelude
    # The ephemeral range is pinned to the appliance's granted reply window.
    assert (
        f"echo '{C.REPLY_PORT_LOW} {C.REPLY_PORT_HIGH}' "
        "> /proc/sys/net/ipv4/ip_local_port_range" in prelude
    )


def test_member_policy_reaches_only_the_appliance():
    policy = Policy.parse(term.member_policy_text())
    lines = {g.line() for g in policy.grants}
    gw = C.APPLIANCE_WIRE_ADDRESS
    # Every member hop is 24h: the plumbing to the appliance should never
    # re-freeze mid-run (the window governs freeze frequency, not reach).
    assert f"release outgoing {gw}:443/tcp (keep_open=24h) (skip_freeze=true)" in lines
    assert f"release outgoing {gw}:80/tcp (keep_open=24h) (skip_freeze=true)" in lines
    assert f"release outgoing {gw}:53/udp (keep_open=24h) (skip_freeze=true)" in lines
    # No world name ever appears on the member border.
    assert all(g.host == "" for g in policy.grants)


def test_appliance_border_judges_the_world_by_name():
    policy = Policy.parse(
        term.appliance_border_policy_text(["deb.debian.org", "astral.sh"])
    )
    host_grants = {(g.host, g.port) for g in policy.grants if g.host}
    assert ("deb.debian.org", 443) in host_grants
    assert ("deb.debian.org", 80) in host_grants
    assert ("astral.sh", 443) in host_grants
    # The upstream resolver and the member's reply window are present.
    ips = {(g.ip, g.port, g.proto) for g in policy.grants if not g.host}
    assert (C.UPSTREAM_DNS, 53, 17) in ips
    assert (term.MEMBER_WIRE_ADDRESS, C.REPLY_PORT_LOW, 6) in ips
    assert (term.MEMBER_WIRE_ADDRESS, C.REPLY_PORT_HIGH, 17) in ips


def test_appliance_border_parses_with_no_hosts():
    # An agentless, task-egress-free trial still stands the pair; the
    # border is just DNS, ARP, and the reply window. Must parse.
    policy = Policy.parse(term.appliance_border_policy_text([]))
    assert not any(g.host for g in policy.grants)


def _make_env_with(tmp_path, **kw):
    ed = tmp_path / "environment"
    ed.mkdir(exist_ok=True)
    (ed / "Dockerfile").write_text("FROM debian:12-slim\n")
    tp = TrialPaths(trial_dir=tmp_path / "trial")
    tp.mkdir()
    return CellaEnvironment(
        environment_dir=ed,
        environment_name="cella-task",
        session_id="cella-task__abc",
        trial_paths=tp,
        task_env_config=TaskEnvironmentConfig(allow_internet=False),
        **kw,
    )


def test_on_completion_parses_teardown_and_archive(tmp_path):
    # The absent default is teardown; only an explicit `archive` archives.
    assert _make_env_with(tmp_path)._on_completion == "teardown"
    assert _make_env_with(tmp_path, on_completion="archive")._on_completion == "archive"
    assert _make_env_with(tmp_path, on_completion="ARCHIVE")._on_completion == "archive"
    assert _make_env_with(tmp_path, on_completion="nonsense")._on_completion == "teardown"


def test_retire_machine_destroys_by_default_and_archives_on_flag(tmp_path):
    env = _make_env_with(tmp_path)
    verbs = []
    env._cella = lambda *a, **k: verbs.append(a[0])
    env._retire_machine("m")
    assert verbs == ["stop", "destroy"]  # teardown: the machine is deleted
    env._on_completion = "archive"
    verbs.clear()
    env._retire_machine("m")
    assert verbs == ["stop", "archive"]  # archive: the machine is kept as an artifact
