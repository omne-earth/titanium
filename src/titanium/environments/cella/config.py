"""Cella's opinionated timing defaults, in one visible place.

Every value here is a deliberate default, not an accident. The reasons are
recorded in ``docs/environments/CELLA.md`` under "## Decisions"; change a
value against its reason, not by habit. The membrane windows are policy
duration strings (``s``/``m``/``h``); the rest are seconds.
"""

# --- Membrane windows (keep_open, as policy duration strings) ----------

# The member border is all appliance plumbing, never a world crossing, so
# it must not re-freeze on a routine hop. The engine pre-plants each hop at
# stream open (the first crossing waits live), and a 24h window keeps that
# memory from lapsing for the machine's whole life -- the member then
# freezes only when genuinely blocked upstream. Covers ARP and the member's
# grants to its appliance (443/80/53).
MEMBER_KEEP_OPEN = "24h"

# ARP and the upstream resolver are infrastructure; they live for the same
# span on every border.
ARP_KEEP_OPEN = "24h"
UPSTREAM_DNS_KEEP_OPEN = "24h"

# The member's reply-port window: the appliance answers the member on these
# ephemeral ports, granted as exact destinations.
REPLY_WINDOW_KEEP_OPEN = "1h"

# The appliance's world hosts. The allowlist is the security boundary, not
# the re-judge frequency: a granted, named host stays live for an hour so a
# long run does not freeze again to re-judge the same allowlisted name. This
# is deliberately shorter than the member windows -- it gates real world
# egress, so it stays a knob, not "effectively forever".
APPLIANCE_HOST_KEEP_OPEN = "60m"

# The member's resolver patience: a world name resolved at the appliance can
# park while the appliance itself freezes for a judgment, so the guest's
# resolver must wait rather than fail fast.
RESOLVER_TIMEOUT_SEC = 30
RESOLVER_ATTEMPTS = 3

# --- Timeouts and margins (seconds) ------------------------------------

# One exec is a whole VM boot/run/collect cycle. This is the default budget
# for the guest to produce a result; a caller (a task's own timeout) may
# override it.
EXEC_TIMEOUT_SEC = 600.0

# Added on top of the exec budget for the boot, the freeze/thaw cycles, and
# the collect, before the harness gives up on a machine.
BOOT_MARGIN_SEC = 180.0

# A single cella verb (create/start/stop/...); short, since a verb is not a
# workload.
CELLA_VERB_TIMEOUT_SEC = 120.0

# How often the harness reads back the guest's result file while it runs.
RESULT_POLL_SEC = 5.0

# The grace given to a guest's ``systemctl poweroff`` before teardown.
POWEROFF_GRACE_SEC = 3.0
