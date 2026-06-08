# Linux Knowledge Base & Best Practices
## Reference for AI Agents (Agentic CLI / Icebreaker v1)

> **Purpose:** This document is a curated Linux reference for AI agents executing Bash commands on behalf of users. It encodes both conceptual knowledge and safety-critical execution patterns. Agents must consult this before generating or running any system command.
>
> **Scope:** Ubuntu/Debian-based systems. Kernel 5.15+. Targeted at agentic CLI execution contexts where the AI has direct shell access and commands produce real, potentially irreversible side effects.

---

## Table of Contents

1. [Filesystem Hierarchy Standard (FHS)](#1-filesystem-hierarchy-standard)
2. [File Types & Metadata](#2-file-types--metadata)
3. [Permissions & Ownership](#3-permissions--ownership)
4. [Process Model](#4-process-model)
5. [Bash Essentials](#5-bash-essentials)
6. [Shell Safety Patterns](#6-shell-safety-patterns)  ← **Read this first before generating any command**
7. [System Administration](#7-system-administration)
8. [Networking](#8-networking)
9. [Logs & Observability](#9-logs--observability)
10. [Package Management (apt/dpkg)](#10-package-management)
11. [Systemd & Service Management](#11-systemd--service-management)
12. [Agentic Execution Guidelines](#12-agentic-execution-guidelines)  ← **Project-specific rules**

---

## 1. Filesystem Hierarchy Standard

The FHS defines where things live. Never invent paths. Every agent-generated command that touches the filesystem must respect these boundaries.

```
/               Root — the top of everything
├── bin/        Essential user binaries (ls, cp, bash). Symlink → /usr/bin on modern systems.
├── boot/       Kernel, initrd, GRUB config. NEVER write here during normal operation.
├── dev/        Device files (block, char, pseudo). Kernel-managed. Do not create files manually.
├── etc/        System-wide configuration. Text files only. Changes here are global.
├── home/       User home directories (/home/<user>/). The safe sandbox for user data.
├── lib/        Shared libraries for /bin and /sbin. Do not modify directly.
├── media/      Mount points for removable media.
├── mnt/        Temporary mount points.
├── opt/        Optional/third-party software packages.
├── proc/       Virtual FS — kernel and process state. Read-only for agents. Never write.
├── root/       Home directory for root. Restricted — avoid using as a working dir.
├── run/        Runtime data (PIDs, sockets). Cleared on boot.
├── srv/        Data for services (web roots, FTP data).
├── sys/        Virtual FS — device and kernel objects. Read-only for agents.
├── tmp/        Temporary files. World-writable but sticky. Cleared on boot (or via systemd-tmpfiles).
├── usr/        Read-only user data and programs.
│   ├── bin/    Non-essential user binaries.
│   ├── lib/    Libraries for /usr/bin.
│   ├── local/  Locally compiled/installed software (prefix for make install).
│   └── share/  Architecture-independent data (man pages, icons, locales).
└── var/        Variable runtime data — logs, caches, spool, databases.
    ├── log/    System and service logs.
    ├── tmp/    Temp files that persist across reboots (unlike /tmp).
    └── lib/    Persistent application state.
```

### Key Rules for Agents

- **Write user files to `/home/<user>/` or `/tmp/` only** unless explicitly operating on system configuration.
- `/etc/` changes require root. Always generate a backup before overwriting: `cp /etc/foo /etc/foo.bak`.
- `/proc/` and `/sys/` are virtual — reading them is safe; writing to them changes live kernel state and is almost always irreversible.
- `/tmp/` is world-writable with the sticky bit — other users cannot delete your files, but they can read them unless you `chmod 600`.

---

## 2. File Types & Metadata

### File Types (from `ls -l` first character)

| Symbol | Type | Notes |
|--------|------|-------|
| `-` | Regular file | Text, binary, everything else |
| `d` | Directory | A file containing directory entries |
| `l` | Symbolic link | Points to another path; target may not exist |
| `b` | Block device | Hard drives, SSDs (`/dev/sda`) |
| `c` | Character device | Terminals, serial ports (`/dev/tty`) |
| `p` | Named pipe (FIFO) | IPC between processes |
| `s` | Socket | Unix domain socket |

### Reading `stat` Output

```bash
stat /etc/passwd
#  File: /etc/passwd
#  Size: 2741      Blocks: 8     IO Block: 4096   regular file
# Device: 8,1      Inode: 131073  Links: 1
# Access: (0644/-rw-r--r--)  Uid: (0/ root)   Gid: (0/ root)
# Access: 2026-05-30 10:11:22.000
# Modify: 2026-05-15 08:00:00.000   ← last content change (mtime)
# Change: 2026-05-15 08:00:00.000   ← last metadata change (ctime)
```

- **mtime** (modify time): content changed. Relevant for build systems and backups.
- **ctime** (change time): metadata changed (permissions, owner). Cannot be set by users — only the kernel sets it.
- **atime** (access time): last read. Often disabled (`noatime` mount option) for performance.

### Inodes

Every file has an inode: a kernel data structure holding metadata (permissions, owner, size, block pointers) but **not** the filename. The directory holds the name→inode mapping.

- Hard links share an inode. Deleting one link doesn't remove the data until all links are gone.
- Symbolic links are their own inode pointing to a path string. Deleting the target breaks the symlink.
- `ls -i` shows inode numbers. `df -i` shows inode usage (a disk can be "full" with free blocks if inodes are exhausted).

---

## 3. Permissions & Ownership

### The Permission Bits

Every file has an owner (user), a group, and three sets of three bits:

```
-rwxr-xr--
 ^^^         owner:  read(4) + write(2) + execute(1) = 7
    ^^^      group:  read(4) + no-write + execute(1) = 5
       ^^^   others: read(4) + no-write + no-execute  = 4
```

Octal notation: `chmod 754 file`

| Octal | Binary | Meaning |
|-------|--------|---------|
| 7 | 111 | rwx |
| 6 | 110 | rw- |
| 5 | 101 | r-x |
| 4 | 100 | r-- |
| 0 | 000 | --- |

**On directories**, the bits mean differently:
- `r`: can list contents (`ls`)
- `w`: can create/delete entries inside (requires `x` too)
- `x`: can enter (`cd`) and access contents

### Special Bits

| Bit | On file | On directory |
|-----|---------|--------------|
| SUID (4000) | Run as file's owner | (no standard effect) |
| SGID (2000) | Run as file's group | New files inherit directory's group |
| Sticky (1000) | (ignored on modern Linux) | Only owner can delete their files (e.g., `/tmp`) |

```bash
chmod 4755 /usr/bin/sudo   # SUID — runs as root regardless of caller
chmod 1777 /tmp            # Sticky + world-writable
```

### Checking Effective Permissions

```bash
# Who am I and what groups am I in?
id
whoami

# Can this user read a file?
sudo -u <user> test -r /path/to/file && echo "readable"

# List all group memberships
groups <user>
```

### umask

`umask` is a mask applied to new file permissions. Default is typically `0022`, meaning:
- New files: `0666 & ~0022 = 0644` (rw-r--r--)
- New dirs:  `0777 & ~0022 = 0755` (rwxr-xr-x)

For sensitive files in agent workflows, set a restrictive umask before creating:
```bash
(umask 077; touch /tmp/secret_output.txt)   # Only owner can read/write
```

### chown / chgrp / chmod Reference

```bash
chmod 644 file               # Set exact permissions
chmod u+x,g-w file           # Symbolic: add execute for owner, remove write for group
chmod -R 755 /opt/myapp/     # Recursive — be careful with -R on mixed dirs/files
chown user:group file        # Change owner and group
chown -R www-data:www-data /var/www/html/
```

---

## 4. Process Model

### Process Lifecycle

```
fork() → child process created (copy of parent)
execve() → child replaces its image with new program
exit() → child terminates, becomes zombie until parent wait()s
wait() → parent reaps zombie, resources freed
```

Every process has:
- **PID**: process ID (unique at any moment)
- **PPID**: parent PID
- **UID/GID**: real, effective, saved set-user-ID (RUID, EUID, SUID)
- **Working directory**: inherited from parent, changeable with `chdir()`/`cd`
- **File descriptor table**: inherits from parent (stdin=0, stdout=1, stderr=2)
- **Environment**: inherited key=value pairs

### Signals

| Signal | Number | Default action | Common use |
|--------|--------|---------------|-----------|
| SIGHUP | 1 | Terminate | Reload config (many daemons) |
| SIGINT | 2 | Terminate | Ctrl+C — graceful interrupt |
| SIGQUIT | 3 | Core dump | Ctrl+\ — quit with core |
| SIGKILL | 9 | **Cannot be caught/ignored** | Last resort termination |
| SIGTERM | 15 | Terminate | Graceful shutdown request |
| SIGSTOP | 19 | **Cannot be caught/ignored** | Suspend process |
| SIGCONT | 18 | Continue | Resume suspended process |
| SIGUSR1/2 | 10/12 | Terminate | App-defined (e.g., log rotation) |

**Agent rule:** Always try `SIGTERM` first. Wait 5–10 seconds. Only escalate to `SIGKILL` if the process doesn't exit.

```bash
kill -TERM <pid>     # Graceful
sleep 10
kill -0 <pid> 2>/dev/null && kill -KILL <pid>   # Escalate only if still alive
```

### Process Inspection

```bash
ps aux                          # All processes, BSD format
ps -eo pid,ppid,user,%cpu,%mem,cmd   # Custom columns
pgrep -u www-data nginx         # Find PIDs by name+user
pstree -p                       # Process tree with PIDs
top -bn1 | head -20             # One-shot snapshot
```

### Job Control

```bash
command &          # Start in background
jobs               # List background jobs
fg %1              # Bring job 1 to foreground
bg %1              # Resume stopped job 1 in background
disown %1          # Detach job from shell (survives shell exit)
nohup cmd &        # Immune to SIGHUP; output to nohup.out
```

---

## 5. Bash Essentials

### Variables and Quoting

```bash
# Assignment: no spaces around =
name="hello world"
count=42

# Double quotes: expands variables and command substitution
echo "Hello $name"          # → Hello hello world
echo "Count: $(( count + 1 ))"  # Arithmetic expansion

# Single quotes: everything literal
echo 'Hello $name'          # → Hello $name

# CRITICAL: Always double-quote variable expansions
rm $file         # DANGEROUS: word-splits on spaces, glob-expands
rm "$file"       # SAFE: treats value as single argument
```

### Parameter Expansion (Safe Patterns)

```bash
${var:-default}      # Use default if var is unset or empty
${var:?error msg}    # Abort with message if unset or empty
${var:+alt}          # Use alt if var is set and non-empty
${#var}              # Length of var
${var%suffix}        # Remove shortest suffix match
${var%%suffix}       # Remove longest suffix match
${var#prefix}        # Remove shortest prefix match
${var##prefix}       # Remove longest prefix match (useful for basenames)
${var/old/new}       # Replace first occurrence
${var//old/new}      # Replace all occurrences

# Safe basename without the basename command:
file="/path/to/script.sh"
echo "${file##*/}"     # → script.sh
echo "${file%.*}"      # → /path/to/script
```

### Arrays

```bash
arr=(one two three "four five")
echo "${arr[0]}"          # → one
echo "${arr[@]}"          # All elements, each as separate word (safe)
echo "${#arr[@]}"         # Number of elements
for item in "${arr[@]}"; do   # Always quote [@] in loops
  echo "$item"
done
```

### Control Flow

```bash
# if: test command exit codes (0 = true, non-zero = false)
if [[ -f "$file" ]]; then
  echo "exists"
elif [[ -d "$file" ]]; then
  echo "is a directory"
else
  echo "not found"
fi

# [[ ]] vs [ ]: prefer [[ ]] — no word splitting, no globbing, no quoting issues with most ops
[[ "$a" == "$b" ]]       # String equality
[[ "$a" =~ ^[0-9]+$ ]]  # Regex match (no quotes around regex)
[[ -z "$var" ]]          # True if empty string
[[ -n "$var" ]]          # True if non-empty

# Arithmetic: use (( )) or $(( ))
(( count++ ))
if (( count > 10 )); then echo "big"; fi

# Loops
for f in /etc/*.conf; do
  echo "$f"
done

while IFS= read -r line; do    # Safe line reading
  echo "$line"
done < /etc/passwd

until [[ -f "/tmp/ready" ]]; do sleep 1; done
```

### Functions

```bash
my_func() {
  local result          # local scope — always use local for function vars
  local input="$1"
  result=$(some_command "$input")
  echo "$result"        # Return value via stdout
  return 0              # Return status via exit code
}

output=$(my_func "arg")
```

### Redirection & Pipes

```bash
cmd > file           # stdout to file (truncate)
cmd >> file          # stdout to file (append)
cmd 2> file          # stderr to file
cmd > file 2>&1      # stdout+stderr to file (order matters: > first, then 2>&1)
cmd 2>&1 | other     # Pipe stdout+stderr
cmd < file           # stdin from file
cmd <<< "string"     # Here-string
cmd <<EOF            # Here-doc
  multiline
  content
EOF
cmd > /dev/null 2>&1 # Discard all output
```

### Exit Codes & Error Handling

```bash
# Check last command's exit code
command
if [[ $? -ne 0 ]]; then echo "failed"; fi

# Idiomatic short-circuit
command || { echo "failed" >&2; exit 1; }
command && echo "succeeded"

# Strict mode — recommended for agent-generated scripts
set -euo pipefail
# -e: exit on any error
# -u: treat unset variables as error
# -o pipefail: pipe fails if any command in it fails

trap 'echo "Error on line $LINENO" >&2' ERR
trap 'cleanup_function' EXIT      # Always runs on exit (even on error)
```

### Command Substitution

```bash
# Preferred form:
output=$(command)

# Old form (avoid — nesting is messy):
output=`command`

# Capture both stdout and stderr:
output=$(command 2>&1)

# Capture with exit code:
output=$(command) || { echo "command failed: $output"; exit 1; }
```

---

## 6. Shell Safety Patterns

> **This section is mandatory reading before generating any shell command that handles user-supplied data, file paths, or external input.**

### Rule 1: Always Quote Variable Expansions

```bash
# UNSAFE: $file may contain spaces, glob characters, or leading dashes
cp $file /dest/

# SAFE: double-quoting prevents word splitting and globbing
cp "$file" /dest/

# ALSO SAFE for arguments that might start with -:
cp -- "$file" /dest/    # -- signals end of options
```

### Rule 2: Never Use `eval` or `shell=True` on Untrusted Input

```bash
# FORBIDDEN — eval executes arbitrary code
eval "$user_input"

# FORBIDDEN — equivalent via python subprocess
subprocess.run(user_input, shell=True)

# SAFE — explicit argument arrays, no shell interpretation
subprocess.run(["cp", source, dest])   # Python
command=( cp "$source" "$dest" )       # Bash array
"${command[@]}"
```

### Rule 3: Validate Paths Before Use

```bash
# Prevent path traversal
validate_path() {
  local path
  path=$(realpath -m "$1")          # Resolve symlinks and ..
  if [[ "$path" != /home/aditya/* && "$path" != /tmp/* ]]; then
    echo "Error: path outside allowed directories: $path" >&2
    return 1
  fi
  echo "$path"
}

safe_path=$(validate_path "$user_provided_path") || exit 1
```

### Rule 4: Use `mktemp` for Temporary Files

```bash
# UNSAFE: predictable name — race condition (TOCTOU), symlink attack
tmpfile=/tmp/myapp_temp.txt

# SAFE: mktemp creates with 0600, random name, atomically
tmpfile=$(mktemp)
tmpdir=$(mktemp -d)

# Always clean up
trap 'rm -f "$tmpfile"' EXIT
trap 'rm -rf "$tmpdir"' EXIT
```

### Rule 5: Prefer Explicit Options Over Positional Ambiguity

```bash
# RISKY: if $filename is "-rf /" due to a bug...
rm $filename

# SAFE: -- marks end of options; file treated as path, not flag
rm -- "$filename"

# For find, use -print0 + xargs -0 to handle filenames with spaces/newlines
find /path -name "*.log" -print0 | xargs -0 rm --
```

### Rule 6: Dry-Run Before Destructive Operations

Before any `rm`, `mv` (overwrite), `dd`, `mkfs`, `truncate`, or write outside `$HOME`:

```bash
# Show what WOULD happen
echo "Would delete: $(find /var/old_data -type f | wc -l) files"
find /var/old_data -type f -ls   # List with details

# Only proceed after review
read -rp "Proceed? [y/N] " confirm
[[ "$confirm" == [yY] ]] || exit 0
```

### Rule 7: Atomic Writes

Never write directly to a file another process might read mid-write:

```bash
# UNSAFE: readers may see partial content
echo "new config" > /etc/app/config.conf

# SAFE: write to temp, then atomic rename
tmp=$(mktemp /etc/app/config.conf.XXXXXX)
echo "new config" > "$tmp"
chmod 644 "$tmp"
mv -f "$tmp" /etc/app/config.conf   # mv on same filesystem = atomic rename(2)
```

### Rule 8: Avoid Parsing `ls`

`ls` output is not safe to parse — filenames can contain newlines, spaces, colors:

```bash
# UNSAFE
for file in $(ls /dir/); do ...

# SAFE: glob directly
for file in /dir/*; do
  [[ -e "$file" ]] || continue   # Handle empty dir
  ...
done

# SAFE: find with -print0
while IFS= read -r -d '' file; do
  ...
done < <(find /dir -maxdepth 1 -type f -print0)
```

### Rule 9: Check Before Acting (TOCTOU Awareness)

```bash
# VULNERABLE: check-then-act race (another process can change state between)
if [[ -f "$file" ]]; then
  cat "$file"    # File might be gone or replaced with symlink
fi

# BETTER: let the command fail and handle the error
cat "$file" 2>/dev/null || echo "file not found or unreadable"

# For critical operations: open the file descriptor, then operate on the fd
exec 3< "$file" || exit 1
# Now use fd 3 — immune to rename/delete of the path
```

### Rule 10: Limit Privilege Scope

```bash
# AVOID running entire scripts as root when only one operation needs it
sudo rm /var/lock/app.lock    # Targeted sudo for one action

# If root is needed for multiple steps, use sudo -i only within a subshell
sudo bash -c '
  chown root:root /opt/app/binary
  chmod 755 /opt/app/binary
'
# Not: sudo su; do everything as root
```

---

## 7. System Administration

### Disk & Filesystem

```bash
df -h                          # Disk space (human-readable)
df -ih                         # Inode usage
du -sh /var/log/               # Size of directory
du -sh /var/log/* | sort -rh | head -20   # Largest subdirs
lsblk                          # Block device tree
lsblk -f                       # Include filesystem info
blkid                          # UUIDs and filesystem types
mount | column -t              # Current mounts, formatted
findmnt                        # Mount tree (cleaner than mount)

# Filesystem check (only on unmounted or read-only filesystem)
fsck -n /dev/sdb1              # Dry-run check, never on live mount
```

### Disk Operations (High Risk — Always Dry-Run First)

```bash
# dd — byte-for-byte copy/write. Wrong target = data destruction.
# Always verify: if=/source of=/dest bs=4M status=progress
dd if=/dev/zero of=/dev/sdb bs=4M status=progress   # WIPES /dev/sdb

# rsync — safe sync with dry-run support
rsync -avhn /source/ /dest/    # -n = dry run, shows what would happen
rsync -avh /source/ /dest/     # Execute after reviewing dry run
```

### User & Group Management

```bash
id                             # Current user's UID, GID, groups
id <username>                  # Another user's identity
getent passwd <username>       # User's /etc/passwd entry (works with LDAP too)
getent group <groupname>       # Group members
useradd -m -s /bin/bash newuser   # Create user with home dir
usermod -aG docker aditya      # Add user to group (append, don't replace)
passwd <username>              # Set/change password
userdel -r olduser             # Delete user and home dir (-r)
```

### File Discovery

```bash
# find — the authoritative tool
find /etc -name "*.conf" -type f                    # By name+type
find /home -user aditya -newer /tmp/reference       # Modified after reference file
find /var/log -mtime +30 -type f                    # Older than 30 days
find / -perm -4000 -type f 2>/dev/null              # SUID files (security audit)
find / -perm -2000 -type f 2>/dev/null              # SGID files

# locate — fast but uses a stale index
updatedb && locate nginx.conf

# which / type — find command location
which python3
type -a python3      # All matches including aliases and builtins
```

---

## 8. Networking

### Interface & Address Inspection

```bash
ip addr show                   # All interfaces and addresses (replaces ifconfig)
ip addr show eth0              # Specific interface
ip route show                  # Routing table
ip route get 8.8.8.8           # Which interface/gateway for a destination
ss -tlnp                       # TCP listening sockets with PIDs (replaces netstat)
ss -tulnp                      # TCP + UDP listening sockets
ss -s                          # Socket summary statistics
```

### Connectivity Testing

```bash
ping -c4 8.8.8.8               # ICMP reachability
traceroute 8.8.8.8             # Hop-by-hop path (use mtr for live view)
mtr --report 8.8.8.8           # Combined ping+traceroute report
dig @8.8.8.8 example.com A     # DNS lookup (authoritative server)
resolvectl query example.com   # Via systemd-resolved
curl -I https://example.com    # HTTP headers only
curl -sv https://example.com 2>&1 | grep -E "^[<>*]"   # TLS + HTTP debug
```

### Firewall (ufw / iptables)

```bash
ufw status verbose             # Current UFW rules
ufw allow 22/tcp               # Allow SSH
ufw deny 8080                  # Block port
iptables -L -n -v              # Raw iptables rules (verbose, numeric)
iptables -L -n -v --line-numbers   # With rule numbers for deletion
```

---

## 9. Logs & Observability

### journald (systemd journal)

```bash
journalctl -xe                         # Recent logs with explanation
journalctl -u nginx                    # Logs for one unit
journalctl -u nginx --since "1 hour ago"
journalctl -f                          # Follow (like tail -f)
journalctl -p err..emerg               # Only errors and above
journalctl --disk-usage                # Journal disk consumption
journalctl --vacuum-size=500M          # Trim to 500MB
```

### Traditional Log Files

```bash
tail -f /var/log/syslog                # System log (live)
tail -n 100 /var/log/auth.log          # Auth events (SSH, sudo)
grep "Failed password" /var/log/auth.log | awk '{print $11}' | sort | uniq -c | sort -rn
# → Top IPs failing SSH login

less +F /var/log/nginx/error.log       # Follow in less (F toggles follow)
zcat /var/log/syslog.1.gz | grep ERROR # Read compressed rotated log
```

### Process & Resource Monitoring

```bash
top                            # Interactive process monitor
htop                           # Enhanced top (may need install)
iotop -ao                      # Disk I/O by process (accumulated)
vmstat 1 5                     # Virtual memory stats, 5 samples 1s apart
iostat -xz 1 5                 # Per-device I/O stats
free -h                        # Memory overview
/proc/meminfo                  # Detailed memory breakdown
sar -u 1 5                     # CPU utilization samples (from sysstat)
```

---

## 10. Package Management

### apt / dpkg (Debian/Ubuntu)

```bash
# Update package index (always before install)
apt update

# Install / remove
apt install -y nginx           # -y: non-interactive
apt remove nginx               # Remove package, keep config
apt purge nginx                # Remove package AND config
apt autoremove                 # Remove orphaned dependencies

# Upgrade
apt upgrade -y                 # Upgrade installed packages
apt full-upgrade -y            # Upgrade + handle dependency changes

# Search and inspect
apt search "python.*redis"
apt show nginx                 # Package metadata
dpkg -l | grep nginx           # Is it installed?
dpkg -L nginx                  # Files installed by package
dpkg -S /usr/bin/python3       # Which package owns this file?

# Hold a package at current version
apt-mark hold nginx
apt-mark showhold

# Non-interactive installs: set DEBIAN_FRONTEND
DEBIAN_FRONTEND=noninteractive apt install -y tzdata
```

### Snap & Flatpak

```bash
snap list
snap install code --classic
snap refresh                   # Update all snaps
flatpak list
flatpak update
```

---

## 11. Systemd & Service Management

### Unit Lifecycle

```bash
systemctl status nginx         # Current state + recent log lines
systemctl start nginx          # Start unit
systemctl stop nginx           # Stop unit
systemctl restart nginx        # Stop then start
systemctl reload nginx         # Send SIGHUP (reload config without restart)
systemctl enable nginx         # Enable at boot (creates symlink in wants/)
systemctl disable nginx        # Disable at boot
systemctl mask nginx           # Prevent start (even manually)
systemctl unmask nginx

# List units
systemctl list-units --type=service --state=running
systemctl list-unit-files --type=service
```

### Inspecting Units

```bash
systemctl cat nginx            # Show unit file(s) with overrides
systemctl show nginx           # All properties (machine-readable)
systemctl show nginx -p MainPID,MemoryCurrent
systemd-analyze blame          # Boot time per unit
systemd-analyze critical-chain # Critical path of boot
```

### Writing a Unit File

```ini
# /etc/systemd/system/myapp.service
[Unit]
Description=My Application
After=network.target
Wants=network-online.target

[Service]
Type=simple
User=myapp
Group=myapp
WorkingDirectory=/opt/myapp
ExecStart=/opt/myapp/bin/myapp --config /etc/myapp/config.yaml
ExecReload=/bin/kill -HUP $MAINPID
Restart=on-failure
RestartSec=5s
StandardOutput=journal
StandardError=journal

# Hardening (recommended)
NoNewPrivileges=yes
PrivateTmp=yes
ProtectSystem=strict
ReadWritePaths=/var/lib/myapp /var/log/myapp

[Install]
WantedBy=multi-user.target
```

```bash
systemctl daemon-reload        # Required after creating/editing unit files
systemctl enable --now myapp   # Enable and start immediately
```

### Timers (Cron Replacement)

```bash
# List active timers
systemctl list-timers

# /etc/systemd/system/cleanup.timer
[Unit]
Description=Run cleanup daily

[Timer]
OnCalendar=daily
Persistent=true   # Run if missed (e.g., system was off)

[Install]
WantedBy=timers.target
```

---

## 12. Agentic Execution Guidelines

> These rules govern how an AI agent should behave when executing Bash commands on behalf of a user in the Icebreaker v1 context.

### 12.1 Risk Classification

Before executing any command, classify it:

| Tier | Examples | Required action |
|------|----------|-----------------|
| **0 — Read-only** | `ls`, `cat`, `grep`, `ps`, `df`, `id`, `ping` | Execute immediately |
| **1 — Reversible write** | `mkdir`, `touch`, `cp` (new file), `apt install` | Execute with output shown |
| **2 — Overwrite/move** | `mv` (existing dest), `cp -f`, config file edits | Show diff/preview; confirm |
| **3 — Destructive** | `rm`, `truncate`, `dd`, `mkfs`, `pkill`, `systemctl stop` | Dry-run first; explicit confirm; 3-second delay |
| **4 — Irreversible system** | `fdisk`, `apt purge`, `userdel -r`, writing to `/dev/*` | Human review required; cannot be automated |

### 12.2 Before Every Command

1. **Expand the command mentally.** What does each argument resolve to? What happens if a variable is empty?
2. **Identify the blast radius.** What is the worst case if this command goes wrong?
3. **Check working directory.** `pwd` before any relative path operation.
4. **Verify target existence.** Does the file/directory/service actually exist?

```bash
# Pattern: verify before acting
[[ -f "$config_file" ]] || { echo "Config not found: $config_file" >&2; exit 1; }
```

### 12.3 Command Construction Rules

```bash
# Always use long-form options in scripts for readability
rm --recursive --force -- "$dir"    # not: rm -rf $dir

# Capture stderr to detect silent failures
output=$(command 2>&1)
status=$?
if [[ $status -ne 0 ]]; then
  echo "Command failed (exit $status): $output" >&2
fi

# Timeout long-running commands
timeout 30 wget -q https://example.com/file.tar.gz
```

### 12.4 Output to the User

Every command sequence should emit:
1. **What it's about to do** (before executing Tier 2+)
2. **What it did** (condensed result, not full dump)
3. **What changed** (explicit: "Created /etc/myapp/config.yaml with 12 lines")
4. **Next step required** (if any — e.g., "restart the service to apply")

### 12.5 Idempotency

Design commands to be safe to run twice. Idempotent patterns:

```bash
# Directory: create only if missing
mkdir -p /opt/myapp/data

# File: only write if content differs
new_content="server_port=8080"
current=$( cat /etc/myapp/config 2>/dev/null )
if [[ "$current" != "$new_content" ]]; then
  echo "$new_content" > /etc/myapp/config
fi

# apt install: already idempotent (no-op if installed)
apt install -y curl

# systemctl enable: idempotent
systemctl enable nginx   # No-op if already enabled

# User creation: check first
id newuser &>/dev/null || useradd -m newuser
```

### 12.6 Rollback Planning

For any Tier 2–3 operation, establish a rollback before proceeding:

```bash
# Before editing a config file
backup_file="/etc/nginx/nginx.conf.bak.$(date +%Y%m%d_%H%M%S)"
cp /etc/nginx/nginx.conf "$backup_file"
echo "Backup at $backup_file"

# Make the change
sed -i 's/worker_processes auto/worker_processes 4/' /etc/nginx/nginx.conf

# Test the change
nginx -t || {
  echo "Config invalid — restoring backup" >&2
  cp "$backup_file" /etc/nginx/nginx.conf
  exit 1
}

# Apply
systemctl reload nginx
```

### 12.7 Avoiding Common Agent Mistakes

| Mistake | Consequence | Prevention |
|---------|-------------|-----------|
| `rm -rf $DIR` with unquoted/unset `$DIR` | `rm -rf /` or `rm -rf ""` = rm current dir contents | `set -u`; always quote; validate non-empty |
| Piping to `bash` without inspection | Arbitrary code execution from network | Inspect scripts before running; use checksums |
| Running `apt upgrade` during a task | Unexpected system changes, service restarts | Explicitly limit to `apt install <specific-package>` |
| Writing to `/etc/` without backup | Unrecoverable misconfiguration | Always backup first (see 12.6) |
| Forgetting `2>&1` when capturing output | Stderr lost; silent failures | Capture both; check `$?` |
| Assuming a service is running | `systemctl reload` fails if not running | Check `systemctl is-active` first |
| Executing as root by default | Maximum blast radius | Run as least-privileged user; use targeted `sudo` |
| Chaining commands with `&&` without checking | Later commands run on unexpected state | Log intermediate results; validate at each step |

### 12.8 Signals for Safe Long-Running Operations

When spawning or managing long-running commands:

```bash
# Start in background, capture PID
long_command &
cmd_pid=$!

# Wait with timeout
if ! wait_with_timeout "$cmd_pid" 300; then   # 300s timeout
  kill -TERM "$cmd_pid"
  sleep 5
  kill -0 "$cmd_pid" 2>/dev/null && kill -KILL "$cmd_pid"
  echo "Command timed out and was terminated" >&2
  exit 1
fi

wait_with_timeout() {
  local pid=$1 timeout=$2 elapsed=0
  while kill -0 "$pid" 2>/dev/null; do
    (( elapsed++ ))
    (( elapsed >= timeout )) && return 1
    sleep 1
  done
  return 0
}
```

---

## Quick Reference Cheatsheet

```bash
# Identity & Environment
id; whoami; env; set; export VAR=value; unset VAR

# Navigation
pwd; cd -; pushd /dir; popd; ls -lahF; tree -L 2

# File Operations (Safe Forms)
cp -- "$src" "$dst"
mv -- "$src" "$dst"
rm -- "$file"                    # Single file
rm -r -- "$dir"                  # Directory (always -r explicitly, not -rf)
ln -s target linkname            # Symlink
readlink -f "$link"              # Resolve to real path

# Text Processing
grep -rn "pattern" /path/        # Recursive, with line numbers
grep -E "regex"                  # Extended regex
sed -n '10,20p' file             # Print lines 10-20
awk '{print $2}' file            # Second field
cut -d: -f1 /etc/passwd          # First colon-delimited field
sort -k2 -n file                 # Sort by second field numerically
uniq -c | sort -rn               # Count occurrences, sort descending
wc -l file                       # Line count
head -n 20 / tail -n 20          # First/last 20 lines
diff file1 file2                 # Differences
diff -u file1 file2 | head -40   # Unified diff, first 40 lines

# Process
ps aux; pgrep nginx; pkill -TERM nginx; kill -0 $pid
nohup cmd > /var/log/cmd.log 2>&1 &

# Network
ip addr; ss -tlnp; curl -sSf URL; wget -q URL

# Disk
df -h; du -sh /path; lsblk; findmnt

# Systemd
systemctl {start,stop,restart,reload,enable,disable,status} unit
journalctl -u unit -f --since "1h ago"
```

---

*Document version: 1.0 — June 2026. Maintained as part of Icebreaker v1 Bash CLI reference. Update this file when new patterns are validated or anti-patterns discovered in practice.*
