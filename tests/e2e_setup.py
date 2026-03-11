"""
End-to-End Test Suite for UnifyRoute Setup

This script uses `pexpect` to automate the interactive `./unifyroute setup` CLI
process to test both installation and uninstallation flows.

Usage:
  uv run --with pexpect python tests/e2e_setup.py
"""

import os
import sys
import time
from pathlib import Path

# Need pexpect. If not installed, error descriptively.
try:
    import pexpect
except ImportError:
    print("❌ pexpect not found.")
    print("   Run with: uv run --with pexpect python tests/e2e_setup.py")
    sys.exit(1)

# Project Root
SCRIPT_DIR = Path(__file__).parent.resolve()
ROOT = SCRIPT_DIR.parent
UNIFYROUTE_BIN = ROOT / "unifyroute"

class UnbufferedLog:
    def __init__(self, filename):
        self.file = open(filename, "w" if not os.path.exists(filename) else "a")
    def write(self, data):
        self.file.write(data)
        self.file.flush()
    def flush(self):
        self.file.flush()

def check_installed() -> bool:
    """Check if the application appears currently installed."""
    venv_dir = ROOT / ".venv"
    db_file = ROOT / "data" / "unifyroute.db"
    
    # We consider it installed if either the venv or db exists
    # (setup.py uninstall intentionally does not remove .env unless asked to delete the whole repo)
    return venv_dir.exists() or db_file.exists()


def run_uninstall():
    """Run the uninstallation sequence."""
    print("▶ Running uninstall...")
    cmd = f"{UNIFYROUTE_BIN} setup uninstall"
    
    # We use a timeout of 120 seconds for uninstall, mostly for docker stuff
    child = pexpect.spawn(cmd, cwd=str(ROOT), encoding="utf-8", timeout=120)
    
    # Log output to file
    logfile = UnbufferedLog("e2e_setup.log")
    child.logfile = logfile
    
    child.expect("Are you sure you want to uninstall", timeout=10)
    child.sendline("y")
    
    child.expect("Save current configuration before uninstall", timeout=10)
    child.sendline("n")  # Don't save backups during tests
    
    # Docker optionally prompts if docker-compose.yml exists and docker is running
    idx = child.expect(["Remove Docker containers and volumes", "Removing Local Files", pexpect.TIMEOUT], timeout=30)
    if idx == 0:
        child.sendline("y")
        idx2 = child.expect(["Remove Docker images", "Removing Local Files", pexpect.TIMEOUT], timeout=10)
        if idx2 == 0:
            child.sendline("y")
            # Now wait for the Local Files header
            child.expect("Removing Local Files", timeout=120)
    elif idx == 1:
        # Prompt for docker cleanups was skipped, moving on
        pass
    elif idx == 2:
        print("❌ Timeout waiting for docker/local files prompt")
        sys.exit(1)
        
    # After 'Removing Local Files', wait for EOF
    child.expect(pexpect.EOF, timeout=120)
        
    child.close()
    if child.exitstatus != 0:
        print(f"❌ Uninstall command failed with exit code: {child.exitstatus}")
        sys.exit(1)
        
    # Verify removal
    if check_installed():
        print("❌ Uninstall completed but files (.venv, .env, or data/) still exist!")
        sys.exit(1)
        
    print("✅ Uninstall successful.")


def run_install():
    """Run the installation sequence."""
    print("▶ Running install...")
    cmd = f"{UNIFYROUTE_BIN} setup install"
    
    # Give installation plenty of time (5 minutes) for uv sync, npm install, etc.
    child = pexpect.spawn(cmd, cwd=str(ROOT), encoding="utf-8", timeout=300)
    logfile = UnbufferedLog("e2e_setup.log")
    child.logfile = logfile

    # In case there are saved configs, it will ask to restore
    idx = child.expect(["Would you like to restore a previously saved configuration", "SQLite file path"], timeout=10)
    if idx == 0:
        child.sendline("n")
        child.expect("SQLite file path", timeout=10)
        
    # 1. Database (SQLite path)
    child.sendline("") # Accept default

    # 2. Application Settings
    child.expect("Application port", timeout=10)
    child.sendline("")
    child.expect("Application host", timeout=10)
    child.sendline("")
    child.expect("API base URL", timeout=10)
    child.sendline("")

    # 3. Master Password
    child.expect("Master password", timeout=10)
    child.sendline("test-password-1234")
    child.expect("Confirm master password", timeout=10)
    child.sendline("test-password-1234")

    # Now it does automated work (pip install, npm install, db migrations)
    # Just wait for EOF (completion) or Timeout
    try:
        child.expect(pexpect.EOF, timeout=300)
    except pexpect.TIMEOUT:
        print("\n❌ Installation timed out after 300 seconds!")
        sys.exit(1)

    child.close()
    if child.exitstatus != 0:
        print(f"❌ Install command failed with exit code: {child.exitstatus}")
        sys.exit(1)
        
    # Verify installation
    if not check_installed():
        print("❌ Install completed but essential files (.venv, .env, or data/) are missing!")
        sys.exit(1)
        
    print("✅ Install successful.")


def main():
    # Remove old logfile if exists
    if os.path.exists("e2e_setup.log"):
        os.remove("e2e_setup.log")
    print("=== UnifyRoute Setup E2E Test Suite ===")
    
    is_installed = check_installed()
    print(f"Initial State: Installed = {is_installed}")
    
    if is_installed:
        print("\n--- Phase 1: Initial Cleanup ---")
        run_uninstall()
        
    print("\n--- Phase 2: Fresh Install ---")
    run_install()
    
    print("\n--- Phase 3: Final Teardown ---")
    run_uninstall()
    
    print("\n🎉 All Setup E2E tests passed successfully!")

if __name__ == "__main__":
    main()
