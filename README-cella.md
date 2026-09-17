# The cella rung, on disk — an operator's guide

[docs/environments/CELLA.md](docs/environments/CELLA.md) tells you how
the rung works. This guide tells you what a cella trial writes to
disk: each `cella-*` folder and file under `.run/`, the meaning of
each machine-name suffix, and which entries appear for which type of
run. Each path below comes from a real trial with reward 1.0.

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

An oracle trial and an agent trial write the same cella records. They
differ in two places only: the contents of the `agent/` folder (§3)
and the number of machine cycles. An agent's setup scripts and
multi-step work each cause their own boots (§4).

## 2. The trial directory

```
.run/jobs/<backend>/<job>/<timestamp>/<task>__<trial>/
├── agent/               # what the agent did (§3)
├── artifacts/           # files collected out of the machine, per task.toml
├── cella-chronicle/     # cella's tamper-evident record, one folder per machine (§5)
├── cella-engine/        # titanium's judge, one folder per machine (§6)
├── cella-policy/        # the composed borders, as enforced (§7)
├── cella-env-<id>/      # work area: build context, rootfs tars, freeze
│                        #   states. It exists while the trial runs. A clean
│                        #   teardown removes it. It stays when a trial dies.
│                        #   Its presence in a finished trial is a symptom.
├── verifier/            # reward.txt, test-stdout.txt, ctrf.json
├── config.json          # the trial's full configuration
├── result.json          # reward, timings, exception info
├── trial.log            # titanium's own trial log
└── exception.txt        # written only when the trial raised
```

Which entries appear when:

| Entry | Airgapped task (`--net none`) | Task with egress (terminated pair) |
|---|---|---|
| `cella-chronicle/` | yes — member machines only | yes — members and the appliance |
| `cella-engine/` | **no** — no nic, no crossings, no judge | yes — one folder per machine |
| `cella-policy/` | **no** — no border to compose | yes — `appliance.policy`, `member.policy` |

Use this table as the first check. An airgapped trial with a
`cella-engine/` folder is wrong. An egress trial without one is
wrong. You do not have to read a log to see either fault.

## 3. `agent/`

- **Oracle run**: `oracle.txt` holds the solution script's stdout and
  stderr. `exit-code.txt` appears when the script exited nonzero.
  Nothing else appears.
- **Agent run** (for example mini-swe-agent): the agent's trajectory
  files (`trajectory.json`, `<agent>.trajectory.json`, `<agent>.txt`)
  and a `setup/` folder from its install phase.

An **empty** `agent/` in a finished trial means the agent-phase exec
did not return. The timeout path then skipped the log download. Start
at `exception.txt` and the last machine's chronicle.

## 4. Machine names and suffixes

A command is one boot. A trial is therefore a sequence of machines.
Each machine has the name `titanium-<task>-<trial>-c<NNNN>-<phase>`.
The counter gives the order. The phase names who booted it:

| Suffix | Booted by | When it appears |
|---|---|---|
| `-setup` | the agent's install and setup scripts | agent runs only; oracle runs have no setup boots |
| `-agent` | the agent phase — each exec, or the oracle's solve | always |
| `-collect` | the task's `pre_artifacts.sh` | only when the task ships one |
| `-verify` | the verifier's execs | always |
| `-appliance` | the terminator appliance (`c000-appliance`) | egress tasks only — **one** machine for the whole trial; cella freezes and thaws it across every cycle, so its chronicle spans all phases |

A green bench trial shows this sequence:
`c0000-setup, c0001-setup, c0002-agent, c0003-agent, c0004-verify,
c0005-verify, c000-appliance`. The oracle run of the same task has no
`-setup` cycles.

## 5. `cella-chronicle/<machine>/` — cella's record

Cella's tamper-evident chronicle, copied out per machine. Each binary
record has a `.txt` decode beside it. Read the `.txt`. Keep the
binary as the evidence.

| File | What it is |
|---|---|
| `audit` / `audit.txt` | each verb each persona ran against this machine — `audit verb=start args=[...] uid=... persona=cella-machine host_ns=...`. This is the trial's spine. All timestamps here are `host_ns` (host nanoseconds), uniform across every log in this guide. |
| `verdict` / `verdict.txt` | the decisions applied to this machine's crossings, one `release id=...` or `refuse id=...` per operation. This is what cella enforced. Cella replays the file into the machine on every thaw. |
| `membrane-memory` / `membrane-memory.txt` | the standing memories planted (`memory ip=... skip_freeze=true keep_open=... written=...`). **An absent file means nothing was planted.** On an egress machine that means every crossing froze it one-shot. Expect that machine to be slow or wedged. |
| `vmm.log` | the VMM's own log: boot, `valve Open/Closed`, `parked egress/ingress ...`, freeze and thaw timings, `applying N decision(s) from the verdict file`. A healthy airgapped machine writes two lines (valve, booting). Quiet is normal there. |
| `network/ledger` / `ledger.txt` | every crossing: `parked id=... dir=... ip=... port=... host=<resolved name> guest_ns=... host_ns=...`, then `released id=... bytes_in=... bytes_out=...`. Measure throughput from the gaps between consecutive `host_ns` values. |
| `network/names` | the resolved world names the appliance stamped (binary; read it with `strings`). This is the fastest check of what the task reached. |
| `manifest.json` | the chronicle's own integrity manifest. |
| `uid` | the machine's sub-uid offset — the throwaway account it ran jailed as. |
| `valve` | the network valve's final position (`open` or `closed`). |

## 6. `cella-engine/<machine>/` — titanium's judge

