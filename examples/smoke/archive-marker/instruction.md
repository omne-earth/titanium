Write the exact text `archived-guest-state` to `/app/archive-marker.txt`.

This task is designed for the `oracle` agent, which runs
`solution/solve.sh` verbatim. The marker is written outside the mounted
log directories so it belongs to the container filesystem that an
environment export captures.
