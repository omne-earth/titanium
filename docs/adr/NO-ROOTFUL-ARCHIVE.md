# No Rootful Archive



Titanium currently does not support `on_completion=archive` for rootful container environments such as Docker.



Archiving processes filesystem state produced by an untrusted workload. We therefore want the archive path to preserve Titanium's existing security model: rootless operation, no privileged container daemon, and no privileged socket.



Generic archive support is currently limited to the rootless Podman family:



- Podman

- gVisor-Podman



Docker and Docker-backed gVisor are rejected for `on_completion=archive`. This does not affect their normal teardown behavior.



This is a current security and scope decision, not a claim that safe Docker archiving is impossible. It can be reconsidered later if a design preserves the same containment properties.



## References



- ThreatDown — CARBONATO: https://www.threatdown.com/blog/carbonato/

- CVE-2026-42497 — archive hardlink privilege-escalation class: https://www.sentinelone.com/vulnerability-database/cve-2026-42497/

- CVE-2026-18477 — archive TOCTOU privilege-escalation class: https://www.sentinelone.com/vulnerability-database/cve-2026-18477/