One folder per machine on egress tasks. Two logs. Both carry
`host_ns` stamps:

- **`edge.log`** — cella's bridge (`cella_network:` lines): the edge
  comes up, wires connect, the stream to the engine opens.
- **`engine.log`** — titanium's in-process judge (`cella_engine:`
  lines): the pre-planted memories at stream open, each judged
  crossing, and at stream end the throughput line:

  ```
  host_ns=... cella_engine: 15099 verdicts, avg 36814 ns, max 2876582 ns
  ```

  The judge must stay well under cella's ~1 ms park delivery. An
  average in the tens of microseconds is the healthy reading.

## 7. `cella-policy/` — the borders as enforced

The two composed border files, kept verbatim as evidence:

- **`member.policy`** — the member's border, the same for every task:
  wire-plane ARP and the appliance's three ports (443/80/53).
  Titanium composes it. The task never writes it.
- **`appliance.policy`** — the world leg: ARP, the upstream resolver,
  the member's reply-port window, and one windowed grant per world
  name from the task's `environment/cella.policy`.

The task's own `cella.policy` (beside its Dockerfile) is the input.
These two files are the output that the engine served. A dry run
rewrites the staged task's `cella.policy` live as it collects. The
smoke targets copy it back to the example for review (see CELLA.md
§3.1 for the review steps).

## 8. Dry run: collect a policy

Do not write a `cella.policy` from guesswork. Observe once, review,
then enforce.

**Run it.** This applies to any task you bring to the rung — a new
bench task, a future dataset, anything with world egress. Write the
task first (Dockerfile, `task.toml`, `solution/`, `tests/`) and leave
`environment/cella.policy` absent or empty. Then collect in two
passes. Successive dry runs accumulate into one file: each recorder
seeds from the collection the last one wrote.

Pass one, the oracle. It is deterministic, and it drives the full
trial, so it collects the solution's egress and the verifier's (the
test harness has world egress of its own):

```bash
titanium run --env cella --ek dry_run=true --agent oracle --path path/to/your-task
# the checked-in smokes have a make convenience for the same thing:
make smoke-cella DRY_RUN=true TITANIUM_AGENT=oracle
```

Pass two, the real agent. A model can solve the task by a different
path than the solution and reach hosts the oracle never touched.
This pass adds those paths to the same file:

```bash
titanium run --env cella --ek dry_run=true --path path/to/your-task
# or: make smoke-cella DRY_RUN=true
```

One agent run samples one path. Run pass two more than once when you
want wider coverage; each run adds only what is new.

**What to expect.** The engine releases every crossing and records
each distinct one as a grant. The engine also plants a `skip_freeze`
memory per outgoing destination, so a dry run does not freeze on
every frame. The task's `environment/cella.policy` is rewritten live
as grants arrive — read it during the run to watch the collection
grow. A trial that dies midway keeps what it observed. With the
direct `titanium run` form the collection lands in your task folder
itself. The make smokes stage a copy of each task first, so they also
copy the collected file back to the example and print
`collected <task>/cella.policy copied back`. The reward is still
judged; a dry run with reward 0 collected from an incomplete trial —
treat its policy as incomplete too.

**Groom the collected file.** The collection is an observation, not a
judgment:

1. Keep only the world names. Delete the appliance's own plumbing —
   the reply-window ports (`10.77.0.2:5xxxx`), the upstream resolver
   (`9.9.9.9:53`), and `arp`. Titanium composes those grants itself
   (§7).
2. Add the windows. Give each kept `outgoing` grant
   `(keep_open=60m) (skip_freeze=true)`, and keep its bare `incoming`
   twin. A bare outgoing grant freezes the machine on **every**
   crossing in enforce mode, not only the first.
3. Write a header that says what the task fetches and why.
   `examples/smoke/cella/build-pmars/environment/cella.policy` is the
   worked example. CELLA.md §3.1 has the full review steps.

**Then stop dry-running.** Commit the groomed file beside the task's
Dockerfile, like a lockfile, and run the task again without the flag:

```bash
titanium run --env cella --agent oracle --path path/to/your-task
# or, for the checked-in smokes:
make smoke-cella
```

Enforce mode is the proof: it must release everything the task and
the verifier need and refuse the rest, on the record. A dry run only
observes; it proves nothing about enforcement. A later dry run keeps
your windows (a groomed grant holds its destination), but it
re-collects the infra grants you stripped — after any re-collection,
groom again before you commit.

**What the file does not need.** `cella.policy` declares the *task's*
egress only. The agent's own line — its inference host and install
endpoints — comes from the agent's allowlist, and titanium composes
it into the appliance border on every trial, with or without a grant
in the task file. Do not add it.

## 9. Where to look, by question

| Question | Read |
|---|---|
| Why was a crossing refused? | the machine's `verdict.txt`, then `engine.log` for the judged name, then the task's `cella.policy` for the missing grant |
| What did the task reach? | the appliance's `network/names`, then `ledger.txt` for the bytes |
| Why is it slow? | the `host_ns` gaps in `ledger.txt` (~1 ms is healthy; ~200 ms is a stale bridge), and whether `membrane-memory.txt` is present |
| Why did it wedge? | the tail of the appliance's `vmm.log` (a freeze-thaw storm? a park with no verdict?), then `exception.txt` |
| Did the agent run at all? | the `agent/` contents (§3), then `audit.txt` in the `-agent` machines |
| A finished trial has `cella-env-<id>/` | the trial died before teardown — its `context/`, rootfs tars, and freeze states are the material for the post-mortem |
