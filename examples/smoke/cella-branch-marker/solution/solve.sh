#!/bin/bash
# Appends one line per leg: the branch smoke counts these lines in the
# leg's state tar to prove the leg booted from the parent's disk.
set -eu

printf 'leg written_at=%s\n' "$(date -Is)" >> /app/branch-log.txt
