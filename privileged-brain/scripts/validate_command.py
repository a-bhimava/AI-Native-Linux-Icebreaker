#!/usr/bin/env python3
"""
Post-generation command validator for Privileged Brain.

Runs two checks before a generated bash command is ever executed:
  1. bash -n syntax check  — catches ALL syntax errors without executing anything
  2. Dangerous pattern scan — catches system-destructive commands

Can be used as a module (validate(cmd) → ValidationResult) or as a CLI:
  python3 scripts/validate_command.py "find . -name '*.log' -delete"
  echo "rm -rf /" | python3 scripts/validate_command.py -

Exit codes (CLI mode):
  0 = valid and safe (or already a REFUSE: response)
  1 = syntax error
  2 = dangerous pattern detected
  3 = empty / unparseable command
"""

import json
import re
import subprocess
import sys
from typing import NamedTuple


class ValidationResult(NamedTuple):
    ok: bool
    code: str    # OK | REFUSED | SYNTAX_ERROR | DANGEROUS | TIMEOUT | EMPTY
    reason: str  # human-readable explanation; empty when ok=True and code=OK


# Patterns: (compiled_regex, human_description)
# Ordered from most dangerous to least.
_DANGEROUS_PATTERNS: list[tuple[re.Pattern, str]] = [
    (re.compile(r'\brm\s+(-\S*\s+)*-[rf]{2,}\s+/', re.I),           "rm -rf on root or critical system path"),
    (re.compile(r'\brm\s+-[rf]+\s+/'),                                "rm -r or rm -f on root path"),
    (re.compile(r'\bmkfs\b', re.I),                                   "filesystem format (mkfs) — destroys all data on target"),
    (re.compile(r'\bdd\b.*\bif=/dev/(zero|random|urandom)\b', re.I), "dd zeroing/randomising a device"),
    (re.compile(r'\bdd\b.*\bof=/dev/[sh]d', re.I),                   "dd writing directly to disk device"),
    (re.compile(r'>\s*/dev/[sh]d[a-z]', re.I),                       "output redirection to raw disk device"),
    (re.compile(r'\bshred\b.*\b/dev/', re.I),                         "shred on disk device"),
    (re.compile(r'(?<!/dev)/etc/passwd\b.*[>|]', re.I),              "overwriting /etc/passwd"),
    (re.compile(r'(?<!/dev)/etc/sudoers\b.*[>|]', re.I),             "overwriting /etc/sudoers"),
    (re.compile(r'\b/boot\b', re.I),                                  "modifying /boot partition"),
    (re.compile(r'\bchmod\s+(-R\s+)?777\s+(/etc|/usr|/var|/boot|/bin|/sbin|/lib)\b', re.I),
                                                                      "chmod 777 on system directory"),
    (re.compile(r'\bchmod\s+(-\S+\s+)*777\s+/$'),                    "chmod 777 on root"),
    (re.compile(r'\bcurl\b[^|]*\|\s*(ba)?sh\b', re.I),               "piping curl output to shell (supply-chain risk)"),
    (re.compile(r'\bwget\b[^|]*-O\s*-[^|]*\|\s*(ba)?sh\b', re.I),   "piping wget output to shell"),
    (re.compile(r'\biptables\s+(-F|--flush)\b', re.I),               "flushing all iptables rules (removes all firewall protection)"),
    (re.compile(r'\bufw\s+(disable|reset)\b', re.I),                  "disabling or resetting ufw firewall"),
    (re.compile(r'\bpasswd\s+-d\s+root\b', re.I),                    "removing root password (enables passwordless root login)"),
    (re.compile(r'\buseradd\b.*-o.*-u\s*0\b', re.I),                 "creating user with root UID (privilege escalation)"),
    (re.compile(r'echo\s+["\']?[^"\']*\s+ALL=\(ALL\)\s+NOPASSWD:ALL', re.I),
                                                                      "writing unrestricted passwordless sudo rule"),
]

_REFUSE_PREFIX = "REFUSE:"


def validate(cmd: str) -> ValidationResult:
    """
    Validate a bash command string.  Returns (ok, code, reason).
    Always call this before executing any model-generated command.
    """
    cmd = cmd.strip()

    if not cmd:
        return ValidationResult(ok=False, code="EMPTY", reason="empty command")

    # Model already decided to refuse — that's a valid outcome
    if cmd.upper().startswith(_REFUSE_PREFIX):
        return ValidationResult(ok=True, code="REFUSED", reason=cmd)

    # --- Step 1: syntax check via bash -n ----------------------------------
    # bash -n parses the command without executing anything.
    # Catches malformed pipes, missing fi/done/esac, unmatched brackets, etc.
    try:
        proc = subprocess.run(
            ["bash", "-n", "-c", cmd],
            capture_output=True,
            text=True,
            timeout=5,
        )
        if proc.returncode != 0:
            # Trim the bash boilerplate prefix from the error message
            err = proc.stderr.strip().replace("bash: -c: line ", "line ")
            return ValidationResult(ok=False, code="SYNTAX_ERROR", reason=err)
    except subprocess.TimeoutExpired:
        return ValidationResult(ok=False, code="TIMEOUT", reason="bash -n parse timed out")
    except FileNotFoundError:
        pass  # bash not on PATH — skip syntax check, still run pattern scan

    # --- Step 2: dangerous pattern scan ------------------------------------
    for pattern, description in _DANGEROUS_PATTERNS:
        if pattern.search(cmd):
            return ValidationResult(ok=False, code="DANGEROUS", reason=description)

    return ValidationResult(ok=True, code="OK", reason="")


def main() -> int:
    if len(sys.argv) < 2:
        print("Usage: python3 validate_command.py \"<bash command>\"", file=sys.stderr)
        print("       echo \"<bash command>\" | python3 validate_command.py -", file=sys.stderr)
        return 3

    if sys.argv[1] == "-":
        cmd = sys.stdin.read().strip()
    else:
        cmd = " ".join(sys.argv[1:])

    result = validate(cmd)
    out = {"command": cmd, "valid": result.ok, "code": result.code, "reason": result.reason}
    print(json.dumps(out, indent=2))

    if not result.ok:
        return 1 if result.code == "SYNTAX_ERROR" else 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
