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

Titanium's engine is the judge on the far end of that dial:
`python -m titanium.environments.cella.engine --listen HOST:PORT
--policy cella.policy [--dry-run]`. It is pure gRPC. It opens no
files in `CELLA_HOME`, invokes no cella verb, and touches no book.
The message codec (`wire.py`) is hand-written and byte-verified
against `protoc` output from cella's own proto file.

The one policy file travels in two directions:

* **Enforce** (default): read `cella.policy`, release granted
  crossings, refuse everything else with the why on the record
  (`no cella.policy grants this crossing`). No file grants nothing:
  fail closed.
* **Dry run** (`--dry-run`): release every crossing and *write* each
  distinct one to `cella.policy` as a grant. Review the collected
  file and check it in beside the Dockerfile, like a lockfile. The
  next run enforces it. `make smoke-cella-policy-engine DRY_RUN=true`
  will regenerate a task's policy this way (the `-www` leg).

There are no unconditional releases in enforce mode. Cella's own
motor fixture lets ARP ride free; this engine does not. ARP, NDP,
and every finer exception belong to `cella.policy`, not to hardcoded
carve-outs. Every refusal lands in the chronicle, so a task that
needed a crossing shows exactly what it asked for. That record —
every crossing the workload attempted, including the refused ones —
is a property no other rung has.

### 3.1 How to collect a policy with --dry-run

Do not write a `cella.policy` from guesswork. Observe once, review,
then enforce forever:

1. **Run the task in collection mode.** For the smoke task:
   `make smoke-cella-policy-engine-www DRY_RUN=true`. For any task:
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
rebuilds N filesystems. The oracle-plus-verifier flow of the smoke
task is 3–4 cycles. This is the price of the sealed model, paid where
the model says to pay it.

## 5. The smoke: `make smoke-cella-policy-engine`

Two legs, one at a time. Both run through `titanium run --env cella`
with the oracle agent, and both gate exactly like
`examples/smoke/fix-git-offline`: the task's own offline verifier
writes `/logs/verifier/reward.txt`, read from evidence.

* **`smoke-cella-policy-engine-airgapped`** (landed):
  `examples/smoke/cella-policy-engine-airgapped` declares
  `allow_internet = false`. The in-guest probe records that no nic
  exists (`lo` alone — a topology, not a firewall), egress fails,
  PID 1 is systemd, one vCPU, the declared memory. The proof is the
  sealed loop end to end under `titanium run`.
* **`smoke-cella-policy-engine-www`** (not yet implemented; the
  sub-target says so and exits 2): `allow_internet = true`, the
  judged world nic, the engine enforcing the task's checked-in
  `cella.policy`, and the chronicle carrying the refusals.

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
| `make smoke-cella-policy-engine` | §5. |

The field flavor is blind by design: no console exists. Completion is
the VMM's exit; diagnosis is `vmm.log` and the evidence tree;
liveness on the judged path is the chronicle. Smokes that must watch
a boot use the lab flavor through `CELLA_BIN`.

## 9. Limitations and future work

* **One vCPU.** Every cella machine runs a single vCPU. The
  environment declares `cpu_limit = false`: a requested ceiling
  cannot be honored as asked. Memory is enforced (`--mem-mb`).
* **The exec cycle is expensive** (§4). Do not put chatty
  many-exec flows on this rung; the rung exists for sealed runs.
* **The `-www` leg is not implemented.** `allow_internet = true`
  raises `NotImplementedError` in the environment until the judged
  world nic, the engine wiring, and the `cella.policy` staging land.
* **`cella.policy` is not compiled from task URLs yet.** The
  allowlist-from-URLs derivation other rungs use has no cella
  translation; dry-run collection is the current authoring path.
* **The evidence read is cella-unmediated** (§6) until a
  `cella inspect --dump` verb exists.
* **Airgapped cella tasks are oracle-only, by construction.** On the
  exec-model rungs the agent process lives host-side and reaches its
  inference API from there, so airgapped tasks still get real agents.
  On this rung an agent would have to live inside the sealed guest,
  and a `--net none` guest can reach no API at all. A real agent on
  cella needs the `-www` leg and a `cella.policy` granting the
  inference endpoints.
* **Windows tasks are unsupported.** The rootfs conversion is a
  Linux systemd story.
