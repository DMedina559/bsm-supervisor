"""Service startup gating using durable markers and a live, boot-bound controller."""

import argparse
import json
from pathlib import Path


def process_generation(pid: int) -> str:
    """Read Linux process start ticks without depending on psutil."""
    if type(pid) is not int or pid < 1:
        raise ValueError("Invalid process identity")
    record = Path(f"/proc/{pid}/stat").read_text()
    return record[record.rfind(")") + 2 :].split()[19]


def controller_pid() -> int:
    """Use the PID visible in the procfs mount used by the service guard."""
    return int(Path("/proc/self/stat").read_text().split(" ", 1)[0])


def boot_identity() -> str:
    """Return the kernel boot UUID."""
    return Path("/proc/sys/kernel/random/boot_id").read_text().strip()


def permit_path(config_dir: Path) -> Path:
    """Use a deterministic public runtime path containing no credentials."""
    import hashlib

    identity = hashlib.sha256(str(config_dir.resolve()).encode()).hexdigest()[:24]
    return Path("/run/bsm-supervisor") / (identity + ".permit")


def allowed(config_dir: Path) -> bool:
    """Reject recovery markers unless a matching live upgrade authorizes startup."""
    if (config_dir / "supervisor-recovery.required").exists():
        return False
    marker = config_dir / "supervisor-upgrade.required"
    if not marker.exists():
        return True
    try:
        if marker.is_symlink() or marker.stat().st_size > 4096:
            return False
        workflow = json.loads(marker.read_text())
        if not isinstance(workflow.get("workflow_key"), str):
            return False
        path = permit_path(config_dir)
        if path.is_symlink() or path.stat().st_size > 4096:
            return False
        permit = json.loads(path.read_text())
        return (
            permit["workflow_id"] == workflow["workflow_id"]
            and permit["workflow_key"] == workflow["workflow_key"]
            and permit["boot_id"] == boot_identity()
            and permit["generation"] == process_generation(permit["pid"])
        )
    except (OSError, ValueError, KeyError, TypeError, IndexError):
        return False


def main() -> int:
    """Provide a quiet systemd ExecCondition; never print runtime credentials."""
    parser = argparse.ArgumentParser()
    parser.add_argument("--config-dir", type=Path, required=True)
    args = parser.parse_args()
    return 0 if allowed(args.config_dir) else 1


if __name__ == "__main__":
    raise SystemExit(main())
