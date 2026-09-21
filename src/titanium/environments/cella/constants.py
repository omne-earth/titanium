"""Every constant of the cella environment, in one place.

One module owns every tunable and every fixed value the rung runs on:
the membrane windows, the budgets, the wire plane's addresses, the
protocol tags, and the builder's pins. Each value states its reason
where it is defined; a module imports what it uses by name. Nothing
here is derived at runtime -- a constant that needs computing is not a
constant.
"""

# --- membrane windows (keep_open, as policy duration strings) ----------
# The member-to-appliance plumbing hops: pre-planted at stream open and
# held for the machine's whole life -- the member freezes only on a
# genuine upstream decision, never on its own plumbing.
MEMBER_KEEP_OPEN = "24h"
ARP_KEEP_OPEN = "24h"
UPSTREAM_DNS_KEEP_OPEN = "24h"
# The member's reply-port window on the appliance border.
REPLY_WINDOW_KEEP_OPEN = "1h"
# World hosts, judged by name: deliberately shorter -- this window
# gates real world egress, not plumbing.
APPLIANCE_HOST_KEEP_OPEN = "60m"

# --- resolver patience --------------------------------------------------
# Past the appliance's first-crossing freeze, or the lookup times out
# before the frozen reply is thawed.
RESOLVER_TIMEOUT_SEC = 30
RESOLVER_ATTEMPTS = 3

# --- budgets ------------------------------------------------------------
# The floor beneath tasks that declare no phase timeout; never the
# figure a declaring task is held to.
CELLA_EXEC_TIMEOUT = 1800.0
# How long past the phases' sum the guest gets to boot and reset.
BOOT_MARGIN_SEC = 180.0
# One cella verb's own budget (create/start/stop/thaw/...).
CELLA_VERB_TIMEOUT_SEC = 120.0
POWEROFF_GRACE_SEC = 3.0

# --- the guest runner ---------------------------------------------------
# The guest-side scratch the orchestrator owns; results live under it
# and never masquerade as task state.
RUNNER_DIR = "/titanium"
# How often the engine-log drainer writes its batch to disk.
ENGINE_LOG_DRAIN_SEC = 1.0

# --- the engine (the judge) ----------------------------------------------
# The full method name from ``service Engine { rpc Decide ... }`` in
# proto/cella.proto; the bridge dials exactly this.
DECIDE_METHOD = "/cella.Engine/Decide"
REFUSAL_WHY_NO_GRANT = "no cella.policy grants this crossing"
# Dry-run collection plants each outgoing destination with a long
# window, 24h like ARP: the window governs freeze frequency during
# collection, never authorization -- the recorder releases everything.
RECORDER_KEEP_OPEN = 86400

# --- the wire plane (the terminated pair) --------------------------------
# Pair-0 addresses: the golden's own boot defaults -- these must equal
# what cella scripts/build/rootfs-terminator.sh writes at boot.
APPLIANCE_WIRE_ADDRESS = "10.77.0.1"
MEMBER_WIRE_ADDRESS = "10.77.0.2"
WIRE_PREFIX = 24
LISTEN_PORTS = (443, 80)
UPSTREAM_DNS = "9.9.9.9"
# The consistent reply port window: eight exact, nameable grants
# instead of an unnameable ephemeral range.
REPLY_PORT_LOW = 50000
REPLY_PORT_HIGH = 50007
# The terminator golden and the pair CA it exports beside itself
# (scripts/init/cella.sh builds both, one per host).
TERMINATOR_GOLDEN = "terminator"
# Guest paths: the pair CA as baked, and the system bundle the member
# prelude folds it into.
# A neutral, substrate-blind path and name: the CA itself is
# observable by design (it sits in the trust bundle), but its name
# must not say what runs the machine. The standard Debian drop-in
# dir keeps it boring.
MEMBER_CA_PATH = "/usr/local/share/ca-certificates/gateway-ca.crt"
SYSTEM_CA_BUNDLE = "/etc/ssl/certs/ca-certificates.crt"

