# Cella environment: the sealed-VM

The selector is `--env cella`. The class is
`titanium.environments.cella.environment.CellaEnvironment`. The
package is `src/titanium/environments/cella/`. Cella itself lives at
<https://github.com/omne-earth/cella>; titanium pins one revision of it
in `runtime.env` and installs that revision with `make .cella`.

This document is complete for fresh eyes. It states the model, the
knobs, the lifecycle, the collection mechanism, the one judgment call,
and why krun is a hard dependency. This is a running document: the
`-www` leg (§5) updates when it lands.

## 1. The model: bake, run, collect

Every other titanium rung drives a live workload from outside. The
harness starts a container, then reaches into it: `exec` runs
commands, `upload` places files, the verifier runs in the workload's
own filesystem, and artifacts are copied out of a running machine.

Cella refuses all of that by design. A cella machine is a sealed
experiment:

1. **Bake.** Content enters at build time only. The converter turns
   the task's `environment/Dockerfile` into a systemd-bootable ext4
   with a manifest (`docs` in the cella repository:
   `docs/integration/TITANIUM.md`). Titanium's contributions enter as
   a *boot layer*: declared files and symlinks, placed at build time.
2. **Run.** The machine runs jailed and alone. There is no exec-into,
   no console in the field flavor, and no host mount. The only live
   observations are host-side files cella itself writes.
3. **Collect.** The run ends when the guest halts or the timeout
   stops it. Results are read from the still disk as evidence. A
   still disk cannot answer questions; it can only be read.
   Verification against evidence is stronger than verification by
   conversation.

The titanium runner user is never used on this rung. Cella ships its
own separation: each machine runs as its own sub-uid in a bwrap jail
with ACL-granted directories. A second separation scheme on top would
fight the first. Titanium invokes cella as the plain operator.

`titanium run` is unmodified. `CellaEnvironment` maps the exec-model
contract onto bake/run/collect (§4). The map is honest: no call lies
about what happened, and no call opens a channel into a machine.

## 2. The two knobs, orthogonal

Two controls exist. They live in different contract layers, they have
different owners, and neither reads the other.

