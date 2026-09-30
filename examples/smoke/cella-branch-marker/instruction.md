Append one line reading `leg` to `/app/branch-log.txt`.

This task is designed for the `oracle` agent, which runs
`solution/solve.sh` verbatim. Each leg of a run appends exactly one
line, so the file's line count says how many legs have touched this
disk: a branched leg that booted from its parent's state shows two.
The file lives outside the mounted log directories so it belongs to
the guest filesystem a state extract captures.
