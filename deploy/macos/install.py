"""Install the operator's per-user backup retention agent; never changes login permissions."""
from pathlib import Path
import os
import plistlib
import shutil
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[2]
BASE = Path.home() / "Library/Application Support/OWKR"
BACKUPS = BASE / "backups/oracle-tokyo"
MAINTENANCE = BASE / "maintenance"
LABEL = "org.overwatchkr.backup-retention"


def main() -> None:
    python = shutil.which("python3.13") or sys._base_executable
    # Validate existing bundles in preview mode before activating deletion.
    subprocess.run([python, str(ROOT / "scripts/retention.py"), "--backups", str(BACKUPS)], check=True)
    MAINTENANCE.mkdir(parents=True, exist_ok=True, mode=0o700)
    MAINTENANCE.chmod(0o700)
    script = MAINTENANCE / "retention.py"
    shutil.copyfile(ROOT / "scripts/retention.py", script)
    script.chmod(0o600)
    wrapper = MAINTENANCE / "run-retention.sh"
    wrapper.write_text('#!/bin/sh\nset -eu\numask 077\nexec "$@" > "$(dirname "$0")/retention-last-run.log" 2>&1\n')
    wrapper.chmod(0o700)
    agents = Path.home() / "Library/LaunchAgents"
    agents.mkdir(exist_ok=True)
    target = agents / f"{LABEL}.plist"
    target.write_bytes(plistlib.dumps({
        "Label": LABEL,
        "ProgramArguments": ["/bin/sh", str(wrapper), python, str(script), "--backups", str(BACKUPS), "--apply"],
        "RunAtLoad": True,
        "StartInterval": 3600,
        "ProcessType": "Background",
    }))
    target.chmod(0o600)
    domain = f"gui/{os.getuid()}"
    subprocess.run(["launchctl", "bootout", f"{domain}/{LABEL}"], capture_output=True)
    subprocess.run(["launchctl", "bootstrap", domain, str(target)], check=True)
    print(f"Installed {LABEL}; latest run log: {MAINTENANCE / 'retention-last-run.log'}")


if __name__ == "__main__":
    main()
