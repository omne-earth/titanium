#!/bin/bash
# Writes the marker an archive smoke looks for inside the exported tar.
# /app is part of the container filesystem; /logs/* are bind mounts and are
# deliberately excluded from a native export.
set -eu

printf 'archived-guest-state\n' > /app/archive-marker.txt
printf 'written_at=%s\n' "$(date -Is)" >> /app/archive-marker.txt
