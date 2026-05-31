#!/usr/bin/env python3
"""
Shellcheck post-processor for generated bash commands.

Pipes a command through shellcheck and returns (is_valid, issues).
Used by the shell trigger to catch syntax errors before execution.

Usage as a library:
    from shellcheck_validate import validate
    ok, issues = validate("find . -name *.py")   # flags SC2035 (unquoted glob)
    ok, issues = validate('find . -name "*.py"') # clean

Usage as a CLI:
    echo "find . -name *.py" | python3 shellcheck_validate.py
    python3 shellcheck_validate.py "find . -name *.py"
"""

import json
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path


def _shellcheck_available() -> bool:
    return shutil.which("shellcheck") is not None


def validate(command: str) -> tuple[bool, list[str]]:
    """
    Validate a bash command with shellcheck.

    Returns:
        (True, [])                   — command is clean
        (False, ["SC2035: ...", …])  — command has issues
        (True, ["shellcheck not installed"])  — shellcheck missing; treated as pass
    """
    if not command.strip():
        return True, []

    # Skip REFUSE: and CLARIFY: — these aren't bash, don't validate them
    upper = command.upper().lstrip()
    if upper.startswith(("REFUSE:", "CLARIFY:")):
        return True, []

    if not _shellcheck_available():
        return True, ["shellcheck not installed — skipping syntax check"]

    # Write to a temp file; shellcheck needs a file, not stdin, for accurate diagnostics
    with tempfile.NamedTemporaryFile(mode="w", suffix=".sh", delete=False) as f:
        f.write("#!/bin/bash\n")
        f.write(command + "\n")
        tmp_path = f.name

    try:
        result = subprocess.run(
            [
                "shellcheck",
                "--format=json",
                "--shell=bash",
                "--severity=warning",   # ignore style (SC2148 etc), catch real errors
                tmp_path,
            ],
            capture_output=True,
            text=True,
            timeout=5,
        )
        Path(tmp_path).unlink(missing_ok=True)

        if result.returncode == 0:
            return True, []

        # Parse JSON output for structured issue list
        try:
            findings = json.loads(result.stdout)
            issues = [
                f"SC{f['code']}: {f['message']} (line {f['line']})"
                for f in findings
            ]
        except (json.JSONDecodeError, KeyError):
            # Fallback to raw text if JSON parsing fails
            issues = [line.strip() for line in result.stdout.splitlines() if line.strip()]

        return False, issues

    except subprocess.TimeoutExpired:
        Path(tmp_path).unlink(missing_ok=True)
        return True, ["shellcheck timed out — skipping"]
    except Exception as e:
        Path(tmp_path).unlink(missing_ok=True)
        return True, [f"shellcheck error: {e}"]


def main():
    if len(sys.argv) > 1:
        command = " ".join(sys.argv[1:])
    else:
        command = sys.stdin.read().strip()

    ok, issues = validate(command)
    if ok:
        print(f"OK: {command!r}")
        if issues:
            print(f"  Note: {issues[0]}")
    else:
        print(f"FAIL: {command!r}")
        for issue in issues:
            print(f"  {issue}")
        sys.exit(1)


if __name__ == "__main__":
    main()
