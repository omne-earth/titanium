# The cella rung, on disk — an operator's guide

[docs/environments/CELLA.md](docs/environments/CELLA.md) tells you how
the rung works. This guide tells you what a cella trial writes to
disk: each `cella-*` folder and file under `.run/`, the machine names,
and which entries appear for which type of run. Each path below comes
from a real trial with reward 1.0.

## 1. Run a trial

```bash
# oracle: replays the task's own solution/ -- deterministic, no model
make titanium-run TITANIUM_ENV=cella TITANIUM_AGENT=oracle \
  TITANIUM_TASK=examples/smoke/verify-cella-env-www

# real agent: the bench smoke (fix-git-offline airgapped, build-pmars online)
make smoke-cella

# policy collection: release each crossing, record it, copy the policy back
make smoke-cella DRY_RUN=true TITANIUM_AGENT=oracle
```

The trial is two machines. The **member** runs the agent's whole
turn — setup, then the agent (or the oracle's solve) — with no tests
and no collect script aboard, and ends itself with a forced reset.
Its full state is then extracted as a tar (cella's own verb) and
rebaked, with the tests and `collect.sh`, into the **verifier**,
which collects, grades, and resets. The agent and its
graders never coexist. An oracle trial and an agent trial write the
same records; they differ only in the `agent/` folder (§4) and the
payload the agent phase runs.

## 2. Orchestration

No process ever enters a machine: each machine boots with its whole
program aboard and runs it under a systemd oneshot
(`titanium-trial.service`). The scripts are checked-in templates —
`src/titanium/environments/cella/scripts/`, verb-noun named —
and titanium fills only their `{{TOKENS}}` at bake:

Both machines get their own family of these — the member's is baked
from the task image with the member phases (setup, payload), the
verifier's is baked from the member's extracted state with the
grader phases (collect, verify):

| Template | Baked as (in-guest) | Machine | What it does |
|---|---|---|---|
| `orchestrate-trial.sh` | `/titanium/orchestrator.sh`, 0700 root | both, each its own | the state machine: runs each phase under its budget, writes the done marker, ends the machine with a forced reset — the completion signal the host observes |
| `run-phase.sh` | inlined per phase | both | mkdir/touch the phase's result dir, run it under `timeout`, record rc 124 when the budget ends it |
| `run-steps.sh` | `/titanium/phases/<phase>.sh`, 0700 root | both, each its own phases | the phase's steps in order; a failing step ends its phase. Carries the merged env exports — root-only because the inference key lives here |
| `invoke-step.sh` | inlined per step | both | the runuser + stdout/stderr/rc capture around one step |
| `fold-results.sh` | inlined | verifier only | copies the result tree under `/logs/titanium-result` so one extract reads everything |
| `titanium-trial.service` | `/etc/systemd/system/`, 0600 root | both | the oneshot unit that starts the orchestrator on boot |

Each step's raw command lands at `/titanium/steps/<phase>-<n>.sh`
(0444: a non-root agent user must read its own command; no secrets
there). `/titanium/task-type` (0444) and `environment/sudoers` are
baked into the **member only** — the verifier inherits them through
the state tar, like everything else the member's disk carried; its
own bake adds only the tests, `collect.sh`, and its script family.
The member's orchestrator writes `/titanium/result`; the verifier's
writes `/titanium/result-verifier`, so the member's carried done
marker cannot trip the verifier's re-entry guard.

The payload never runs as root by omission: every rootfs bakes the
standard user `titanium` at image build (`useradd -m`, a no-op when
the image ships it), and an agent phase whose task declares no user
runs as `titanium` — root is a decision a task writes down
(`agent.user = "root"`), never inherited. The orchestrator hands the
payload user its writable surfaces (the workdir, `/logs/agent`)
before any phase runs. A task declares any elevation itself, in
`environment/sudoers` beside its Dockerfile — baked verbatim to
`/etc/sudoers.d/titanium-agent`, 0440 root. No file, no elevation.

## 3. The trial directory

```
.run/jobs/<backend>/<job>/<timestamp>/<task>__<trial>/
├── agent/               # what the agent did (§4)
├── artifacts/           # files extracted from the machine, per task.toml
├── cella-chronicle/     # cella's tamper-evident record, one folder per machine (§6)
├── cella-engine/        # titanium's judge, one folder per machine (§7)
├── cella-policy/        # the composed borders, as enforced (§8)
├── cella-env-<id>/      # work area, kept as part of the record: the build
│                        #   context, the base tar the machine was made from,
│                        #   and the extracted evidence caches
├── verifier/            # reward.txt, test-stdout.txt, ctrf.json
├── config.json          # the trial's full configuration
├── result.json          # reward, timings, exception info
├── trial.log            # titanium's own trial log
└── exception.txt        # written only when the trial raised
```

Which entries appear when:

| Entry | Airgapped, agentless (`--net none`) | Terminated pair (egress, or any agent) |
|---|---|---|
| `cella-chronicle/` | yes — the member alone | yes — the member and the appliance |
| `cella-engine/` | **no** — no nic, no crossings, no judge | yes — one folder per machine |
| `cella-policy/` | **no** — no border to compose | yes — `appliance.policy`, `member.policy` |

Use this table as the first check. An agentless airgapped trial with a
`cella-engine/` folder is wrong. A paired trial without one is wrong.

Note the timing: the record is live. The `cella-*` folders appear
when their machine does, and the host-side books — the chronicle
files, `edge.log`, `engine.log` — mirror into the trial dir once a
second while a machine runs. Only the still-disk evidence — the
member's state tar, the verifier's output, the `.txt` decodes —
lands when its machine is still and `cella extract` reads it.

## 4. `agent/`

- **Oracle run**: `oracle.txt` holds the solution script's stdout and
  stderr. `exit-code.txt` appears when the agent phase exited nonzero.
- **Agent run** (for example mini-swe-agent): the agent's trajectory
  files, extracted from `/logs/agent` at trial end.

The agent phase's own stdout and stderr also land in the phase
result, extracted with the member's state. A phase killed by its
in-guest budget records `rc` 124; the trial then records the timeout
and still grades what the payload left — in the verifier, which
never boots until the member is still.

## 5. Machine types and what each is baked with

One trial is at most three machines. Each name starts with the
session id (the task and trial), sanitized to lowercase, digits, and
dashes:

| Machine | Count | What it is |
|---|---|---|
| `<session>` | 1 | the member: the agent's turn — setup, payload, reset. No tests, no collect script aboard. |
| `<session>-verifier` | 1 | the grader: baked from the member's extracted state plus the tests and `collect.sh`; collects, grades, folds its results under `/logs`, resets |
| `<session>-appliance` | 1, paired trials only | the terminator: holds the world leg; freezes on each park, thaws on each verdict, for the trial's whole life |
| `<session>-extractor`, `<session>-verifier-extractor` | transient | `cella extract`'s twins — the member's full-state read and the verifier's results read; cella destroys them |

What each image is made from, and what titanium bakes into it:

| Machine | Image source | Titanium bakes |
|---|---|---|
| `<session>` | the task's Dockerfile, staged (`FROM` qualified, agent install and the `titanium` user appended), built, exported as a tar | the orchestrator family (§2, member phases), `titanium-trial.service`, the agent's config and uploads (the oracle's `solution/`), `/titanium/task-type`, `environment/sudoers` when the task ships one, and the pair CA trust when paired |
| `<session>-verifier` | the member's full-state tar, exactly as extracted | its own orchestrator family (§2, collect + verify phases, result root `/titanium/result-verifier`, the `/logs` fold), the task's `tests/`, `/titanium/collect.sh`, and the pair CA trust when paired. Everything else — the agent's work, `task-type`, the sudoers grant — arrives through the state tar |
| `<session>-appliance` | cella's terminator golden, booted directly | **nothing** — its init writes its own conf at boot; titanium composes borders against those defaults |
| the extractors | cella's own stock rootfs | nothing — they are cella's verb, not titanium's bake |

Five boots paired, four airgapped-agentless — exactly. There is no
cycle counter and no harness prefix; the session id alone names the
trial. The name budget is deliberate — cella caps names at 64, and
the extractor suffix (10) must fit past `-verifier`.

## 6. `cella-chronicle/<machine>/` — cella's record

Cella's tamper-evident chronicle, mirrored live while the machine
runs and made final when it ends. Each binary book gets a `.txt`
decode beside it at trial end (cella's own `--dump`). Read the
`.txt`. Keep the binary as the evidence.

| File | What it is |
|---|---|
| `audit` / `audit.txt` | each verb each persona ran against this machine, `host_ns`-stamped. The trial's spine. |
| `verdict` / `verdict.txt` | the decisions applied to this machine's crossings — what cella enforced, replayed on every thaw. |
| `membrane-memory` / `.txt` | the standing memories planted. **Absent on a paired machine = nothing was planted**; expect it slow or wedged. |
| `vmm.log` | boot, parks, freeze and thaw timings — and at the end, a forensic line (`exit=` token, rip, exception state) then `cella: guest requested shutdown`. `exit=reset` = the guest reached the reset vector: the orchestrator's own end **or** a crash whose triple fault completed — the state tar's `done` marker is the discriminator. `exit=fault` = a death that never reached reset. No final lines at all = the VMM was killed from outside. |
| `network/ledger` / `ledger.txt` | every crossing with its resolved name, bytes, and `host_ns`. Measure throughput from the gaps. |
| `network/names` / `names.txt` | the names the appliance resolved and stamped. The fastest check of what the task reached. |
| `manifest.json`, `uid`, `valve` | the chronicle's integrity manifest; the machine's sub-uid; the valve's final position. |

## 7. `cella-engine/<machine>/` — titanium's judge

Paired trials only; one folder each for the member and the appliance:

- **`engine.log`** — the in-process judge, written live during the
  run (1-second drain): pre-planted memories, each judged crossing,
  and at stream end the throughput line
  (`cella_engine: N verdicts, avg … ns, max … ns`). Healthy is tens
  of microseconds average.
- **`edge.log`** — cella's bridge and gateway record, mirrored live
  like the chronicle.

## 8. `cella-policy/` — the borders as enforced

- **`member.policy`** — the member's fixed border: wire-plane ARP and
  the appliance's three ports. Titanium composes it.
- **`appliance.policy`** — the world leg: ARP, the resolver, the
  reply-port window, and one windowed grant per world name from the
  task's `environment/cella.policy` plus the agent's allowlist.

The appliance itself boots cella's terminator golden directly;
titanium injects nothing. The golden's init writes its own conf at
boot, and its defaults are the constants titanium's borders are
composed against (`constants.py`, pinned by test).

## 9. Dry run: collect a policy

Do not write a `cella.policy` from guesswork. Observe once, review,
then enforce.

A dry run collects the policy a normative run requires, and that
collection is the baseline. The baseline serves two purposes past the
first green run. First, detection: in enforce mode every crossing
outside the baseline is refused and lands in the chronicle, so a
later benchmark run that behaves non-normatively shows itself in the
refusal record — the membrane is an anomaly detector, not only a
gate. Second, ablation: a committed policy pins the task's network
contract, so runs that compare models, agents, or prompts differ only
in the variable under study.

**Run it.** This applies to any task with world egress. Write the
task first, leave `environment/cella.policy` absent or empty, and
collect in two passes. Successive dry runs accumulate into one file:
each recorder seeds from the last collection.

Pass one, the oracle — deterministic, and it drives the verify phase,
whose test harness has world egress of its own:

```bash
titanium run --env cella --ek dry_run=true --agent oracle --path path/to/your-task
# the checked-in smokes have a make convenience for the same thing:
make smoke-cella DRY_RUN=true TITANIUM_AGENT=oracle
```

Pass two, the real agent — a model can solve by a different path and
reach hosts the oracle never touched. Each run adds only what is new:

```bash
titanium run --env cella --ek dry_run=true --path path/to/your-task
```

**Groom the collected file.** The collection is an observation, not a
judgment:

1. Keep only the world names. Delete the appliance's own plumbing —
   the reply-window ports, the upstream resolver, `arp`. Titanium
   composes those grants itself (§8).
2. Add the windows: give each kept `outgoing` grant
   `(keep_open=60m) (skip_freeze=true)`, keep its bare `incoming`
   twin. A bare outgoing grant freezes the machine on **every**
   crossing in enforce mode.
3. Write a header that says what the task fetches and why.
   `examples/smoke/cella/build-pmars/environment/cella.policy` is the
   worked example; CELLA.md §3.1 has the full review steps.

**Then stop dry-running.** Commit the groomed file beside the task's
Dockerfile, like a lockfile, and run without the flag. Enforce mode
is the proof. A later dry run keeps your windows but re-collects the
infra grants you stripped — after any re-collection, groom again.

**What the file does not need.** `cella.policy` declares the *task's*
egress only. The agent's inference and install line comes from the
agent's allowlist, composed into the appliance border on every trial.
Do not add it.

## 10. Watching a live guest

The field flavor is blind by design: no console exists, and nothing
can enter a machine. To watch a boot or a wedge, rerun the trial
under the **lab flavor** — the same pinned cella, console on:

```bash
make .cella-debug   # builds the lab binary in the pinned clone
CELLA_BIN=$HOME/.cache/titanium/cella-src/target/lab/cella \
  make titanium-run TITANIUM_ENV=cella TITANIUM_TASK=path/to/task ...
```

Under the lab flavor the machine dir grows a `console.log` (mirrored
live into the trial dir like the other books), and
`cella enter <machine>` attaches the console of a running machine
interactively. The lab flavor is a debugging instrument: a trial run
under it is an observation, never the graded record — the smokes and
the benchmarks run the field flavor.

## 11. Where to look, by question

| Question | Read |
|---|---|
| Why was a crossing refused? | the machine's `verdict.txt`, then `engine.log` for the judged name, then the task's `cella.policy` for the missing grant |
| What did the task reach? | the appliance's `names.txt`, then `ledger.txt` for the bytes |
| Why is it slow? | the `host_ns` gaps in `ledger.txt` (~1 ms is healthy), and whether `membrane-memory.txt` exists |
| Did the trial complete on its own? | the member's `vmm.log` forensic line **plus** the state tar: `exit=reset` with `titanium/result/done` present = clean; `exit=reset` without `done` = a crash that completed its triple fault; `exit=fault` = died before reset; no final lines = ended from outside (budget or kill) |
| Which phase failed? | `exception.txt` and `result.json`; a phase's `rc` 124 is its in-guest budget |
| What was the machine made from? | `cella-env-<id>/` — the build context and the base tar, kept for every trial |
