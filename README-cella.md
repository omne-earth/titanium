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

The whole trial is one boot. A guest orchestrator runs the phases —
setup, agent (or the oracle's solve), collect, verify — writes each
phase's result, and ends the machine with a forced reset. An oracle
trial and an agent trial write the same records; they differ only in
the `agent/` folder (§3) and the payload the agent phase runs.

## 2. The trial directory

```
.run/jobs/<backend>/<job>/<timestamp>/<task>__<trial>/
├── agent/               # what the agent did (§3)
├── artifacts/           # files extracted from the machine, per task.toml
├── cella-chronicle/     # cella's tamper-evident record, one folder per machine (§5)
├── cella-engine/        # titanium's judge, one folder per machine (§6)
├── cella-policy/        # the composed borders, as enforced (§7)
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
second while the experiment runs. Only the still-disk evidence —
results, artifacts, the verifier's output, the `.txt` decodes — lands
at trial end, when `cella extract` reads the halted machine.

## 3. `agent/`

- **Oracle run**: `oracle.txt` holds the solution script's stdout and
  stderr. `exit-code.txt` appears when the agent phase exited nonzero.
- **Agent run** (for example mini-swe-agent): the agent's trajectory
  files, extracted from `/logs/agent` at trial end.

The agent phase's own stdout and stderr also land in the phase result
(`/titanium/result/agent/` in the guest, extracted at collection). A
phase killed by its in-guest budget records `rc` 124; the trial then
records the timeout and still grades what the payload left.

## 4. Machine names

One trial is at most three machines. Each name starts with the
session id (the task and trial), sanitized to lowercase, digits, and
dashes:

| Machine | Count | What it is |
|---|---|---|
| `<session>` | 1 | the member: the experiment itself — orchestrator, phases, reset |
| `<session>-appliance` | 1, paired trials only | the terminator: holds the world leg; freezes on each park, thaws on each verdict, for the trial's whole life |
| `<session>-extractor` | transient | `cella extract`'s twin, one per evidence read; cella destroys it, nothing is preserved from it |

There is no cycle counter and no harness prefix: one boot runs the
whole trial, so the session id alone names it. The name budget is
deliberate — cella caps names at 64, and the extractor suffix (10)
must fit.

## 5. `cella-chronicle/<machine>/` — cella's record

Cella's tamper-evident chronicle, mirrored live while the machine
runs and made final when it ends. Each binary book gets a `.txt`
decode beside it at trial end (cella's own `--dump`). Read the
`.txt`. Keep the binary as the evidence.

| File | What it is |
|---|---|
| `audit` / `audit.txt` | each verb each persona ran against this machine, `host_ns`-stamped. The trial's spine. |
| `verdict` / `verdict.txt` | the decisions applied to this machine's crossings — what cella enforced, replayed on every thaw. |
| `membrane-memory` / `.txt` | the standing memories planted. **Absent on a paired machine = nothing was planted**; expect it slow or wedged. |
| `vmm.log` | boot, parks, freeze and thaw timings — and at a healthy end, `cella: guest requested shutdown`: the orchestrator's reset exiting the VMM. A vmm.log without that line is a trial that did not complete on its own. |
| `network/ledger` / `ledger.txt` | every crossing with its resolved name, bytes, and `host_ns`. Measure throughput from the gaps. |
| `network/names` / `names.txt` | the names the appliance resolved and stamped. The fastest check of what the task reached. |
| `manifest.json`, `uid`, `valve` | the chronicle's integrity manifest; the machine's sub-uid; the valve's final position. |

## 6. `cella-engine/<machine>/` — titanium's judge

Paired trials only; one folder each for the member and the appliance:

- **`engine.log`** — the in-process judge, written live during the
  run (1-second drain): pre-planted memories, each judged crossing,
  and at stream end the throughput line
  (`cella_engine: N verdicts, avg … ns, max … ns`). Healthy is tens
  of microseconds average.
- **`edge.log`** — cella's bridge and gateway record, mirrored live
  like the chronicle.

## 7. `cella-policy/` — the borders as enforced

- **`member.policy`** — the member's fixed border: wire-plane ARP and
  the appliance's three ports. Titanium composes it.
- **`appliance.policy`** — the world leg: ARP, the resolver, the
  reply-port window, and one windowed grant per world name from the
  task's `environment/cella.policy` plus the agent's allowlist.

The appliance itself boots cella's terminator golden directly;
titanium injects nothing. The golden's init writes its own conf at
boot, and its defaults are the constants titanium's borders are
composed against (`constants.py`, pinned by test).

## 8. Dry run: collect a policy

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
   composes those grants itself (§7).
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

## 9. Where to look, by question

| Question | Read |
|---|---|
| Why was a crossing refused? | the machine's `verdict.txt`, then `engine.log` for the judged name, then the task's `cella.policy` for the missing grant |
| What did the task reach? | the appliance's `names.txt`, then `ledger.txt` for the bytes |
| Why is it slow? | the `host_ns` gaps in `ledger.txt` (~1 ms is healthy), and whether `membrane-memory.txt` exists |
| Did the trial complete on its own? | `cella: guest requested shutdown` at the end of the member's `vmm.log`; absent = the budget ended it |
| Which phase failed? | `exception.txt` and `result.json`; a phase's `rc` 124 is its in-guest budget |
| What was the machine made from? | `cella-env-<id>/` — the build context and the base tar, kept for every trial |
