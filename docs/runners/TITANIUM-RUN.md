# titanium-run: the runner-user shim

The runner is `titanium-run`. The script is `scripts/titanium-run.sh`.
The Make target is `titanium-run`.

`titanium-run` runs a whole titanium invocation as a dedicated,
throwaway user. That user is the `titanium` runner. The runner owns
nothing but trial state. If an agent escapes its container, the escape
lands in the runner account, not the operator account. The escape does
not reach the operator's keys or source.

This runner is shipped and in use. The reflexive runner is
[CELLA-RUN.md](CELLA-RUN.md); it wraps a whole titanium run inside a
sealed VM, one boundary further out.

## 1. Purpose

Titanium runs untrusted agent code. The agent runs inside a container.
A container escape is possible. `titanium-run` limits the blast radius
of an escape.

`titanium-run` moves the whole run off the operator account. It runs
titanium, the builds, and the containers as the runner. The runner is
a `nologin` account. It has its own subuid range and its own container
storage. `scripts/init/titanium.sh` provisions it.

## 2. Who runs

The `RUNNER` variable names the runner user. The default is `titanium`
when the host is provisioned. An unprovisioned host runs as the
invoking user. A provisioning sentinel file decides this
(`/usr/local/share/titanium/titanium.provisioned`).

`titanium-run` wraps the podman family only. The podman family is
`podman`, `gvisor-podman`, and `krun-podman`. Two other cases never
wrap:

* **The docker family (`docker`, `gvisor`) never wraps.** These
  environments talk to the host's root-owned docker daemon. The runner
  would need the `docker` group. The `docker` group is root-equivalent.
  The runner must never join it. That would nullify the separation.
* **The cella rung never wraps.** Cella ships its own separation. Each
  machine runs jailed as its own throwaway sub-uid. There is no runner
  user to borrow.

Opt out for one invocation with `make titanium-run RUNNER=`. Wrap a
hand-built command with the shim directly:
`bash scripts/titanium-run.sh .venv/bin/titanium run -p path/to/task --env gvisor-podman`.

## 3. The mechanism

`titanium-run` runs one privileged command. That command is a single
`sudo systemd-run`. It asks PID 1 to start a transient scope. The scope
runs as the runner uid. The scope waits for the run to finish. The
scope collects itself on exit.

```
sudo systemd-run --uid=<runner> --pipe --wait --quiet --collect
  --working-directory=<pwd>
  --setenv=XDG_RUNTIME_DIR=/run/user/<runner-uid>
  <allowlisted setenv args>
  -- /usr/bin/bash -c 'exec "$0" "$@"' <command> <args>
```

The scope sets `XDG_RUNTIME_DIR`. Rootless podman needs it. Systemd
leaves it unset for system units, so the shim sets it. The scope does
not set `HOME`. Systemd reads `HOME` from the user database for any
unit with `User=`.

## 4. The privilege inventory

The single `sudo systemd-run` is the only privileged command in the run
path. Its sole job is to start the scope and drop to the runner.

Everything inside the scope runs unprivileged as the runner. This
includes titanium, podman-compose, the builds, conmon, and the
containers.

The remaining privileged machinery is not specific to this shim. The
setuid `newuidmap` and `newgidmap` helpers serve all rootless podman.
They are capability-scoped to the runner's `/etc/subuid` range. PID 1
itself is the scope manager.

## 5. Rejected alternatives

Two other designs were rejected:

* **Per-command `sudo -u`.** This breaks rootless podman's session
  assumptions. It breaks the user manager, `XDG_RUNTIME_DIR`, and
  cgroup ownership.
* **Podman's per-user API socket.** This reintroduces the control
  socket. The podman environments exist to avoid that socket
  (PODMAN.md §1).

## 6. The environment allowlist

Only allowlisted variables cross into the scope. `PASS_VARS` names
them. A trial needs the model API credentials and the `TITANIUM_*`
knobs. Nothing else from the operator's environment reaches the scope.

The allowlist holds these variables: `OPENROUTER_API_KEY`,
`ANTHROPIC_API_KEY`, `TITANIUM_API_BASE`, `TITANIUM_IMAGE_SOURCE`,
`TITANIUM_PODMAN_SELINUX_RELABEL`, `TITANIUM_PODMAN_CGROUP_FAIL_CLOSED`,
and `TITANIUM_RUNSC_DIGEST_PIN`.

## 7. The file descriptors

The shim passes stdin, stdout, and stderr through its own pipes. It
does not hand the caller's file descriptors to the scope. Two failures
force this:

* **A regular file as stdout.** `systemd-run --pipe` gives the caller's
  fds to PID 1. PID 1 accepts pipes and ttys. PID 1 rejects regular
  files. `make smoke-* > log` is a regular file. A pipe created here is
  not.
* **An ssh-created stdin.** An `ssh host make smoke-...` invocation
  gives an sshd stdin labeled `sshd_session_t`. SELinux forbids
  dbus-broker to read that label. A pipe created here carries this
  shell's own context, which dbus-broker may read.

## 8. The SELinux domain entry

The scope's entrypoint is `/usr/bin/bash`, not the target binary. On an
SELinux-enforcing host the unit's first exec runs in `init_t`. `init_t`
is not permitted to execute `user_home_t` files. A repo venv under
`/home` is a `user_home_t` file. Entry through a system shell
transitions into the unconfined service domain, which is permitted.
Relabeling the venv would not survive the next `uv sync`.

The shim also fixes the executable path. Systemd's `ExecStart=` needs an
absolute path. A relative path with a slash (`.venv/bin/titanium`)
resolves against the working directory, so the shim makes it absolute.

## 9. Inspect runner-owned state

The runner's containers and images live in the runner's own storage.
The operator's own `podman ps` shows nothing. Use the make proxy to
reach the runner's context:

```bash
make podman-ps ARGS=--all          # the runner's containers
make podman-images                 # the runner's images
make podman-logs ARGS=<container>  # a runner container's logs
make podman-inspect ARGS=<id>      # any other verb forwards the same way
```

The proxy forwards the verb. Whether the verb succeeds is the runtime's
call. `podman exec` reaches crun and runsc containers. `podman exec`
never reaches krun ones. The krun handler has no exec (KRUN-PODMAN.md
§5).

## 10. References

* [../../README.md](../../README.md) — the "Run" section, which states
  how the invocation decides who runs.
* [../environments/PODMAN.md](../environments/PODMAN.md) — the podman
  family and the control-socket rationale (§1).
* [../environments/KRUN-PODMAN.md](../environments/KRUN-PODMAN.md) — the
  krun handler and its lack of exec (§5).
* `scripts/init/titanium.sh` — the runner user provisioning.
* [CELLA-RUN.md](CELLA-RUN.md) — the reflexive runner, one boundary out.