# --- the wire codec (proto3 tags and protocol numbers) -------------------
VARINT_WIRE_TYPE = 0
I64_WIRE_TYPE = 1
LEN_WIRE_TYPE = 2
I32_WIRE_TYPE = 5
DIRECTION_OUTGOING = 0
DIRECTION_INCOMING = 1
ETHERTYPE_ARP = 0x0806

# --- the rootfs builder ---------------------------------------------------
ROOTFS_BUILDER_BASE_IMAGE = "docker.io/library/alpine:3.22"
ROOTFS_BUILDER_IMAGE = "localhost/titanium-cella-rootfs-builder:5"
# GNU tar explicitly: busybox tar has no --numeric-owner, and silently
# losing numeric ownership is exactly the failure this pipeline exists
# to prevent. e2fsprogs carries mkfs.ext4: the image is populated with
# ``mkfs.ext4 -d`` from a staged tree -- nothing ever mounts, fuses, or
# edits a filesystem in place.
ROOTFS_BUILDER_CONTAINERFILE = (
    f"FROM {ROOTFS_BUILDER_BASE_IMAGE}\n"
    "RUN apk add --no-cache e2fsprogs tar\n"
)
BUILD_ROOT = "/work/root"
IN_TAR_NAME = "rootfs.tar"
IN_ENTRY_PREFIX = "entry-"

# --- flavors and goldens ---------------------------------------------------
ROOTFS_AXIS = "rootfs"
ROOTFS_ARTIFACT_NAME = "rootfs.ext4"
MANIFEST_NAME = "golden.json"
# Read-only once published: a golden that can be edited in place is
# not a golden.
MANIFEST_MODE = 0o444
STAGING_PREFIX = ".tmp-"
# NAME_MAX. Not a policy, the filesystem's own limit.
FLAVOR_NAME_MAX = 255

# --- images and builds -----------------------------------------------------
IMAGE_FORMAT = "docker"
BUILD_FILE_NAMES = ("Dockerfile", "Containerfile")
STAGED_BUILD_FILE_NAME = "Dockerfile"

# The harness's standard non-root user, baked into every cella rootfs
# at image-build time (a real /etc/passwd entry with a home directory,
# never an ext4 edit). A task that runs its agent as this user grants
# elevation, command by command, in its own environment/sudoers.
AGENT_USER = "titanium"
USER_BAKE_LINE = (
    f"RUN id -u {AGENT_USER} >/dev/null 2>&1 || "
    f"useradd -m -s /bin/bash {AGENT_USER}\n"
)
CONVERTER_VERSION = "1"
BOOT_LAYER_FIELD = "input_boot_layer"
MAX_GUEST_FILE_MODE = 0o7777

# --- systemd provisioning ---------------------------------------------------
GUEST_INIT_PATH = "/sbin/init"
SYSTEMD_BINARY_CANDIDATES = ("/usr/lib/systemd/systemd", "/lib/systemd/systemd")
OS_RELEASE_CANDIDATES = ("/etc/os-release", "/usr/lib/os-release")
STRATEGY_ALREADY_SYSTEMD = "already-systemd"
MAX_SYMLINK_HOPS = 40
OS_RELEASE_KEYS = ("ID", "ID_LIKE", "VERSION_ID", "PRETTY_NAME")

# --- the policy grammar -----------------------------------------------------
POLICY_VERBS = ("release", "refuse")
POLICY_DIRECTIONS = ("outgoing", "incoming")
PROTO_NAMES = {6: "tcp", 17: "udp"}
PROTO_NUMBERS = {"tcp": 6, "udp": 17}
ETHERTYPE_NAMES = {0x0806: "arp", 0x86DD: "ipv6"}
ETHERTYPE_NUMBERS = {"arp": 0x0806, "ipv6": 0x86DD}
WINDOW_UNITS = {"s": 1, "m": 60, "h": 3600}
POLICY_HEADER = (
    "# cella.policy \u2014 the crossings this task is granted.\n"
    "# <release|refuse> <incoming|outgoing> <destination> (key=value)*\n"
    '# keys: keep_open=<90s|5m|24h> skip_freeze=true reason="..."\n'
    "# '*' matches any ip or any port. Everything not granted is refused.\n"
)