**`allow_internet` (harbor's knob, in `task.toml`) defines the network
topology.** The task author declares it. On this rung the two values
are two different machines:

| Value   | Machine                                                       |
|---------|---------------------------------------------------------------|
| `false` | `cella create --net none`. No nic exists. Not a shut valve on a network: no translator, no membrane, no ledger, and none of the judgment machinery. |
| `true`  | `cella create --net world`, then `cella gateway <vm> open` after start. Open is the membrane, not a free path: every crossing parks for a decision. |

The mapping is `network_topology()` in `environment.py`: total,
closed, pure. There is no third topology.

**`cella.policy` (titanium+cella's knob, beside the Dockerfile)
defines the crossing rules at a border.** One grant per line.
`outgoing` grants are the egress rules; `incoming` grants are the
ingress rules. `*` matches any ip or any port. L2 grants name an
ethertype (`arp`, `ipv6`, `0xNNNN`). Everything not granted is
refused.

```
allow outgoing 140.82.112.3:443/tcp
allow incoming *:2222/tcp
allow outgoing arp
```

The knobs never meet. The flag's whole meaning is spent at
`cella create`, before any engine exists. The engine never consults
the flag. The flag never reaches into a grant. A `--net none` machine
has no border, so its `cella.policy` is never read.

## 3. The policy engine

A cella machine with a world nic decides nothing for itself. Every
crossing parks at the membrane. Cella's bridge
(`cella-engine <vm> --dial <addr>`) tails the machine's ledger,
streams each park as an `Event` over gRPC (`cella.Engine/Decide`,
from `proto/cella.proto`), lands each returned `Decision` in the
verdict file, witnesses it in the audit book, and kicks the VMM.

Titanium's engine is the judge on the far end of that dial: the
policy engine (`titanium.environments.cella.engine`), a grpclib
server titanium runs **in-process** — no subprocess, no spawn, no
readiness poll — one per machine, each dialed by that machine's own
bridge and logging under `cella-engine/<vm-id>/` (so a cycle's
crossings stay separate from the next cycle's). It opens no files in
`CELLA_HOME`, invokes no cella verb, and touches no book. The codec
(`wire.py`) is hand-written and byte-verified against `protoc`, so the
transport carries no protoc codegen and no C runtime.

That is a deliberate stack. The judge itself is a dict lookup, so the
cost that matters is the park→decide→verdict→thaw round trip, not the
message encode. Staying **in-process Python** removes the process
boundary — the spawn, the handshake, the readiness wait, and the
"Connection lost" at every teardown — that a separate engine pays per
cycle. A faster transport would not help: `grpcio` (C core) or a Rust
rewrite would only speed the encode while *reintroducing* the process
boundary in-process removes, and both undo the no-protoc lightness
`wire.py` exists to keep. `grpclib` — pure asyncio — is what embeds
cleanly in-process. If profiling ever shows the transport itself
dominating, revisit then, with data.

The one policy file travels in two directions:

* **Enforce** (default): read `cella.policy`, release granted
  crossings, refuse everything else with the why on the record
  (`no cella.policy grants this crossing`). No file grants nothing:
  fail closed.
* **Dry run** (`--dry-run`): release every crossing and *write* each
  distinct one to `cella.policy` as a grant. Review the collected
  file and check it in beside the Dockerfile, like a lockfile. The
  next run enforces it. `make smoke-cella-integration DRY_RUN=true`
  will regenerate a task's policy this way (the `-www` leg).

There are no unconditional releases in enforce mode: ARP, NDP, and
every finer exception belong to `cella.policy`, not to a hardcoded
carve-out. (This differs from cella's `motor` fixture, which lets ARP
ride free — titanium requires the grant. Once granted, though, its
membrane memory is pre-planted just as `motor` does, §3.2: the grant
requirement and the standing memory are separate things.) Every
refusal lands in the chronicle, so a task that needed a crossing shows
exactly what it asked for — every crossing the workload attempted,
including the refused ones, a property no other rung has.

### 3.1 How to collect a policy with --dry-run

Do not write a `cella.policy` from guesswork. Observe once, review,
then enforce forever:

1. **Run the task in collection mode.** For the smoke task:
   `make smoke-cella-integration DRY_RUN=true`. For any task:
   `titanium run --env cella --ek dry_run=true --path <task> ...`.
   `--ek key=value` is `titanium run`'s generic environment-kwarg
   flag — the pairs are passed into the environment class's
   constructor, and `dry_run` is `CellaEnvironment`'s. The `--ek`
   form is the real invocation; `DRY_RUN=true` is the make-level
   convenience that threads it through.
   The engine releases every crossing and writes each distinct one to
   the task's `environment/cella.policy` as a grant, rewritten on
   every new grant — a run that dies mid-way still leaves what it
   observed. The make target copies the collected file back to the
   example directory; a plain `titanium run` leaves it in the staged
   task copy under `.run/tasks/`.
2. **Review every line.** The collected file is an observation, not a
   judgment. Expect and keep the round-trip pairs — a granted flow
   needs both its `outgoing` grant and its `incoming` reply twin, and
   ARP needs both directions (the guest asks, the translator
   answers). Delete what the task does not need: background noise
   such as a resolver or time-sync attempt is a crossing the guest
   *made*, not one the task *requires*. Widen to `*` by hand only
   where a destination genuinely varies; collection always records
   exact addresses.
3. **Check it in beside the Dockerfile**, like a lockfile. The policy
   is a reviewable artifact; its diff is the task's network story.
4. **Re-run without the flag** and confirm the reward: enforce mode
   must release everything the task needs and refuse the rest, with
   the refusals on the record.

A dry run only observes; it proves nothing about enforcement. Step 4
is the proof.

### 3.2 The membrane-memory state machine (the circuit)

Latency is answered by cella's membrane memory (N.F.7), never by a
weaker thaw. A grant with a `keep_open` window plants a standing
memory, and while it stands the machine waits *live* on that
destination instead of freezing — the park is the freeze, so without
the memory the *first* crossing to every destination freezes once.
Titanium's engine models this as an explicit state machine: one small
automaton per destination, per machine.

```
  unplanted ──(plant: emit verdict + memory)──▶ remembered
      ▲                                             │
      └──────────(keep_open elapses: lapse)─────────┘
```

* **unplanted** — the first crossing emits the verdict *and* a
  membrane memory, moving the destination to *remembered*.
* **remembered** — further crossings emit the verdict only; the
  machine does not freeze on that destination.
* **lapse** — when `written + keep_open` passes, the memory clears by
  cella's own arithmetic and the destination returns to *unplanted*;
  the next crossing re-plants it. (A short-lived exec-cycle machine
  usually dies before any window clears, but the edge is real and the
  automaton owns it.)

Two rules make the circuit correct, and each was a bug before it was
a rule:

* **Pre-plant at stream open.** For a *concrete* destination — ARP (an
  ethertype) or an exact `ip:port/proto` — the memory is planted the
  moment the bridge stream opens, *before any crossing*, exactly as
  cella's reference engine (`cella-engine motor`) does. So the first
  ARP never freezes and the wire comes up at once, instead of wedging
  on a first-crossing freeze under load. A *host* or wildcard grant
  names no concrete destination until the appliance resolves it
  (§9, the terminated pair), so it plants reactively on its first
  crossing and freezes exactly once — unavoidably.
* **Per machine, not global.** The circuit resets when each bridge
  stream opens, because every exec cycle is a fresh machine with an
  empty membrane memory (§4). A single global memory would plant only
  the first machine's and leave every later cycle — the solution run,
  the verifier — to freeze on its own first ARP again.

And one transition is forbidden: an **incoming** grant never plants.
An incoming park never freezes (`skip_freeze` is outgoing-only), so
its memory would be inert — and worse, cella keys a memory by its
destination alone, so an incoming memory (`skip_freeze=false`) would
collide with and suppress the outgoing leg's live one for the same
destination.

## 4. The exec cycle: one exec, one machine

Cella has no exec-into, so `CellaEnvironment` honors the
`BaseEnvironment` contract the only honest way a sealed runtime
allows: **every `exec` is one whole experiment.**

`start()` builds once: stage the build context (`FROM` lines
qualified, agent install baked when given), `podman build`, export,
and provision systemd into the tree when the image does not carry it
(the same pipeline `make smoke-cella-rootfs` proves). The result is
the *base tar*: the guest filesystem as bytes, numeric ownership
preserved. Nothing boots yet.

Each `exec(command)` then runs one cycle:

1. **Bake.** Files uploaded since the last cycle plus a runner enter
   as the boot layer: `/titanium/command.sh` (the command),
   `/titanium/job.sh` (cwd, env exports, the run — as root or via
   `runuser` for a declared user — result capture to
   `/titanium/result/{rc,stdout,stderr}`, then
   `systemctl poweroff`), a oneshot unit, and its enablement
   symlink. Cycle 0 builds its ext4 from the base tar
   (`build_ext4`). Every later cycle is **disk to disk**: the
   previous cycle's evidence copy *is* the next filesystem, and
   `place_into_ext4` writes only the new entries into it -- the
   same placement helpers and refusals, running against a fuse2fs
   mount inside a krun guest. The flavor publishes under
   `$CELLA_HOME/rootfs/` with its manifest.
2. **Run.** `cella create <name> --kernel canonical --rootfs
   <flavor> --mem-mb <task> --net <topology> --root rw`, then
   `cella start`. Titanium waits for the VMM process to exit — a
   completion signal that needs no console, so the field flavor's
   blindness costs nothing.
3. **Collect.** `cella stop`, then harvest (§6): three targeted
   `debugfs` dumps read `/titanium/result/{rc,stdout,stderr}` -- the
   `ExecResult`, and nothing else. The evidence copy is kept as the
   next cycle's filesystem. The machine is destroyed and the cycle's
   flavor removed. Harvest cost does not scale with the guest tree:
   no full-tree pass exists anywhere in the cycle.

State moves forward only as evidence off still disks. Nothing is ever
injected into a machine, live or stopped, so cella's rule — nothing
is installed after boot — holds for every machine, literally.

`upload_file` / `upload_dir` queue entries for the next bake
(last-write-wins; symlinks preserved). `download_file` /
`download_dir` read exactly the asked-for paths from the current
evidence (the base tar before the first cycle, the last evidence
disk after) and never touch a machine.
`stop(delete)` destroys any machine and removes the work directory;
nothing persists but trial evidence.

The cost is stated plainly: a trial with N execs boots N machines and
rebuilds N filesystems. This is the price of the sealed model, paid
where the model says to pay it.

Each boot is one harness command, so the machines of a trial read as
its phases in order. A real-agent trial boots roughly six; the
verifier's machines carry a `-verifier` suffix (from the
`begin_verification` hook), so its crossings read apart from the
agent's:

| Boot               | Command               | Reason                              |
|--------------------|-----------------------|-------------------------------------|
| cycle 0            | `pwd`                 | resolve the agent's working directory |
| cycle 1            | the setup script      | agent install and environment prep  |
| cycle 2            | the agent run         | the agent attempts the task         |
| cycle 3            | `pre_artifacts.sh`    | collect state before grading        |
| cycle 4 (verifier) | `chmod +x` the tests  | verifier prep                       |
| cycle 5 (verifier) | the test script       | grade against evidence              |

The exact count varies with the task -- an agentless oracle trial is
shorter (no agent run; the solution replay stands in), and a task with
no setup or pre-artifacts step drops those boots -- but the shape holds:
setup, run, collect, verify, each a boot, because nothing may exec into
a live machine.

## 5. The smoke: `make smoke-cella-integration`

The cella-*unique* probes — the policy engine and the terminated pair,
which no other rung has — run together through one `titanium run --env
cella` with the oracle agent, and each gates exactly like
`examples/smoke/fix-git-offline`: the task's own offline verifier
writes `/logs/verifier/reward.txt`, read from evidence. Four tasks, two
topologies:

* **airgapped** (`cella-policy-engine-airgapped`,
  `verify-cella-env-airgapped`): `allow_internet = false`, agentless —
  `--net none`. The in-guest probe records that no nic exists (`lo`
  alone — a topology, not a firewall), egress fails, PID 1 is systemd,
  one vCPU, the declared memory.
* **www** (`cella-policy-engine-www`, `verify-cella-env-www`):
  `allow_internet = true` — the terminated pair (§9). The probe
  reaches a granted name (`example.com`) through the appliance, its TLS
  terminated on a pair-CA leaf and the world leg judged by the resolved
  name, and confirms an ungranted name is refused. It times three
  sequential calls, so the membrane-memory warming (§3.2) shows as a
  cold call then two live ones.

`make smoke-cella-all` runs the rootfs proof (§8) and this suite
together. `DRY_RUN=true` flips the appliance engine to collection and
copies each www task's collected `cella.policy` back for review (§3.1).
The rung-parity `smoke-cella` (the shared bench tasks under a real
agent, like `smoke-krun-podman`) is separate future work.

## 6. Evidence collection, and the one judgment call

`cella inspect` is an operator verb: it boots an inspector twin with
the rock disk read-only at `/rock` and attaches the console — which
only the lab flavor has. It is not a programmatic extraction API.

So collection reads the still `disk.img` directly: copy the file out
of `machines/<vm>/` after `cella stop`, then read only the asked-for
paths from the copy (§7). This is the one place titanium touches a machine-directory
artifact instead of a cella verb. What defends it: cella's own
documentation blesses the machine directory as plain files that can
be read; the read happens only on a still machine, only on a copy,
read-only; and it recovers exactly what `inspect`'s evidence view
exists to provide. It is a read of evidence, not a channel into
anything.

Named honestly, it is a cella-unmediated evidence read. The durable
fix is cella-side: a programmatic verb such as
`cella inspect <vm> --dump <guest-path>` emitting a tar stream, where
`--dump /` is the whole rootfs. That form preserves numeric
ownership, covers single-file reads and full-tree export with one
flag, and would put every evidence read on cella's audit record —
better than today, where the direct read leaves no trace in cella's
books. The environment's `_harvest` is a one-function swap when it
lands. Until then, the direct read stays, and mounting *into* a cella
VM is not an alternative: cella has no host-mount device at all (by
design), its `--attach-ro` disk mechanism is not reachable from the
CLI, and a reader VM cannot hand results back without the host
reading a disk in the end anyway.

## 7. Why krun was needed

The disk being read is written by the task's own workload. Ext4
metadata is parser input, and the workload controls every byte of it.
A malicious task can craft a filesystem image that attacks the
program that parses it — filesystem parsers have a long CVE history,
in kernels and in userspace tools alike.

The first implementation parsed the copy with `fuse2fs` on the host.
That put task-controlled bytes through a host-side parser: exactly
the class of exposure this stack exists to remove.

So both directions of untrusted parsing run inside krun microVMs
(`--runtime krun`, the KVM-isolated OCI runtime `make .krun-podman`
provisions — see [KRUN-PODMAN.md](KRUN-PODMAN.md)):

* **Reading** (`_harvest`, `download_*`): targeted `debugfs` dumps
  for single files, and a read-only `fuse2fs` mount for directory
  reads, both inside a krun guest (`--network=none`,
  `--device /dev/fuse`, two bind mounts). A hostile filesystem
  compromises a disposable KVM guest with no network, not the host.
* **Writing** (`place_into_ext4` on the exec-cycle path): from the
  second cycle on, the image the placement edits is guest-produced.
  fuse2fs parses and writes it inside the krun guest; the placement
  script is the same one `build_ext4` uses, with the same refusals.
  Cycle 0 and the converter keep podman's default runtime: their
  input is the task's own build, not a guest's output.

The extractor image is the pinned rootfs builder
(`localhost/titanium-cella-rootfs-builder:2`, alpine + e2fsprogs +
GNU tar + fuse2fs, digest-addressed by id at use): the image that
writes filesystems is the image that reads them. krun is therefore a
hard dependency of this environment — `preflight()` refuses without
it — not an optimization.

## 8. Provisioning and targets

| Target | What it does |
|---|---|
| `make .cella` | Installs cella from the git revision pinned in `runtime.env` (https URL, exact rev): cella's own field installer into `~/.cella/bin`, the canonical kernel golden, a trust-on-first-use digest pin, and `cella doctor gate`. |
| `make .cella-debug` | Builds the lab flavor (console on) in the same pinned clone, for smokes that watch a guest console. The lab never installs; `CELLA_BIN` defaults to it in `smoke-cella-rootfs`. |
| `make .krun-podman` | Provisions krun (§7). |
| `make unit-cella` | The offline unit suite for the whole package, with coverage under `reports/unit/unit-cella/`. No podman, no cella, no network. |
| `make smoke-cella-rootfs` | The conversion acceptance proof: a stock Debian with no init becomes a systemd-bootable ext4 and survives boot → freeze → thaw → stop → archive → destroy, driven by cella's own verbs. |
| `make smoke-cella-integration` | §5. |

The field flavor is blind by design: no console exists. Completion is
the VMM's exit; diagnosis is `vmm.log` and the evidence tree;
liveness on the judged path is the chronicle. Smokes that must watch
a boot use the lab flavor through `CELLA_BIN`.

## Integration Guide

This guide is for you if you add a task to the cella smoke. It walks
through one task, `build-pmars`, from an empty policy to a green run.
The task builds pMARS from Debian source packages. It needs the network
at solve time, so it uses the terminated pair — a good example.

Read sections 1 to 8 first. This guide uses those terms: *member*,
*appliance*, *crossing*, *grant*, *chronicle*.

### G.1 Before you start

Do these steps once on the host.

1. Confirm KVM is present.
```bash
test -c /dev/kvm && echo "kvm ok"
```
2. Provision cella at the pinned revision.
```bash
make .cella
```
`make .cella` clones the revision in `runtime.env`, builds the field
binaries, builds the kernel golden, and builds the terminator golden.
The terminator golden holds this host's pair CA.

3. Confirm the goldens are present.
```bash
ls ~/.cella/rootfs/terminator/   # rootfs.ext4  golden.json  ca.pem
```

### G.2 Know the two knobs

Two knobs control the network. They are independent.

* `allow_internet` in `task.toml` sets the topology. `false` with no
  agent gives `--net none`; the member has no nic. `true`, or any
  agent, gives the terminated pair; the member is a wire-only guest,
  and a terminator appliance holds the world nic.
* `cella.policy` beside the Dockerfile sets the crossing rules, one
  grant per line.

Under the terminated pair, the member reaches the world only through
the appliance. The appliance judges the world leg by the resolved
**name**, not by the ip. So you grant names, not addresses.

### G.3 Add your task

A cella task has this shape.
```text
examples/smoke/build-pmars/
  task.toml
  instruction.md
  environment/
    Dockerfile
    cella.policy       <- you write this (G.4, G.5)
  solution/solve.sh
  tests/test.sh
  tests/test_outputs.py
```

Set `allow_internet = true` in `task.toml`. `build-pmars` fetches apt
packages at solve time, so it needs the world.
```toml
[environment]
allow_internet = true
```

### G.4 Write the network policy

`build-pmars` reaches Debian's apt mirror over plain HTTP. Grant that
name. Write `environment/cella.policy`.
```text
# The world names this task reaches, judged at the appliance.
release outgoing deb.debian.org:80/tcp (keep_open=5m) (skip_freeze=true)
release incoming deb.debian.org:80/tcp
```

Follow these rules.

* Grant the name (`deb.debian.org`), not an ip. CDN ips rotate; the
  name does not.
* An outgoing grant needs its incoming reply twin.
* `keep_open` plants a membrane memory, so the flow waits live.
* `skip_freeze=true` is outgoing only. An incoming park never freezes,
  so an incoming grant carries no window.

Do not grant the wire to the appliance. Titanium adds the member's
fixed grants for you.

### G.5 Collect the policy with a dry run

Do not guess the names. Observe them once, then enforce.

1. Run the task in collection mode.
```bash
make smoke-cella-integration DRY_RUN=true
```
In dry-run, the appliance releases every world crossing and writes each
resolved name to `cella.policy`.

2. Review the collected file. Keep the names the task needs. Delete
background noise, such as a stray resolver or a time-sync attempt. A
collected file looks like this.
```text
release outgoing deb.debian.org:80/tcp (keep_open=5m) (skip_freeze=true)
release incoming deb.debian.org:80/tcp
release outgoing security.debian.org:80/tcp (keep_open=5m) (skip_freeze=true)
release incoming security.debian.org:80/tcp
```

3. Check the file in beside the Dockerfile. It is a lockfile for the
task's network. Its diff is the task's network story.

4. Enforce. Run the smoke again without the flag (G.6). A dry run
proves nothing about enforcement.

### G.6 Run the smoke

Run the suite.
```bash
make smoke-cella-integration
```
The default is one trial at a time (`TITANIUM_N=1`), like the other
rungs. To run trials together, override N.
```bash
make smoke-cella-integration TITANIUM_N=4
```
A pass prints this line.
```text
4/4  Mean: 1.000
```

### G.7 Read the result

Read the reward first. `1` is a pass.
```bash
find .run/jobs/openrouter/smoke-cella-integration -name reward.txt -exec cat {} \;
```

Read the task's report. It carries the warming curve.
```bash
cat .run/jobs/openrouter/smoke-cella-integration/*/build-pmars__*/artifacts/report.json
```

Read the chronicle. Each machine has one directory. `verdict` and
`ledger` are binary; titanium renders each to a `.txt`.
```bash
cat .run/jobs/.../build-pmars__*/cella-chronicle/*/verdict.txt
```

Read the per-machine engine log. Each machine writes its own, so the
solution's crossings stay separate from the verifier's.
```bash
cat .run/jobs/.../cella-engine/<vm-id>/engine.log
```

### G.8 When a crossing is refused

A refused crossing means a name is not granted. Find it in the
appliance engine log.
```bash
grep refuse .run/jobs/.../cella-engine/*-term/engine.log
```
A refusal line looks like this.
```text
cella engine: refuse id=… host='files.pythonhosted.org' ip=… port=443 direction=0
```
Read the `host=` field. That is the name the task reached. Add its
grant to `cella.policy`.
```text
release outgoing files.pythonhosted.org:443/tcp (keep_open=5m) (skip_freeze=true)
release incoming files.pythonhosted.org:443/tcp
```
Run the smoke again (G.6).

### G.9 When it is slow

The first call to a name is slow. Later calls are fast. This is normal.
The first crossing to each new destination freezes once. A membrane
memory then makes the destination live. You see the curve in
`report.json`.
```json
"calls": [{"secs": 16.0}, {"secs": 4.6}, {"secs": 4.6}]
```
The cold call pays the freeze. The two warm calls do not.

The wire itself does not wait. Titanium pre-plants the memory for ARP
and the appliance ports before the first crossing (§3.2). So the wire
comes up at once. You do not tune this.

### G.10 Keep a machine for forensics

By default titanium destroys each machine when it ends. The evidence is
already copied out (G.7), so the machine is not needed.

To keep a machine instead, pass `on_completion=archive`.
```bash
titanium run --env cella --ek on_completion=archive --path <task> ...
```
`archive` stops each machine and latches it as a cella artifact, not a
deleted machine. The archive verb sets the machine `state=archived`: a
*rock*. A rock is frozen for keeps. You do not thaw a rock — thaw
resumes a live frozen machine (the `freeze`/`thaw` pair), and archive
closes that door. You read a rock, and you fork a runnable machine off
it (the `archive`/`inspect` pair, with `branch`/`extract`):
```bash
cella list                    # find the archived machine (state=archived)
cella inspect <machine>       # read the rock read-only (lab flavor: console at /rock)
cella branch  <machine> <new> # fork a fresh, runnable machine off the rock
cella extract <machine> <path># copy a file out of the rock
```
Use this to hold a run for a human to inspect — for example, an agent
that reached an ungranted name. `inspect` opens the rock without
altering it; `branch` is the only way back to a running machine, and it
leaves the rock itself untouched. The default, `teardown`, deletes the
machine.

`archive` keeps `disk.img` and `ram.img` per machine (gigabytes each).
Use it for a targeted run, not a routine smoke.

## 9. Limitations and future work

* **One vCPU.** Every cella machine runs a single vCPU. The
  environment declares `cpu_limit = false`: a requested ceiling
  cannot be honored as asked. Memory is enforced (`--mem-mb`).
* **The exec cycle is expensive** (§4). Do not put chatty
  many-exec flows on this rung; the rung exists for sealed runs.
* **`cella.policy` is not compiled from task URLs yet.** The
  allowlist-from-URLs derivation other rungs use has no cella
  translation; dry-run collection is the current authoring path.
* **The evidence read is cella-unmediated** (§6) until a
  `cella inspect --dump` verb exists.
* **A pure `--net none` airgap is agentless.** A real agent is baked
  into the guest and needs its inference line, so an agented task
  always stands the terminated pair -- the appliance's world leg
  carries only the inference host, so the task workload stays
  airgapped while the agent resolves its API. Only an *agentless*
  airgapped trial (an oracle replay) boots `--net none` with no
  appliance at all.
* **Windows tasks are unsupported.** The rootfs conversion is a
  Linux systemd story.

## Decisions

Opinionated defaults, with the reason each was chosen. A default here
is a deliberate ruling, not an accident; change one only against the
reason recorded with it. The values themselves live in one checked-in
place -- `src/titanium/environments/cella/config.py` -- so the knobs are
visible and greppable, each with its reason inline.

### The member border holds every hop live for 24h

The member's grants to its appliance -- ARP, `443` (https and the
agent's inference line), `80` (apt), `53` (the interceptor's DNS) --
all carry `keep_open=24h` with `skip_freeze=true`.

The window governs *freeze frequency*, not reach. Each of these is the
member-to-appliance plumbing hop, not a world crossing: the world name
is resolved and judged on the *appliance's* border, so a long window on
the member authorizes nothing new. What it buys is the absence of a
re-freeze. The engine pre-plants the memory for each concrete
destination at stream open, so the first crossing already waits live
and never freezes; a finite window then lapses mid-run, and the next
crossing to that hop parks with no live memory and freezes -- and every
freeze pays a full cryogenic thaw (a 2 GB guest re-warms in ~20-28 s).
A real agent run makes DNS and inference crossings for minutes, so a
short window (the earlier `90s` DNS, `5m` https) lapsed repeatedly and
the hot path re-froze again and again; one build-pmars run froze ten
times, about four minutes of pure re-warming, and exceeded its exec
budget.

24h -- effectively the machine's whole life, and the same window ARP
already used -- keeps the pre-planted memory from ever lapsing, so the
member freezes only when it is genuinely blocked on an upstream
decision, never on its own plumbing. This applies to every cella task.

The rule does **not** extend to the appliance's *world-host* windows.
Those gate real world egress and real re-judgment, so they stay a
deliberate, shorter knob -- a different border, a different rule.

### The exec budget comes from the task, not a constant

A cella exec is a whole VM boot, run, and collect cycle. Because the
guest command runs on a background thread that cannot be cancelled from
outside, cella must bound each exec itself; the outer timeout the harness
applies at the trial layer cannot stop a guest that has already begun.
The bound is therefore taken from the task's own declaration, not from a
figure fixed in cella.

The per-exec budget resolves in the following order, and the first value
that is set is used:

1. An explicit `timeout_sec` passed to the exec. This is a per-call
   override and is normally absent, since the harness bounds the agent
   and verifier at the trial layer rather than per command.
2. The task's declared timeout for the current phase: `[verifier]
   timeout_sec` once verification has begun (see `begin_verification`),
   and `[agent] timeout_sec` otherwise. This value is read from
   `task.toml`, resolved with any multiplier, and passed to the
   environment. It is the canonical source.
3. `CELLA_EXEC_TIMEOUT` (`config.py`). A last-resort ceiling, used only
   when a task declares no timeout for the phase. It exists so that a
   hung guest is still bounded, rather than leaking a live machine and a
   thread; it is not the normal path.

The chosen value is then extended by a fixed boot margin, which covers
the guest's boot and halt around the command itself.

The intent is that a task states its own time budget and cella honours
it. A task that needs longer raises its `[agent]`/`[verifier]
timeout_sec`; nothing in cella needs to change. The constant is the
floor beneath tasks that state nothing, never the figure a task is held
to.
