# Compatibility and support

BSM Supervisor is experimental software. Use a disposable deployment to validate
installation, upgrades and recovery before using it with important server data.
Keep recovery copies outside the LXC and test their restoration.

| Component | Compatibility target |
| --- | --- |
| Operating system | Debian 12/13 or Ubuntu 24.04+, with systemd |
| Python | 3.11+; automated tests cover 3.11 and 3.14 |
| Host provisioning | Proxmox VE 9.2+ with native `pct --env` support; x86-64 unprivileged LXC |
| BSM | 4.0 releases and prereleases starting at 4.0.0b1 |
| Readiness authentication | Local readiness credentials when supplied by BSM; otherwise a private user bearer token |
| Graceful shutdown | Requires a verifiable local BSM control record |
| Checkpoint database | Initialized, local file-backed SQLite |
| Filesystem coverage | Declared local directories; external server/plugin data must be included explicitly |
| Console | One BSM instance per configuration; trusted administrators share one console credential |
| Reverse proxy | Exact public origin, preserved Host header, HTTPS for remote access; root URL hosting |
| Export storage | POSIX-compatible filesystem with hard-link publication and directory fsync support |

External databases, Windows service management, subpath console hosting and
per-user console roles are outside this compatibility target. A successful
Python CI run verifies the tested contracts; use the [Proxmox validation
procedure](validation.md) for actual container lifecycle behavior.

## Report a problem

Include reproduction steps, expected behavior, operating-system/Proxmox versions,
`bsm-supervisor version`, the selected BSM version, and relevant `doctor`, `status`
and `history` output. Review output before posting it. Exclude credentials,
private environment files and private infrastructure details.

Recovery depends on what completed before the failure. Preserve the checkpoint,
operation journal and any retained restore workspaces. Inspect the corresponding
[upgrade](upgrades.md) or [checkpoint](checkpoints.md) recovery instructions before
acknowledging an interruption.
