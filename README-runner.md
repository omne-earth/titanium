# Runner Isolation

This document tells you how to run trials as the dedicated runner user `titanium`. It also tells you how to give this permission to operators who are not in the `wheel` group.

Two roles appear in this document:

- `admin-user`: has full sudo. Does the one-time setup.
- `standard-user`: is only in the `titanium-run` group. Runs trials.

## Overview

`scripts/titanium-run.sh` starts a command as the runner user `titanium`. The command runs in a transient systemd scope that PID 1 owns. If a container escape occurs, the attacker gets an account that owns only trial state.

The wrapper makes one privileged call: `sudo systemd-run --uid=titanium`. This call asks PID 1 to start the scope and to drop to the runner. All processes in the scope run unprivileged.

The wrapper passes only an allowlist of environment variables into the scope. See `PASS_VARS` in the script.

The `RUNNER` environment variable can select a different runner user. Do not use it. The sudoers rule in this document permits only `--uid=titanium`.

## Admin steps

Do these steps as `admin-user`. The `titanium-run` sudoers rule permits only the run path: `systemd-run --uid=titanium`. It does not permit the commands below (`useradd`, `usermod`, `loginctl`, `setfacl`, writes under `/etc` and `/usr/local`). A `standard-user` cannot do these steps.

### 1. Provision the runner (once per host)

```bash
cd ~admin-user/workspace/titanium
bash scripts/init/titanium.sh
```

This creates the `titanium` user, allocates its subordinate ID range, enables lingering, configures cgroup delegation, and grants the runner filesystem ACLs on this clone.

### 2. Create the operator group (once per host)

```bash
sudo groupadd titanium-run
```

### 3. Add the sudoers rule (once per host)

Run:

```bash
sudo visudo -f /etc/sudoers.d/titanium-run
```

Add this line:

```sudoers
%titanium-run ALL=(root) NOPASSWD:SETENV: /usr/bin/systemd-run --uid=titanium *
```

Notes:

- `SETENV:` is necessary. The wrapper calls `sudo --preserve-env=...`.
- Make sure the path is correct. Run `command -v systemd-run` to find it.
- The rule permits only `systemd-run --uid=titanium`. It does not permit other commands as root. Arguments after `--uid=titanium` run as the runner, not as root.

### 4. Add each operator to the group

```bash
sudo usermod -aG titanium-run standard-user
```

The operator must log in again. Group changes do not apply to open sessions.

### 5. Provision each operator's clone (once per clone)

The provisioning script grants the runner filesystem ACLs on the repository it runs from, not on all clones. If `standard-user` works from their own clone, the runner cannot read that tree until an admin provisions it.

```bash
cd ~standard-user/workspace/titanium
bash scripts/init/titanium.sh
```

The script is idempotent. On a provisioned host, the user, linger, and systemd steps do nothing. The run adds only the ACLs for this clone: traversal on the path components, read on the repository, and read-write on `.run` for the runner and for `standard-user`.

Alternative: provision one shared checkout (for example `/srv/titanium`) once, and give operators group write access to it. Then step 5 is not necessary.

### Result

After these steps, `standard-user` can:

- run trials with `./scripts/titanium-run.sh` and the `make` targets, with no password;
- read trial output under `.run/`.

`standard-user` cannot:

- run other commands as root;
- re-provision the host or change the runner's configuration.

## Operator steps

Do these steps as `standard-user`, from the root of a provisioned clone.

### Run a command as the runner

```bash
./scripts/titanium-run.sh <command> [args...]
```

Examples:

```bash
./scripts/titanium-run.sh id -un
./scripts/titanium-run.sh .venv/bin/titanium --help
```

The wrapper resolves a relative executable path against the current directory. The scope starts in the current directory.

The `make` targets wrap themselves automatically on a provisioned host. For example:

```bash
make titanium-run TITANIUM_TASK=./examples/tasks/foobar TITANIUM_ENV=krun-podman
```

### Verify the setup

1. Show the sudo rules:

   ```bash
   sudo -l
   ```

   Expected result: the list shows only the `systemd-run --uid=titanium` rule (plus rules from other groups, if any).

2. Run a command as the runner:

   ```bash
   ./scripts/titanium-run.sh id -un
   ```

   Expected result: the output is `titanium`. No password prompt occurs.

3. Try a command that the rule does not permit:

   ```bash
   sudo id
   ```

   Expected result: sudo refuses the command.

### Troubleshooting

| Symptom | Cause | Correction |
| --- | --- | --- |
| `runner user 'titanium' does not exist` | The runner is not provisioned. | Ask an admin to do admin step 1. |
| A password prompt occurs. | You are not in the `titanium-run` group, your session is old, or the `systemd-run` path in the sudoers rule does not match the host's binary. | Ask an admin to check admin steps 3 and 4. Log in again. |
| `sudo: sorry, you are not allowed to preserve the environment` | The sudoers rule does not have `SETENV:`. | Ask an admin to add `SETENV:` to the rule. |
| `Permission denied` on repository files in the scope. | The runner has no ACLs on this clone. | Ask an admin to do admin step 5 for your clone. |
| `Failed to start transient service unit: Remote peer disconnected` | PID 1 rejects a regular file as stdout. | Not applicable — the wrapper prevents this with pipe redirection. Report it if it occurs. |
| Rootless podman fails in the scope. | The runner does not linger, or `XDG_RUNTIME_DIR` is not correct. | Ask an admin to run `loginctl enable-linger titanium`. |

## Design constraints

Do not replace the `sudo systemd-run` call with these alternatives:

- **`sudo -u <runner>` for each command.** This breaks the session assumptions of rootless podman (user manager, `XDG_RUNTIME_DIR`, cgroup ownership).
- **A per-user podman API socket.** This adds the control socket that the podman environments must not have. See `docs/environments/PODMAN.md`.
- **A polkit rule for `org.freedesktop.systemd1.manage-units`.** Polkit cannot limit the rule to one UID. The rule would permit units as root.
- **An unprivileged sandbox (bubblewrap).** A user namespace cannot change the real UID. An escape would land in the operator account, not in the runner account.
