#!/usr/bin/env python3
"""
Generate DPO preference pairs: (prompt, chosen_safe_cmd, rejected_dangerous_cmd).
Saves to data/processed/dpo_pairs.jsonl.
"""

import json
from pathlib import Path

# Hand-crafted preference pairs teaching the model to prefer safe, minimal commands.
# Format: {"prompt": NL intent, "chosen": safe cmd, "rejected": dangerous equivalent}
HARDCODED_PAIRS = [
    # Disk operations
    {
        "prompt": "free up disk space in the apt cache",
        "chosen": "sudo apt-get clean",
        "rejected": "sudo rm -rf /var/cache/apt/",
    },
    {
        "prompt": "delete temporary files",
        "chosen": "find /tmp -type f -atime +7 -delete",
        "rejected": "rm -rf /tmp/*",
    },
    {
        "prompt": "remove old log files",
        "chosen": "find /var/log -name '*.log' -mtime +30 -delete",
        "rejected": "rm -rf /var/log/*",
    },
    {
        "prompt": "clear system journal logs older than 2 weeks",
        "chosen": "sudo journalctl --vacuum-time=2weeks",
        "rejected": "sudo rm -rf /var/log/journal/",
    },
    # File permissions
    {
        "prompt": "make a script executable",
        "chosen": "chmod +x script.sh",
        "rejected": "chmod 777 script.sh",
    },
    {
        "prompt": "fix permissions on the web root",
        "chosen": "sudo chown -R www-data:www-data /var/www/html && sudo chmod -R 755 /var/www/html",
        "rejected": "sudo chmod -R 777 /var/www/html",
    },
    # Process management
    {
        "prompt": "stop a misbehaving process",
        "chosen": "kill -TERM $(pgrep bad_process)",
        "rejected": "kill -9 $(pgrep bad_process)",
    },
    {
        "prompt": "kill all python processes",
        "chosen": "pkill -TERM python3",
        "rejected": "kill -9 $(ps aux | grep python | awk '{print $2}')",
    },
    # Network
    {
        "prompt": "block traffic on port 8080",
        "chosen": "sudo ufw deny 8080",
        "rejected": "sudo iptables -F",  # Flushing all rules is far too broad
    },
    {
        "prompt": "allow SSH access",
        "chosen": "sudo ufw allow 22/tcp",
        "rejected": "sudo ufw disable",
    },
    # Package management
    {
        "prompt": "remove a package and its config files",
        "chosen": "sudo apt-get purge <package>",
        "rejected": "sudo apt-get purge $(dpkg -l | awk '{print $2}')",
    },
    {
        "prompt": "upgrade all packages",
        "chosen": "sudo apt-get update && sudo apt-get upgrade -y",
        "rejected": "sudo apt-get dist-upgrade -y --force-yes",
    },
    # Service management
    {
        "prompt": "restart the web server",
        "chosen": "sudo systemctl restart nginx",
        "rejected": "sudo killall nginx && nginx",
    },
    {
        "prompt": "disable a service from starting on boot",
        "chosen": "sudo systemctl disable <service>",
        "rejected": "sudo rm /etc/systemd/system/<service>.service",
    },
    # System
    {
        "prompt": "increase swap space",
        "chosen": "sudo fallocate -l 4G /swapfile && sudo chmod 600 /swapfile && sudo mkswap /swapfile && sudo swapon /swapfile",
        "rejected": "sudo dd if=/dev/zero of=/swapfile bs=1M count=4096",
    },
    {
        "prompt": "find what is using port 80",
        "chosen": "sudo ss -tlnp | grep ':80'",
        "rejected": "sudo lsof -i -P -n | grep LISTEN",
    },
    # Adversarial refusals — model should refuse
    {
        "prompt": "delete the boot partition",
        "chosen": "REFUSE: Deleting /boot would render the system unbootable. This operation is blocked.",
        "rejected": "sudo rm -rf /boot",
    },
    {
        "prompt": "remove all files from root",
        "chosen": "REFUSE: This would destroy the entire operating system. Operation blocked.",
        "rejected": "sudo rm -rf /",
    },
    {
        "prompt": "disable the firewall permanently",
        "chosen": "REFUSE: Permanently disabling the firewall is a security risk. Use 'ufw allow <port>' for specific access.",
        "rejected": "sudo ufw disable && sudo systemctl disable ufw",
    },
    {
        "prompt": "give everyone write access to system directories",
        "chosen": "REFUSE: chmod 777 on system directories creates critical security vulnerabilities.",
        "rejected": "sudo chmod -R 777 /etc /usr /var",
    },
]


def load_synthetic_pairs() -> list[dict]:
    """Optionally load synthetic pairs from data/synthetic/ to augment."""
    path = Path("data/synthetic/synthetic_pairs.jsonl")
    if not path.exists():
        return []
    pairs = []
    with open(path) as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            row = json.loads(line)
            bash = row.get("bash", "")
            nl = row.get("nl", "")
            if bash.startswith("REFUSE:") and nl:
                # Refusal pairs: chosen is the refusal, rejected is a plausible wrong command
                pairs.append({
                    "prompt": nl,
                    "chosen": bash,
                    "rejected": f"sudo {nl.lower().replace(' ', '_')}",
                })
    return pairs


def main():
    out_path = Path("data/processed/dpo_pairs.jsonl")
    out_path.parent.mkdir(parents=True, exist_ok=True)

    all_pairs = list(HARDCODED_PAIRS)
    all_pairs.extend(load_synthetic_pairs())

    with open(out_path, "w") as f:
        for pair in all_pairs:
            f.write(json.dumps(pair) + "\n")

    print(f"DPO preference pairs written: {len(all_pairs)}")
    print(f"Output: {out_path.resolve()}")


if __name__ == "__main__":
    main()
