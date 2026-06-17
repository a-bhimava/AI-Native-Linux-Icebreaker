# Linux: The Complete Guide
### From First Principles to Production Mastery

*A comprehensive reference for system administrators, developers, and AI agents operating in Linux environments.*

---

**Edition:** 2026  
**Coverage:** Ubuntu 24.04 LTS / Debian 12 / RHEL 9 family  
**Kernel baseline:** 6.x  

---

## Table of Contents

**Part I — Foundations**
- Chapter 1: Linux Philosophy, History, and Distributions
- Chapter 2: Installation and the Boot Process

**Part II — The Command Line Interface**
- Chapter 3: Shell Basics and Navigation
- Chapter 4: Files, Directories, and the Filesystem

**Part III — Text and Data**
- Chapter 5: Text Processing — grep, awk, sed, and Friends
- Chapter 6: Users, Groups, and Permissions

**Part IV — System Control**
- Chapter 7: Processes, Signals, and Job Control
- Chapter 8: Storage, Partitions, and Filesystems

**Part V — System Administration**
- Chapter 9: Software Management — apt, snap, and Flatpak
- Chapter 10: Networking — Concepts and Configuration

**Part VI — Services and Observability**
- Chapter 11: Systemd — Services, Timers, and the Boot Process
- Chapter 12: Logs, Monitoring, and Observability

**Part VII — Security**
- Chapter 13: Linux Security Fundamentals
- Chapter 14: Firewalls, SSH, and Network Security

**Part VIII — Automation**
- Chapter 15: Bash Scripting — Variables, Control Flow, and Functions
- Chapter 16: Advanced Scripting — Error Handling, Testing, and Best Practices

**Part IX — Modern Linux**
- Chapter 17: Containers with Docker and Podman
- Chapter 18: Performance Tuning and Troubleshooting

---

## Preface

Linux powers an extraordinary breadth of computing: smartphones running Android, the world's top supercomputers, most web servers on the internet, embedded systems in cars and routers, and increasingly the AI infrastructure that is reshaping every industry. Despite this diversity, a single coherent philosophy unites all of it — and that philosophy is worth understanding before touching a single command.

This book is written for people who want to understand Linux deeply, not just memorize commands. Every chapter explains the *why* before the *how*. Commands are presented in context, not as isolated incantations. Security considerations are woven throughout, not relegated to a single chapter. Modern practices — systemd, containers, eBPF observability, rootless workloads — are treated as first-class topics alongside the fundamentals that have been stable for decades.

Whether you are a developer who needs to manage servers, a sysadmin expanding into automation, or an AI agent executing shell commands on behalf of users, this guide will give you the conceptual grounding to reason about what Linux is doing — and to act safely and effectively within it.

---

# PART I — FOUNDATIONS

---

## Chapter 1: Linux Philosophy, History, and Distributions

### 1.1 What Linux Actually Is

Linux is a kernel — the core program that sits between hardware and software, managing CPU time, memory, devices, and the abstract interfaces that every other program uses. When people say "Linux" colloquially, they usually mean a *Linux distribution*: a bundled collection of the kernel plus a userland (shell, utilities, init system, package manager, and optionally a desktop environment). The kernel itself was created by Linus Torvalds in 1991 as a hobby project and released under the GNU General Public License (GPL), which requires that modifications be shared back under the same terms.

The distinction matters for practical reasons. If a program doesn't work, the bug might be in the kernel, in a library provided by your distribution, in the distribution's configuration, or in the program itself. Understanding the layers — kernel, libc, shell, utilities, application — gives you a map for diagnosing problems.

### 1.2 The Unix Philosophy

Linux inherits its design thinking from Unix, developed at Bell Labs in the late 1960s. The Unix philosophy, as articulated by Doug McIlroy, has three rules:

1. **Write programs that do one thing and do it well.**
2. **Write programs to work together.**
3. **Write programs that handle text streams, because that is a universal interface.**

These rules explain why the Linux command line looks the way it does. Tools like `grep`, `sort`, `awk`, `cut`, and `wc` each do exactly one thing. They read from standard input and write to standard output, which means you can chain them with pipes (`|`) into arbitrarily powerful data pipelines without any of them needing to know about the others. This composability is not incidental — it is the design.

A corollary principle, the "rule of silence," holds that programs should produce no output if they have nothing interesting to say. A command that succeeds should exit silently; output is reserved for results and errors. This is why so many Linux commands appear to do nothing when they succeed — silence means success.

### 1.3 Free Software and Open Source

Linux is inseparable from the free software movement founded by Richard Stallman in 1983. Stallman's GNU project (GNU's Not Unix) produced the compilers, libraries, and utilities that surround the Linux kernel. The philosophy asserts four freedoms: to run the program, to study and modify the source code, to redistribute copies, and to distribute modified versions.

The "open source" framing, adopted in 1998, focuses on the practical engineering benefits of public source code (peer review, community contribution) rather than the ethical dimension. Most distributions blend components under various compatible licenses — GPL, LGPL, MIT, Apache, BSD — and practically speaking, the Linux ecosystem is the largest collaborative software development project in history.

### 1.4 A Brief History

| Year | Event |
|------|-------|
| 1969 | Unix created at Bell Labs by Thompson and Ritchie |
| 1983 | GNU Project launched by Richard Stallman |
| 1991 | Linus Torvalds releases Linux 0.01 |
| 1993 | Debian and Slackware founded; first practical distributions |
| 1994 | Red Hat Linux released; commercial Linux begins |
| 1996 | Linux kernel 2.0 adds SMP (multi-CPU) support |
| 2000 | IBM invests $1 billion in Linux; enterprise adoption accelerates |
| 2004 | Ubuntu founded by Mark Shuttleworth; focus on desktop usability |
| 2008 | Android launches, running Linux kernel on mobile |
| 2011 | Linux kernel 3.0; systemd begins replacing SysVinit |
| 2015 | Microsoft ships Windows Subsystem for Linux (WSL) |
| 2019 | Microsoft joins the Linux Foundation |
| 2022 | Linux 6.0; eBPF, io_uring, and Landlock mature as kernel features |
| 2024 | Ubuntu 24.04 LTS ("Noble Numbat"); Linux dominates cloud infrastructure |

### 1.5 The Linux Kernel

The kernel is monolithic — all core functionality (scheduling, memory management, drivers, networking, filesystems) runs in a single privileged address space called *kernel space*. This contrasts with microkernels (like Mach or L4) that run services in separate processes.

The kernel exposes its services to user programs through **system calls** (syscalls) — the only legal way for user-space code to request privileged operations. When you open a file, `open(2)` is a syscall. When you create a process, `fork(2)` is a syscall. When you write to a socket, `write(2)` is a syscall. Everything else in the Linux userland is built on these ~300 syscalls.

Kernel modules allow drivers and filesystems to be loaded and unloaded without rebooting. `lsmod` lists loaded modules; `modprobe` loads them; `rmmod` removes them. Most hardware drivers on desktop Linux are loaded automatically via udev when devices are detected.

### 1.6 Major Linux Distributions

Distributions are grouped by lineage. The lineage determines the package format, the package manager, the default init system, and many configuration conventions.

**Debian family:**
- **Debian** — the "universal operating system"; stable, conservative, community-driven. Releases every ~2 years. The reference distribution from which Ubuntu derives.
- **Ubuntu** — the most widely used desktop and server Linux. LTS releases every 2 years with 5-year support. Adds hardware support, polish, and Snap integration over Debian. Versions: 22.04 Jammy, 24.04 Noble.
- **Linux Mint** — Ubuntu-based; popular for users migrating from Windows due to familiar desktop.
- **Kali Linux** — Debian-based; purpose-built for penetration testing and security research.

**Red Hat family:**
- **RHEL (Red Hat Enterprise Linux)** — the dominant enterprise Linux. Subscription-based with commercial support. The reference for enterprise deployments and certifications.
- **AlmaLinux / Rocky Linux** — free, binary-compatible rebuilds of RHEL, filling the gap left by CentOS's end-of-life in 2021.
- **Fedora** — the community upstream for RHEL; first to ship new technologies (Wayland, pipewire, Btrfs). Cutting-edge but shorter lifecycle.

**SUSE family:**
- **openSUSE** — community distribution with two variants: Leap (stable, RHEL-comparable) and Tumbleweed (rolling release).
- **SLES (SUSE Linux Enterprise Server)** — enterprise counterpart to openSUSE Leap.

**Independent:**
- **Arch Linux** — rolling release; minimal base; users build their system from scratch. Excellent documentation (the ArchWiki is authoritative for all Linux users).
- **Gentoo** — source-based; everything compiled from scratch; maximum optimization at the cost of time.
- **NixOS** — declarative, reproducible configuration; the entire system state defined in a single Nix expression.
- **Alpine Linux** — tiny (< 10 MB) musl-based distribution; default in many Docker images due to size.

**Choosing a distribution** for server work: Ubuntu LTS and RHEL/AlmaLinux dominate. Ubuntu wins on ease of use and tooling; RHEL wins on enterprise support contracts and certification requirements. For containers, Alpine is the default base image. For security research, Kali. For desktop, Ubuntu, Fedora, or Linux Mint are the most accessible starting points.

### 1.7 The Linux Desktop

The Linux desktop is built in layers: the kernel's DRM/KMS subsystem manages the GPU; a display server (historically X.Org, now increasingly **Wayland**) provides the protocol for drawing windows; a **desktop environment** (GNOME, KDE Plasma, XFCE, etc.) provides the window manager, panel, file manager, and application suite.

Wayland has been the preferred display server since 2021 for most major distributions. It is more secure than X11 because applications cannot spy on each other's input or windows. GNOME runs Wayland by default since GNOME 40 (2021); KDE Plasma since version 6 (2024).

For servers and headless systems, no desktop environment is needed. Remote access is via SSH, and graphical applications (when needed) can be forwarded over SSH with X11 forwarding or accessed via VNC/RDP.

---

## Chapter 2: Installation and the Boot Process

### 2.1 Before You Install

**Hardware requirements (Ubuntu 24.04 LTS):**
- CPU: 2 GHz dual-core or better (64-bit x86 or ARM64)
- RAM: 4 GB minimum; 8 GB recommended for desktop
- Storage: 25 GB minimum; 50+ GB recommended
- UEFI firmware: recommended; legacy BIOS supported

**Before installing on bare metal:**
1. Back up all data.
2. Note whether the machine uses UEFI or legacy BIOS (check the firmware menu at boot, usually F2/Del/F12).
3. Download the ISO from the official distribution website. Always verify the checksum:
   ```bash
   sha256sum ubuntu-24.04-desktop-amd64.iso
   # Compare with the value published on the download page
   ```
4. Write the ISO to a USB drive. On Linux: `sudo dd if=ubuntu-24.04.iso of=/dev/sdX bs=4M status=progress && sync`. On macOS: use `diskutil unmountDisk` first. On Windows: use Rufus or balenaEtcher.

### 2.2 Disk Partitioning

Modern Linux installations need at minimum:

| Partition | Mount point | Filesystem | Minimum size | Notes |
|-----------|------------|------------|-------------|-------|
| EFI System Partition | `/boot/efi` | FAT32 | 512 MB | UEFI systems only |
| Root | `/` | ext4 or Btrfs | 20 GB | Contains OS and programs |
| Swap | (swap) | swap | RAM size or 2× | Used for hibernation and overflow |
| Home (optional) | `/home` | ext4 or Btrfs | Remainder | Separating /home survives OS reinstalls |

**Filesystem choice in 2026:**
- **ext4** — battle-tested, stable, excellent tool support. The safe default.
- **Btrfs** — copy-on-write, built-in snapshots, RAID support, transparent compression. Ubuntu now defaults to Btrfs on desktop. Excellent for systems where you want snapshot-based rollback (with tools like Timeshift).
- **XFS** — high-performance, especially for large files. Default on RHEL/AlmaLinux.
- **ZFS** — enterprise-grade with deduplication, encryption, and native RAID. Requires the `zfsutils` package on Ubuntu (licensed separately from the kernel).

**LVM (Logical Volume Manager):** A layer between physical disks and filesystems that allows resizing volumes without repartitioning. Useful in server environments. The installer can set this up automatically.

**Encryption:** LUKS (Linux Unified Key Setup) provides full-disk encryption at the block device level. Ubuntu's installer offers this as a checkbox. Enable it for any machine that might be physically stolen (laptops especially).

### 2.3 The Installation Process (Ubuntu)

1. Boot from the USB drive. The installer detects UEFI/BIOS and presents either GRUB (text boot menu) or a graphical splash.
2. Select language, keyboard layout, and timezone.
3. Choose **Normal installation** (full desktop) or **Minimal installation** (browser + basics only). For servers, use the Server ISO instead.
4. On the Installation type screen: "Erase disk and install Ubuntu" is simplest. "Something else" gives full partitioning control.
5. Set your username, hostname, and password. The installer creates your account and sets up `sudo` access.
6. Installation takes 10–20 minutes. On completion, remove the USB and reboot.

### 2.4 The Boot Process in Detail

Understanding what happens between pressing the power button and seeing a login prompt is essential for diagnosing boot failures and configuring boot-time behavior.

**Stage 1: Firmware (UEFI or BIOS)**

When power is applied, the CPU executes code from a fixed address in non-volatile ROM. On modern machines this is UEFI firmware. UEFI performs the Power-On Self-Test (POST), initializes hardware, and looks for a bootloader in the EFI System Partition (the FAT32 partition mounted at `/boot/efi`). The boot entry order is configurable in the UEFI menu (`efibootmgr -v` from Linux shows the current order).

On legacy BIOS systems, the firmware reads the first 512 bytes of the boot disk (the Master Boot Record, MBR), which contains a small bootloader. The MBR bootloader then loads a larger stage-2 bootloader from disk.

**Stage 2: GRUB**

GRUB (GNU GRand Unified Bootloader) is the standard Linux bootloader. Its job: present a menu (briefly, often hidden), load the kernel image and initramfs into memory, pass kernel parameters, and transfer control to the kernel.

Key GRUB files:
```
/boot/grub/grub.cfg        — generated config (do not edit manually)
/etc/default/grub          — user-editable defaults
/etc/grub.d/               — scripts that generate grub.cfg
```

Edit `/etc/default/grub` to change behavior (e.g., increase timeout, add kernel parameters), then regenerate:
```bash
sudo update-grub                    # Debian/Ubuntu
sudo grub2-mkconfig -o /boot/grub2/grub.cfg   # RHEL/Fedora
```

Common `GRUB_CMDLINE_LINUX_DEFAULT` parameters:
- `quiet splash` — suppress boot messages, show splash screen
- `nomodeset` — disable KMS (use for graphics troubleshooting)
- `systemd.unit=rescue.target` — boot to rescue mode
- `ro` — mount root filesystem read-only initially (standard)
- `init=/bin/bash` — start a bare shell instead of init (emergency recovery)

**Stage 3: The Kernel and initramfs**

GRUB loads two files: the compressed kernel image (`vmlinuz-*`) and the initial RAM filesystem (`initramfs-*` or `initrd-*`). The initramfs is a small compressed archive that is extracted into a temporary in-memory filesystem. The kernel mounts this as its initial root filesystem.

The initramfs contains the minimum tools needed to mount the real root filesystem: disk drivers, filesystem modules, LVM tools, LUKS decryption code, and an early `init` script. If the root filesystem is on LVM, RAID, or an encrypted device, the initramfs handles all of that before handing off to the real root.

Once the real root filesystem is mounted at `/`, the kernel executes the first user-space process: `/sbin/init`. On modern Linux, this is `systemd`.

**Stage 4: systemd**

systemd is PID 1 — the first process, the parent of all other processes, and the last process to exit when the system shuts down. It reads its configuration from unit files and orchestrates the parallel startup of all system services.

systemd's startup proceeds through a sequence of *targets* (analogous to the old SysV runlevels):

```
sysinit.target     → basic.target     → multi-user.target
                                      ↘ graphical.target
```

- `sysinit.target`: mounts filesystems listed in `/etc/fstab`, brings up device management (udev), sets up the hostname
- `basic.target`: starts essential services (logging, sockets, timers)
- `multi-user.target`: starts all normal services; the system is usable for login
- `graphical.target`: starts the display manager (GDM, SDDM); adds the desktop

The entire startup is logged to the systemd journal. `journalctl -b` shows logs from the current boot; `journalctl -b -1` shows the previous boot (useful after a crash).

**Analyzing boot time:**
```bash
systemd-analyze                    # Total time breakdown
systemd-analyze blame              # Per-unit time, sorted
systemd-analyze critical-chain     # The critical path (what actually delayed boot)
systemd-analyze plot > boot.svg    # SVG waterfall diagram
```

### 2.5 Post-Installation Steps

After a fresh install, perform these steps before the machine does any real work:

```bash
# 1. Update all packages
sudo apt update && sudo apt full-upgrade -y

# 2. Install essential tools
sudo apt install -y curl wget git vim htop tmux ufw fail2ban unattended-upgrades

# 3. Enable automatic security updates
sudo dpkg-reconfigure --priority=low unattended-upgrades

# 4. Configure the firewall (deny inbound by default, allow SSH)
sudo ufw default deny incoming
sudo ufw default allow outgoing
sudo ufw allow ssh
sudo ufw enable
sudo ufw status verbose

# 5. Set the hostname
sudo hostnamectl set-hostname myserver

# 6. Verify system time (NTP)
timedatectl status
# If not synced:
sudo timedatectl set-ntp true
```

### 2.6 Virtual Machines and Cloud Instances

For development and testing, you rarely install Linux on bare metal. The common alternatives:

**VirtualBox / VMware / GNOME Boxes:** Type-2 hypervisors that run on your existing OS. Install via the distributor's website. Performance is adequate for most development work. Use *snapshots* liberally — they let you roll back to a known-good state in seconds.

**KVM/QEMU:** The native Linux hypervisor, type-1 performance. Used by all major cloud providers. On Ubuntu: `sudo apt install qemu-kvm libvirt-daemon-system virt-manager`. `virt-manager` provides a GUI; `virsh` provides a CLI; `virt-install` creates VMs from the command line.

**WSL 2 (Windows Subsystem for Linux):** A real Linux kernel running inside a lightweight VM on Windows 10/11. Excellent for development on Windows machines. Install from the Microsoft Store or: `wsl --install`. Runs Ubuntu by default; other distributions available.

**Cloud instances (AWS EC2, GCP Compute Engine, Azure VMs):** The most common way to run Linux in production. Images are pre-built; you SSH in after creation. Key differences from bare metal:
- No GRUB interaction; kernel parameters are set via instance user data or cloud-init
- Storage is often ephemeral (lost on stop) or EBS/persistent volume
- Networking is managed by the cloud provider's VPC; `ufw` is secondary to security groups
- Cloud-init handles first-boot configuration (hostname, SSH keys, package installs)

**Container-based Linux (Docker, Podman):** Containers share the host kernel and provide filesystem isolation. They are not VMs. Chapter 17 covers this in depth.

### 2.7 Kernel Parameters and `/proc/cmdline`

After boot, the parameters the kernel was given are visible at:
```bash
cat /proc/cmdline
```
This is useful for verifying that parameters you set in GRUB were actually passed to the kernel.

The running kernel version is:
```bash
uname -r        # e.g., 6.8.0-45-generic
uname -a        # Full info: kernel, hostname, architecture
```

Multiple kernels can be installed simultaneously. `dpkg --list | grep linux-image` shows installed kernels; GRUB's menu lets you boot into older ones (useful after a kernel update breaks something).

---

# PART II — THE COMMAND LINE INTERFACE

---
## Chapter 3: Shell Basics and Navigation

### 3.1 What the Shell Is

The shell is a command interpreter — a program that reads text you type, parses it into commands and arguments, and asks the kernel to execute them. It is also a programming language: you can write scripts, define functions, use variables and control flow, and automate entire workflows. Most Linux systems offer several shells. The default on Ubuntu and Debian is **Bash** (Bourne Again Shell), and this book uses Bash throughout.

When you open a terminal, the shell starts, prints a **prompt**, and waits for input. The prompt typically looks like:

```
aditya@myserver:~$
```

This breaks down as `user@hostname:current_directory$`. The `$` indicates you are a regular user; `#` indicates root. The `~` is shorthand for your home directory.

### 3.2 Anatomy of a Command

Every shell command follows the same structure:

```
command  [options]  [arguments]
   ↑         ↑          ↑
  what    how to      what to
  to run   run it    run it on
```

For example:
```bash
ls   -lh   /var/log
```
- `ls` — list directory contents
- `-lh` — long format (`-l`), human-readable sizes (`-h`)
- `/var/log` — the directory to list

Options come in two forms:
- Short: single dash + single letter (`-l`, `-h`, `-a`). Can be combined: `-lha`
- Long: double dash + word (`--long`, `--human-readable`, `--all`). Cannot be combined.

The `--` marker signals the end of options. Everything after it is treated as an argument, even if it starts with a dash. This matters for filenames that start with `-`:
```bash
rm -- -strange-filename    # Safe
```

### 3.3 Getting Help

```bash
man ls              # Manual page for ls — comprehensive reference
man 5 passwd        # Section 5: file formats (passwd file format, not the command)
man -k "disk usage" # Search manual page names and descriptions
info coreutils      # GNU info pages — often more detailed than man
ls --help           # Inline help — quick reminder of options
type ls             # Is ls a builtin, alias, function, or external command?
which python3       # Full path of the executable
```

Man pages are organized in sections. Section 1 is user commands; 2 is syscalls; 3 is library functions; 5 is file formats; 8 is admin commands. When the section matters, write `man 2 open` (the syscall) vs `man 1 open` (the command).

### 3.4 Navigating the Filesystem

```bash
pwd                  # Print working directory
cd /etc              # Change to absolute path
cd logs              # Change to relative path (relative to pwd)
cd ..                # Go up one level
cd ../..             # Go up two levels
cd ~                 # Go to home directory
cd -                 # Go to previous directory (toggle)
cd ~aditya           # Go to another user's home directory
```

**Listing directory contents:**
```bash
ls                   # Basic listing
ls -l                # Long format: permissions, owner, size, date, name
ls -lh               # Long format with human-readable sizes (KB, MB, GB)
ls -la               # Include hidden files (names starting with .)
ls -lt               # Sort by modification time, newest first
ls -ltr              # Sort by modification time, oldest first (reverse)
ls -R                # Recursive listing
ls -d */             # List only directories
```

Long-format output explained:
```
-rw-r--r-- 1 aditya aditya 4096 Jun 1 10:00 file.txt
↑           ↑ ↑      ↑      ↑    ↑           ↑
type+perms  links owner  group  size  date      name
```

### 3.5 Tab Completion

Tab completion is the single most productive feature of the shell. Press `Tab` once to complete an unambiguous prefix; press it twice to show all matches when there are multiple options.

```bash
cd /usr/lo<Tab>         # Completes to /usr/local/
systemctl sta<Tab>      # Completes to systemctl start
systemctl start ngi<Tab> # Completes to systemctl start nginx
```

Bash completion is extended by the `bash-completion` package (`sudo apt install bash-completion`), which adds context-aware completion for hundreds of programs — it knows to complete hostnames after `ssh`, branch names after `git checkout`, package names after `apt install`, and so on.

### 3.6 Command History

Bash stores command history in `~/.bash_history`. By default, 1000 commands are remembered (configurable via `HISTSIZE` and `HISTFILESIZE`).

```bash
history              # Print numbered history
history 20           # Last 20 commands
!!                   # Repeat last command
!ssh                 # Repeat most recent command starting with "ssh"
!42                  # Repeat command number 42
^old^new             # Repeat last command with "old" replaced by "new"
```

**Reverse history search (Ctrl+R):** The most powerful history feature. Press `Ctrl+R` and type part of a previous command; Bash searches backwards through history for a match. Press `Ctrl+R` again to cycle to the next match. Press `Enter` to execute, or the right arrow to edit first.

**History expansion settings** (add to `~/.bashrc`):
```bash
HISTSIZE=10000              # Remember 10,000 commands in memory
HISTFILESIZE=20000          # Save 20,000 commands to file
HISTCONTROL=ignoredups      # Don't save duplicate consecutive commands
HISTTIMEFORMAT="%F %T "     # Timestamp each history entry
shopt -s histappend         # Append to history file, don't overwrite
```

### 3.7 Readline Shortcuts

Bash uses the Readline library for line editing. These shortcuts work in any Readline-aware program (bash, gdb, python REPL, etc.):

| Shortcut | Action |
|----------|--------|
| `Ctrl+A` | Move to beginning of line |
| `Ctrl+E` | Move to end of line |
| `Ctrl+F` / `→` | Move forward one character |
| `Ctrl+B` / `←` | Move backward one character |
| `Alt+F` | Move forward one word |
| `Alt+B` | Move backward one word |
| `Ctrl+W` | Delete word before cursor |
| `Alt+D` | Delete word after cursor |
| `Ctrl+U` | Delete from cursor to beginning of line |
| `Ctrl+K` | Delete from cursor to end of line |
| `Ctrl+Y` | Paste (yank) last deleted text |
| `Ctrl+L` | Clear screen (like `clear`) |
| `Ctrl+C` | Interrupt current command |
| `Ctrl+Z` | Suspend current command (send to background) |
| `Ctrl+D` | Send EOF / exit shell |

### 3.8 Variables and the Environment

```bash
# Shell variables: exist only in current shell
name="Aditya"
echo $name
echo "$name"          # Always quote variable references

# Environment variables: inherited by child processes
export PATH="$PATH:/opt/myapp/bin"
export EDITOR=vim
export PAGER=less

# View all environment variables
env
printenv              # Same thing
printenv PATH         # Single variable

# Common important variables
echo $HOME            # Home directory path
echo $USER            # Current username
echo $SHELL           # Path to current shell
echo $PATH            # Colon-separated list of directories searched for commands
echo $PWD             # Current directory (same as pwd)
echo $OLDPWD          # Previous directory (same as cd -)
echo $?               # Exit code of last command (0 = success)
echo $$               # PID of current shell
echo $!               # PID of last background command
```

**Modifying PATH:** Always append to `$PATH` rather than replacing it:
```bash
export PATH="$HOME/.local/bin:$PATH"    # Add personal bin directory
export PATH="$PATH:/opt/newtool/bin"    # Append at end (lower priority)
```

### 3.9 Shell Configuration Files

Bash reads different files depending on how it starts:

| File | When read |
|------|-----------|
| `/etc/profile` | Login shell, all users |
| `~/.profile` | Login shell, user-specific |
| `/etc/bash.bashrc` | Interactive non-login shell, all users |
| `~/.bashrc` | Interactive non-login shell, user-specific |
| `~/.bash_logout` | On exit from login shell |

A **login shell** is started when you log in via SSH, console, or `su -`. A **non-login interactive shell** is opened when you open a terminal emulator inside a desktop session. Most configuration (aliases, functions, prompt, PATH additions) goes in `~/.bashrc`. Shell-specific environment variables for login sessions go in `~/.profile`.

After editing `~/.bashrc`, apply changes to the current shell:
```bash
source ~/.bashrc
# or equivalently:
. ~/.bashrc
```

### 3.10 Aliases

Aliases are shortcuts that expand to longer commands:

```bash
# Define
alias ll='ls -lhF'
alias la='ls -lhFA'
alias ..='cd ..'
alias ...='cd ../..'
alias grep='grep --color=auto'
alias df='df -h'
alias du='du -h'
alias please='sudo'

# Use
ll /etc

# List all aliases
alias

# Remove an alias
unalias ll

# Bypass an alias (use the real command)
\ls            # Backslash prefix
command ls     # 'command' builtin
```

Add aliases to `~/.bashrc` to make them permanent.

### 3.11 Pipes and Redirection

The pipe operator `|` connects the stdout of one command to the stdin of the next, creating a pipeline:

```bash
ls -la /etc | grep conf              # List /etc, keep only lines containing "conf"
ps aux | grep nginx | grep -v grep   # Find nginx processes
cat /var/log/syslog | grep ERROR | tail -20   # Last 20 errors
```

Redirection operators route stdin/stdout/stderr to/from files:

```bash
command > file          # Write stdout to file (truncate)
command >> file         # Append stdout to file
command 2> file         # Write stderr to file
command > file 2>&1     # Write stdout AND stderr to file
command &> file         # Same (Bash shorthand)
command < file          # Read stdin from file
command <<< "string"    # Feed a string as stdin (here-string)
command > /dev/null     # Discard stdout
command 2>/dev/null     # Discard stderr (silence error messages)
command &>/dev/null     # Discard all output
```

The `tee` command reads stdin and writes to both stdout and a file simultaneously:
```bash
make 2>&1 | tee build.log    # See output AND save it
```

### 3.12 Command Chaining

```bash
cmd1 ; cmd2         # Run cmd2 after cmd1, regardless of exit code
cmd1 && cmd2        # Run cmd2 only if cmd1 succeeds (exit code 0)
cmd1 || cmd2        # Run cmd2 only if cmd1 fails (exit code non-zero)
(cmd1 ; cmd2)       # Run in a subshell
{ cmd1 ; cmd2 ; }   # Run in current shell (note: space and ; required)
```

Real-world patterns:
```bash
cd /tmp && rm -rf ./build && mkdir build    # Abort if any step fails
apt update || { echo "Update failed"; exit 1; }
```

---

## Chapter 4: Files, Directories, and the Filesystem

### 4.1 Everything Is a File

In Linux, almost everything is represented as a file. Regular files hold data; directories are files that list other files; devices (`/dev/sda`, `/dev/tty`) are files; named pipes and sockets are files; even running processes have a virtual file representation in `/proc`. This uniformity means the same tools — `open`, `read`, `write`, `close` — work for nearly everything.

### 4.2 The Filesystem Hierarchy Standard

The **FHS** (Filesystem Hierarchy Standard) defines where things live. Every distribution follows it (with minor variations).

```
/               The root — the top of the entire tree
├── bin/        Essential user programs (ls, cp, bash). Symlink → /usr/bin on modern systems
├── boot/       Kernel, initramfs, GRUB. Do not modify manually
├── dev/        Device files, managed by udev and the kernel
├── etc/        System-wide configuration files (text-based)
├── home/       User home directories (/home/username/)
├── lib/        Shared libraries needed by /bin and /sbin
├── media/      Auto-mount points for removable media (USB drives, DVDs)
├── mnt/        Temporary mount points for manual mounting
├── opt/        Self-contained third-party software packages
├── proc/       Virtual filesystem exposing kernel and process state (read-only for users)
├── root/       Home directory for the root user
├── run/        Runtime data (PIDs, sockets). Cleared at boot
├── sbin/       System administration binaries (for root). Symlink → /usr/sbin
├── srv/        Data served by services (web root, FTP data)
├── sys/        Virtual filesystem for kernel objects and hardware (sysfs)
├── tmp/        Temporary files. World-writable, sticky bit. Often cleared at boot
├── usr/        The bulk of installed software
│   ├── bin/    Non-essential user programs
│   ├── include/ C header files
│   ├── lib/    Libraries for /usr/bin and /usr/sbin
│   ├── local/  Locally compiled software (make install puts things here)
│   ├── sbin/   Non-essential admin programs
│   └── share/  Architecture-independent data (man pages, icons, locale data)
└── var/        Variable data that changes during operation
    ├── cache/  Application cache data
    ├── lib/    Persistent application state (database files, package manager state)
    ├── log/    Log files
    ├── mail/   User mailboxes
    ├── run/    Old location; now symlink → /run
    ├── spool/  Print queues, cron jobs
    └── tmp/    Temporary files that persist across reboots
```

### 4.3 Creating and Removing Files and Directories

```bash
# Create files
touch newfile.txt           # Create empty file, or update timestamps if it exists
touch -t 202601010000 file  # Set specific timestamp

# Create directories
mkdir mydir                 # Create one directory
mkdir -p path/to/deep/dir   # Create entire path, no error if exists
mkdir -m 750 private_dir    # Create with specific permissions

# Copy
cp source dest              # Copy file
cp -r sourcedir destdir     # Copy directory recursively
cp -p source dest           # Preserve permissions, timestamps, ownership
cp -a sourcedir destdir     # Archive copy: recursive + preserve everything
cp -u source dest           # Only copy if source is newer (update)

# Move / Rename
mv oldname newname          # Rename (same directory)
mv file /new/location/      # Move to different directory
mv -i file dest             # Interactive: ask before overwriting
mv -n file dest             # No-clobber: never overwrite

# Remove
rm file                     # Delete file (no recycle bin — permanent)
rm -i file                  # Interactive: ask before each deletion
rm -r directory             # Remove directory and all contents
rm -ri directory            # Interactive recursive remove
rmdir emptydir              # Remove only if empty (safer than rm -r)
```

**The golden rule:** `rm` is permanent. There is no undo. For operations on important data, test with `ls` first to see what matches, or use `rm -i` for confirmation. For directories, consider using `trash-cli` (`sudo apt install trash-cli`) which moves files to the trash instead of deleting immediately.

### 4.4 Finding Files

**`find` — the definitive tool:**
```bash
find /etc -name "*.conf"                    # By name pattern
find /home -name "*.log" -type f            # Files only (not dirs)
find /var -type d -name "cache"             # Directories named cache
find /home/aditya -user aditya             # Owned by user
find / -perm -4000 -type f 2>/dev/null     # SUID files (security audit)
find /var/log -mtime +30 -type f           # Older than 30 days
find /var/log -mtime -1 -type f            # Modified in last 24 hours
find /tmp -size +100M                       # Larger than 100 MB
find . -name "*.pyc" -delete               # Find and delete
find . -name "*.sh" -exec chmod +x {} \;  # Find and execute command on each
find . -name "*.log" -print0 | xargs -0 grep -l "ERROR"  # Safe with spaces
```

**`locate` — fast index-based search:**
```bash
sudo updatedb               # Update the database
locate nginx.conf           # Find by name (instant, searches index)
locate -i readme            # Case-insensitive
```
`locate` is faster than `find` but searches a database that may be hours old. Use `find` when you need real-time results or complex criteria.

**`which`, `type`, `whereis`:**
```bash
which python3               # First match in $PATH
type -a python3             # All matches: aliases, functions, builtins, files
whereis nginx               # Binary, source, and man page locations
```

### 4.5 Viewing File Contents

```bash
cat file                    # Print entire file
cat -n file                 # With line numbers
cat -A file                 # Show special characters (tabs, end-of-line)
less file                   # Paginated viewer (q to quit, /pattern to search)
more file                   # Older paginator (less is better)
head -n 20 file             # First 20 lines
tail -n 20 file             # Last 20 lines
tail -f /var/log/syslog     # Follow file as it grows (live view)
tail -F /var/log/syslog     # Follow, handle log rotation
watch -n 2 cat /proc/loadavg  # Repeat command every 2 seconds

# Binary/hex viewing
xxd file | head             # Hex dump
od -c file | head           # Octal dump with character representation
strings file | head         # Extract printable strings from binary
file mystery.bin            # Identify file type by magic bytes
```

**`less` is the most important pager.** Key bindings:
- `Space` / `b`: next/previous page
- `g` / `G`: beginning/end of file
- `/pattern`: search forward; `?pattern`: search backward
- `n` / `N`: next/previous match
- `q`: quit
- `F`: follow mode (like `tail -f`)

### 4.6 Links: Hard and Symbolic

**Hard links** are additional names for the same inode (the same data on disk):
```bash
ln original.txt hardlink.txt
ls -li original.txt hardlink.txt    # Same inode number
```
Hard link rules: both names refer to identical data; deleting one doesn't affect the other; cannot cross filesystem boundaries; cannot link to directories.

**Symbolic (soft) links** are special files that contain a path string:
```bash
ln -s /usr/share/nginx/html /var/www/html    # Create symlink
ln -s ../relative/path linkname              # Relative symlink
ls -la /var/www/html                         # Shows: html -> /usr/share/nginx/html
readlink /var/www/html                       # Print link target
readlink -f /var/www/html                    # Resolve to final real path
```
Symlink rules: can cross filesystem boundaries; can link to directories; target path is stored as text (can be broken if target moves); the link itself can be deleted without affecting the target.

### 4.7 File Permissions Deep Dive

Permissions are three sets of three bits: `rwx` for owner, group, and others.

```
-rwxr-xr--
│└┬┘└┬┘└┬┘
│ │  │  └── others: r-- = read only (4)
│ │  └───── group:  r-x = read+execute (5)
│ └──────── owner:  rwx = read+write+execute (7)
└────────── file type: - = regular file, d = directory, l = symlink
```

```bash
chmod 755 script.sh          # Owner: rwx; group: r-x; others: r-x
chmod 644 config.txt         # Owner: rw-; group: r--; others: r--
chmod 600 private_key        # Owner: rw-; group: ---; others: ---
chmod +x script.sh           # Add execute for all
chmod u+x,g-w file           # Symbolic: owner+execute, group-write
chmod -R 755 /var/www/html   # Recursive (be careful with -R)
chown user:group file        # Change owner and group
chown -R www-data:www-data /var/www/
```

**On directories, permissions mean:**
- `r`: can list contents with `ls`
- `w`: can create, delete, or rename files inside (requires `x` too)
- `x`: can enter the directory with `cd` and access files inside

**Numeric (octal) shortcuts:** 777 (full for all), 755 (standard for executables), 644 (standard for data files), 600 (private, owner only), 700 (private directory).

### 4.8 File Archiving and Compression

`tar` (tape archive) is the standard tool for bundling files. Compression is handled by separate programs (`gzip`, `bzip2`, `xz`, `zstd`).

```bash
# Create archives
tar -czf archive.tar.gz  directory/     # Create gzip-compressed archive
tar -cjf archive.tar.bz2 directory/     # bzip2 (better compression, slower)
tar -cJf archive.tar.xz  directory/     # xz (best compression, slowest)
tar -cf  archive.tar     directory/     # No compression

# Extract archives
tar -xzf archive.tar.gz                 # Extract gzip archive
tar -xf  archive.tar.gz                 # Auto-detect compression (modern tar)
tar -xf  archive.tar.gz -C /target/dir/ # Extract to specific directory

# Inspect without extracting
tar -tzf archive.tar.gz                 # List contents
tar -tzf archive.tar.gz | head

# Compression tools standalone
gzip file.txt           # Compress: creates file.txt.gz, removes original
gzip -d file.txt.gz     # Decompress (same as gunzip)
gzip -k file.txt        # Keep original when compressing
zcat file.txt.gz        # Read compressed file without decompressing

# zip (for cross-platform compatibility with Windows)
zip -r archive.zip directory/
unzip archive.zip
unzip -l archive.zip    # List contents
```

**`tar` memory aid:** `-c` create, `-x` extract, `-t` list, `-z` gzip, `-j` bzip2, `-J` xz, `-f` filename, `-v` verbose.

### 4.9 Disk Usage

```bash
df -h                          # Free space on all mounted filesystems
df -ih                         # Inode usage (can be "full" even with free blocks)
du -sh /var/log                # Total size of /var/log
du -sh /var/log/*              # Size of each item inside
du -sh /* 2>/dev/null | sort -rh | head -10   # Top 10 largest directories at root
du -sh ~/* | sort -rh | head   # Largest items in home directory
ncdu /                         # Interactive disk usage viewer (apt install ncdu)
```

### 4.10 Mounting Filesystems

A **mount point** is a directory where a filesystem is attached to the tree:

```bash
mount                          # Show all currently mounted filesystems
findmnt                        # Tree view of mounts (cleaner)
findmnt -t ext4,xfs,btrfs     # Filter by filesystem type

# Manual mounting
sudo mount /dev/sdb1 /mnt/usb          # Mount by device
sudo mount UUID=abc123... /mnt/data    # Mount by UUID (more stable)
sudo mount -t nfs server:/share /mnt   # Mount NFS share
sudo umount /mnt/usb                   # Unmount

# Persistent mounts: /etc/fstab
# Format: <device> <mountpoint> <fstype> <options> <dump> <pass>
# UUID=abc123  /data  ext4  defaults,noatime  0  2
cat /etc/fstab
```

UUIDs are preferred over device names (`/dev/sdb1`) in `/etc/fstab` because device names can change across reboots if disks are added or removed. Find the UUID with `blkid`.

---

# PART III — TEXT AND DATA

---

## Chapter 5: Text Processing — grep, awk, sed, and Friends

### 5.1 Why Text Processing Matters

Linux configuration is text. Logs are text. Command output is text. The shell itself passes text between programs via pipes. Mastering the core text-processing tools means you can query, transform, and analyze any data the system can produce — without writing a full program.

The tools in this chapter form a pipeline vocabulary. Each one does one thing well; chained together, they solve arbitrarily complex data problems.

### 5.2 grep — Searching Text

`grep` (global regular expression print) searches input for lines matching a pattern and prints matching lines.

```bash
grep "error" /var/log/syslog              # Lines containing "error"
grep -i "error" /var/log/syslog           # Case-insensitive
grep -n "error" file.txt                  # With line numbers
grep -c "error" file.txt                  # Count of matching lines
grep -v "debug" /var/log/syslog           # Lines NOT matching (invert)
grep -l "TODO" src/*.py                   # List files that match
grep -r "password" /etc/                  # Recursive search
grep -r "password" /etc/ --include="*.conf"  # Limit to .conf files
grep -w "root" /etc/passwd                # Whole-word match only
grep -A 3 "FAILED" auth.log              # 3 lines After match
grep -B 2 "FAILED" auth.log              # 2 lines Before match
grep -C 2 "FAILED" auth.log              # 2 lines Context (before + after)
```

**Extended regex with `-E` (or `egrep`):**
```bash
grep -E "error|warning|critical" syslog  # OR pattern
grep -E "^[0-9]{4}-" logfile             # Lines starting with 4 digits + dash
grep -E "[0-9]{1,3}\.[0-9]{1,3}\.[0-9]{1,3}\.[0-9]{1,3}" access.log  # IP addresses
```

**Fixed-string matching with `-F` (faster, no regex):**
```bash
grep -F "192.168.1.1" access.log         # Literal string, no regex interpretation
```

**Practical recipes:**
```bash
# Count failed SSH logins by IP
grep "Failed password" /var/log/auth.log | grep -oE "[0-9]+\.[0-9]+\.[0-9]+\.[0-9]+" | sort | uniq -c | sort -rn

# Find all files containing a string, recursively
grep -rl "TODO" /home/aditya/projects/

# Find config files that set a specific option
grep -rn "PermitRootLogin yes" /etc/ssh/
```

### 5.3 sed — Stream Editor

`sed` reads input line by line and applies editing commands. The most common command is substitution.

**Substitution:**
```bash
sed 's/old/new/' file           # Replace first occurrence per line
sed 's/old/new/g' file          # Replace all occurrences (global)
sed 's/old/new/gi' file         # Global, case-insensitive
sed 's/old/new/2' file          # Replace only the 2nd occurrence
sed -i 's/old/new/g' file       # Edit file in-place (modifies file directly)
sed -i.bak 's/old/new/g' file   # In-place, keep backup as file.bak
```

**Address ranges:**
```bash
sed -n '10,20p' file            # Print only lines 10-20 (-n suppresses default output)
sed '10,20d' file               # Delete lines 10-20
sed '/pattern/d' file           # Delete lines matching pattern
sed '/^#/d' file                # Delete comment lines (starting with #)
sed '/^$/d' file                # Delete blank lines
sed -n '/START/,/END/p' file    # Print lines between START and END markers
```

**Inserting and appending:**
```bash
sed '5a\This line is inserted after line 5' file
sed '5i\This line is inserted before line 5' file
sed '/pattern/a\New line after each match' file
```

**Practical recipes:**
```bash
# Remove all comment lines and blank lines from a config file
sed -e '/^#/d' -e '/^$/d' /etc/ssh/sshd_config

# Replace a value in a config file in-place
sed -i 's/^#PermitRootLogin yes/PermitRootLogin no/' /etc/ssh/sshd_config

# Extract lines 100-200 from a large log file (faster than head+tail)
sed -n '100,200p' /var/log/huge.log

# Add a line at the top of a file
sed -i '1i\# Auto-generated — do not edit manually' config.conf
```

### 5.4 awk — Pattern Scanning and Processing

`awk` processes text field-by-field. It splits each line into fields (default separator: whitespace) and lets you process, filter, and compute on them.

**Field references:** `$1` is the first field, `$2` the second, ..., `$NF` is the last field. `$0` is the entire line. `NR` is the current line number.

```bash
awk '{print $1}' file           # Print first field of each line
awk '{print $1, $3}' file       # Print fields 1 and 3
awk '{print NR, $0}' file       # Print with line numbers
awk 'NR==5' file                # Print only line 5
awk 'NR>=10 && NR<=20' file     # Print lines 10-20
awk '/pattern/' file            # Print lines matching pattern (like grep)
awk '!/pattern/' file           # Print lines NOT matching
awk -F: '{print $1}' /etc/passwd  # Use colon as field separator
awk -F, '{print $2}' data.csv   # CSV: print second column
```

**Computing and aggregating:**
```bash
# Sum the file sizes from ls -l output
ls -l | awk '{sum += $5} END {print "Total:", sum, "bytes"}'

# Count occurrences of each value in column 1
awk '{count[$1]++} END {for (k in count) print count[k], k}' file | sort -rn

# Print lines where field 3 is greater than 100
awk '$3 > 100' data.txt

# Average of column 2
awk '{sum+=$2; n++} END {print "Average:", sum/n}' data.txt
```

**BEGIN and END blocks:**
```bash
awk 'BEGIN {print "Starting..."} {print $1} END {print "Done. Lines:", NR}' file
```

**Practical recipes:**
```bash
# Show disk usage percentage for each filesystem
df -h | awk 'NR>1 {print $5, $6}' | sort -rn

# Extract usernames from /etc/passwd
awk -F: '$3 >= 1000 {print $1}' /etc/passwd    # Regular users (UID ≥ 1000)

# Show processes consuming more than 1% CPU
ps aux | awk '$3 > 1.0 {print $1, $2, $3, $11}'

# Parse Apache access log: count requests per IP
awk '{print $1}' /var/log/apache2/access.log | sort | uniq -c | sort -rn | head -20
```

### 5.5 cut, sort, uniq, tr, wc

These tools handle common transformations in pipelines:

**`cut` — extract columns:**
```bash
cut -d: -f1 /etc/passwd          # First colon-delimited field
cut -d, -f2,4 data.csv           # Fields 2 and 4 from CSV
cut -c1-10 file                  # Characters 1-10 of each line
```

**`sort` — sort lines:**
```bash
sort file                        # Alphabetical sort
sort -r file                     # Reverse order
sort -n file                     # Numeric sort (10 comes after 9, not before)
sort -rn file                    # Reverse numeric
sort -k2 file                    # Sort by second field
sort -k2,2n file                 # Sort by second field numerically
sort -t: -k3,3n /etc/passwd      # Sort passwd by UID (field 3, colon-delimited)
sort -u file                     # Sort and remove duplicates
```

**`uniq` — remove or count duplicates (input must be sorted):**
```bash
sort file | uniq                 # Remove consecutive duplicate lines
sort file | uniq -c              # Count occurrences of each unique line
sort file | uniq -d              # Print only lines that appear more than once
sort file | uniq -u              # Print only lines that appear exactly once
```

**`tr` — translate or delete characters:**
```bash
echo "Hello World" | tr 'a-z' 'A-Z'    # Uppercase
echo "Hello World" | tr 'A-Z' 'a-z'    # Lowercase
echo "a:b:c" | tr ':' '\n'             # Replace colons with newlines
echo "hello   world" | tr -s ' '       # Squeeze multiple spaces to one
echo "hello" | tr -d 'aeiou'           # Delete vowels
```

**`wc` — word/line/byte count:**
```bash
wc -l file                       # Line count
wc -w file                       # Word count
wc -c file                       # Byte count
wc -m file                       # Character count (handles multi-byte)
ls /etc/*.conf | wc -l           # Count config files
```

### 5.6 head, tail, and less for Large Files

```bash
head -n 50 bigfile.log           # First 50 lines
tail -n 50 bigfile.log           # Last 50 lines
tail -f /var/log/syslog          # Follow in real time (Ctrl+C to stop)
tail -F /var/log/app.log         # Follow, survives log rotation

# Get lines 500-600 from a large file efficiently
sed -n '500,600p' bigfile.log

# Count total lines without loading the file into memory
wc -l < bigfile.log
```

### 5.7 Putting It Together: Pipeline Patterns

The real power comes from chaining these tools:

```bash
# Top 10 most common HTTP status codes in an access log
awk '{print $9}' /var/log/nginx/access.log \
  | sort \
  | uniq -c \
  | sort -rn \
  | head -10

# Find the 5 largest directories under /var
du -sh /var/* 2>/dev/null | sort -rh | head -5

# List all unique users who have logged in via SSH today
grep "Accepted" /var/log/auth.log \
  | grep "$(date +%b\ %e)" \
  | awk '{print $9}' \
  | sort -u

# Show config file with all comments and blanks stripped
grep -v '^\s*#' /etc/nginx/nginx.conf | grep -v '^\s*$'

# Check which ports are listening and what process owns them
ss -tlnp | awk 'NR>1 {print $4, $6}' | column -t
```

---

## Chapter 6: Users, Groups, and Permissions

### 6.1 The Identity Model

Linux is a multi-user operating system. Every process runs as a specific user, and every file is owned by a user and a group. The kernel enforces access control based on these identities — there is no way to bypass it from user space.

Every user has:
- **UID (User ID):** A number. The kernel works with UIDs, not names. `root` is always UID 0.
- **GID (Primary Group ID):** Every user belongs to one primary group.
- **Supplementary groups:** Users can belong to many additional groups.
- **Home directory:** Where personal files live.
- **Login shell:** The program started on login.

All of this is stored in `/etc/passwd` (user info) and `/etc/shadow` (hashed passwords). Groups are in `/etc/group`.

```bash
id                               # Your UID, GID, and all group memberships
id username                      # Another user's identity
whoami                           # Just the username
groups                           # Just your group memberships
```

### 6.2 /etc/passwd, /etc/shadow, /etc/group

**`/etc/passwd`** — world-readable, one line per user:
```
username:x:UID:GID:comment:home:shell
root:x:0:0:root:/root:/bin/bash
aditya:x:1000:1000:Aditya,,,:/home/aditya:/bin/bash
www-data:x:33:33:www-data:/var/www:/usr/sbin/nologin
```
The `x` in field 2 means the password is in `/etc/shadow`. A shell of `/usr/sbin/nologin` or `/bin/false` prevents interactive login (used for service accounts).

**`/etc/shadow`** — readable only by root:
```
username:hashed_password:last_change:min_age:max_age:warn:inactive:expire
```
Password hashes use formats like `$6$salt$hash` (SHA-512). An `!` or `*` instead of a hash means the account is locked.

**`/etc/group`** — group definitions:
```
groupname:x:GID:member1,member2,member3
sudo:x:27:aditya
docker:x:998:aditya
```

### 6.3 Managing Users

```bash
# Create a user
sudo useradd -m -s /bin/bash -c "Full Name" username
# -m: create home directory
# -s: login shell
# -c: comment (GECOS field, usually full name)
# -G: supplementary groups: -G docker,sudo

sudo passwd username             # Set password interactively

# Modify a user
sudo usermod -aG docker aditya   # Add to group (CRITICAL: -a means append, without -a it replaces)
sudo usermod -s /bin/zsh aditya  # Change shell
sudo usermod -d /new/home aditya # Change home directory
sudo usermod -L aditya           # Lock account
sudo usermod -U aditya           # Unlock account

# Delete a user
sudo userdel aditya              # Delete user (keeps home directory)
sudo userdel -r aditya           # Delete user AND home directory and mail spool

# View user info
getent passwd aditya             # User record (works with LDAP too, unlike cat /etc/passwd)
finger aditya                    # Detailed user info (if installed)
last aditya                      # Login history
lastlog                          # Last login for all users
```

### 6.4 Managing Groups

```bash
sudo groupadd developers         # Create group
sudo groupdel developers         # Delete group
sudo groupmod -n newname oldname # Rename group
sudo gpasswd -a aditya developers   # Add user to group
sudo gpasswd -d aditya developers   # Remove user from group
getent group developers             # View group members

# After adding yourself to a group, you need to re-login
# OR use newgrp to switch primary group in current session:
newgrp docker
```

### 6.5 sudo — Controlled Privilege Escalation

`sudo` (substitute user do) allows authorized users to run commands as another user (usually root) without knowing root's password. It logs every command it executes.

```bash
sudo command                     # Run as root
sudo -u www-data command         # Run as www-data
sudo -i                          # Start a root shell (login environment)
sudo -s                          # Start a root shell (current environment)
sudo !!                          # Re-run last command as root
sudo -l                          # List what you're allowed to do
sudo -l -U username              # List another user's sudo permissions
```

**`/etc/sudoers`** — configure sudo permissions. Always edit with `visudo` (which validates syntax and prevents lockouts):

```bash
sudo visudo
```

Common sudoers patterns:
```
# Give user full sudo access
aditya  ALL=(ALL:ALL) ALL

# Give user sudo without password (convenient but less secure)
aditya  ALL=(ALL:ALL) NOPASSWD: ALL

# Allow user to run specific commands only
deploy  ALL=(root) NOPASSWD: /bin/systemctl restart nginx, /usr/sbin/nginx -t

# Grant a group sudo access
%developers  ALL=(ALL:ALL) ALL

# Include drop-in files from /etc/sudoers.d/ (standard Ubuntu setup)
#includedir /etc/sudoers.d
```

**Drop-in files:** Modern practice is to put per-user or per-role configuration in `/etc/sudoers.d/` rather than editing the main file. This keeps changes modular and makes it easy to audit:
```bash
echo "aditya ALL=(ALL:ALL) ALL" | sudo tee /etc/sudoers.d/aditya
sudo chmod 440 /etc/sudoers.d/aditya
```

**Security principle:** Grant the minimum sudo access needed. Full `NOPASSWD: ALL` should only be used in specific automation contexts (CI/CD, configuration management agents), and even then, prefer scoping it to specific commands.

### 6.6 Switching Users

```bash
su username                      # Switch to username (requires their password)
su -                             # Switch to root (requires root's password)
su - username                    # Full login switch (loads their environment)
```

On Ubuntu, the root account has no password by default — you cannot `su -` to root directly. Use `sudo -i` or `sudo su -` instead. To set a root password (generally not recommended): `sudo passwd root`.

### 6.7 File Ownership and Permissions in Depth

Every file has exactly one owner (UID) and one group (GID). Permissions are checked in order:
1. If the process UID matches the file owner UID, apply owner permissions.
2. Else if the process GID (or any supplementary GID) matches the file GID, apply group permissions.
3. Else apply other permissions.

This means if you are the owner, only owner permissions apply — not group permissions. If you are the owner but the owner permission is `---`, you cannot read the file even if group is `rwx`.

**Special permission bits:**

*SUID (Set User ID, bit 4000):* When set on an executable, the program runs as the file's owner rather than the user executing it. This is how `passwd` (owned by root) can modify `/etc/shadow` even when run by a regular user.
```bash
ls -la /usr/bin/passwd
# -rwsr-xr-x 1 root root ... /usr/bin/passwd
#    ↑ 's' in owner execute position = SUID
sudo chmod u+s /opt/myapp/binary    # Set SUID
sudo chmod 4755 /opt/myapp/binary   # Same via octal
```

*SGID (Set Group ID, bit 2000):* On executables: runs as the file's group. On directories: new files created inside inherit the directory's group (instead of the creator's primary group). Useful for shared project directories.
```bash
sudo chmod g+s /shared/project/     # New files get the directory's group
sudo chmod 2775 /shared/project/
```

*Sticky bit (bit 1000):* On directories: users can only delete or rename files they own, even if they have write permission on the directory. This is set on `/tmp` so users cannot delete each other's temporary files.
```bash
ls -la / | grep tmp
# drwxrwxrwt ... tmp    ← 't' in others execute = sticky bit
sudo chmod +t /shared/tmp/
```

### 6.8 Access Control Lists (ACLs)

Standard Unix permissions (owner/group/others) cannot express "give user alice read access to this file without changing the owner or group." ACLs solve this.

```bash
# View ACL
getfacl file.txt

# Set ACL entries
setfacl -m u:alice:r file.txt         # Give alice read access
setfacl -m g:developers:rw file.txt   # Give developers group read+write
setfacl -m o::- file.txt             # Remove others' permissions
setfacl -x u:alice file.txt          # Remove alice's ACL entry
setfacl -b file.txt                  # Remove all ACL entries

# Default ACLs (inherited by new files in directory)
setfacl -d -m g:developers:rw /shared/    # All new files in /shared/ get this ACL
setfacl -R -m u:alice:r /project/         # Recursive: apply to existing files
```

ACL support must be enabled on the filesystem. Most modern ext4, XFS, and Btrfs mounts have it enabled by default. Check with `tune2fs -l /dev/sda1 | grep "Default mount"`.

### 6.9 umask — Default Permission Control

When a new file is created, its permissions are `0666 & ~umask` (files) or `0777 & ~umask` (directories). The default umask is `0022`, so new files get `0644` and new directories get `0755`.

```bash
umask                            # View current umask
umask 027                        # Set umask (group can't write; others get nothing)
umask 077                        # Private: only owner can access anything

# For a specific script or session:
(umask 077; touch sensitive.key)  # File created with 600 permissions
```

### 6.10 Auditing and Monitoring Access

```bash
# Who is currently logged in
who                              # Current logins
w                                # Logged-in users with what they're doing
last                             # Login history from /var/log/wtmp
last -n 20                       # Last 20 logins
lastb                            # Failed login attempts (requires root)
lastlog                          # Last login for every user

# File access monitoring with auditd
sudo apt install auditd
sudo auditctl -w /etc/passwd -p rwxa -k passwd_changes  # Watch file
sudo ausearch -k passwd_changes                          # Query audit log
sudo aureport --login                                    # Login summary report
```

---

# PART IV — SYSTEM CONTROL

---

## Chapter 7: Processes, Signals, and Job Control

### 7.1 The Process Model

Every running program is a process. A process is an instance of a program in execution — it has its own memory space, file descriptor table, credentials, and scheduling state. The kernel tracks every process with a data structure called the *process descriptor*.

Key attributes every process carries:
- **PID** (Process ID): a unique number identifying the process while it runs.
- **PPID** (Parent PID): every process except PID 1 was created by another process.
- **UID/EUID**: real user ID (who launched it) and effective user ID (who it runs as — differs for SUID programs).
- **Working directory**: inherited from parent; changed with `chdir()` / `cd`.
- **File descriptor table**: open files and sockets. `stdin` (fd 0), `stdout` (fd 1), `stderr` (fd 2) are inherited.
- **Environment**: a copy of the parent's environment at fork time.

### 7.2 How Processes Are Created

Processes are created exclusively via the `fork()` system call. `fork()` creates an exact copy of the calling process (the parent). The copy (the child) then typically calls `exec()` to replace its memory image with a new program. This *fork-exec* pattern is how every command you type in the shell becomes a running process.

```
Shell process (PID 1234)
    │
    ├─ fork() ──► Child process (PID 1235) — exact copy of shell
    │               │
    │               └─ exec("ls", args) ──► Child is now "ls", runs, exits
    │
    └─ wait() ──── Shell waits for child to exit, then prints next prompt
```

When a process exits, it becomes a *zombie* — its memory is freed but its entry in the process table is kept until the parent calls `wait()` to collect the exit status. A zombie that accumulates because a parent never calls `wait()` is a resource leak. If a parent exits before its children, the children are *reparented* to PID 1 (systemd), which reaps them.

### 7.3 Viewing Processes

```bash
ps                               # Processes in current terminal session
ps aux                           # All processes, BSD format (most common)
ps -ef                           # All processes, UNIX format
ps -eo pid,ppid,user,%cpu,%mem,stat,cmd   # Custom columns
ps aux --sort=-%cpu | head -15   # Top 15 by CPU usage
ps aux --sort=-%mem | head -15   # Top 15 by memory usage

pgrep nginx                      # PIDs of processes named nginx
pgrep -u www-data                # PIDs owned by www-data
pgrep -la nginx                  # PIDs + full command line
pstree                           # Process tree
pstree -p                        # Tree with PIDs
pstree -u                        # Tree with usernames
```

**Interactive monitoring:**
```bash
top                              # Classic interactive monitor (q to quit)
htop                             # Enhanced top — mouse support, colors, tree view
                                 # Install: sudo apt install htop
```

Inside `top`: press `M` to sort by memory, `P` to sort by CPU, `k` to kill a process, `r` to renice, `1` to show individual CPUs, `q` to quit.

**Understanding `ps aux` columns:**
```
USER   PID  %CPU  %MEM   VSZ   RSS  TTY   STAT  START   TIME  COMMAND
root     1   0.0   0.1  168MB   9MB  ?     Ss    10:00   0:02  /sbin/init
aditya 1234  2.1   0.5  450MB  40MB  pts/0  S+   10:05   0:15  python3 app.py
```
- **VSZ**: Virtual memory size (total memory map, including not-yet-used)
- **RSS**: Resident Set Size — actual physical memory in use now
- **STAT**: Process state. `S` = sleeping, `R` = running, `Z` = zombie, `T` = stopped, `D` = uninterruptible sleep (usually I/O), `+` = foreground, `s` = session leader

### 7.4 Signals

Signals are asynchronous notifications sent to processes. They are the kernel's mechanism for telling a process that something happened.

| Signal | Number | Default | Use |
|--------|--------|---------|-----|
| SIGHUP | 1 | Terminate | Reload config (by convention for daemons) |
| SIGINT | 2 | Terminate | Ctrl+C — interrupt |
| SIGQUIT | 3 | Core dump | Ctrl+\ — quit with core dump |
| SIGKILL | 9 | **Kill (uncatchable)** | Last-resort termination |
| SIGTERM | 15 | Terminate | Polite shutdown request |
| SIGSTOP | 19 | **Stop (uncatchable)** | Pause process |
| SIGCONT | 18 | Continue | Resume stopped process |
| SIGUSR1 | 10 | Terminate | Application-defined |
| SIGUSR2 | 12 | Terminate | Application-defined |
| SIGCHLD | 17 | Ignore | Child process state changed |
| SIGPIPE | 13 | Terminate | Write to broken pipe |

```bash
kill PID                         # Send SIGTERM (default)
kill -TERM PID                   # Same, explicit
kill -9 PID                      # Send SIGKILL — immediate, no cleanup
kill -HUP PID                    # Send SIGHUP — typically reloads config
kill -0 PID                      # Test if process exists (no signal sent)

killall nginx                    # Kill all processes named nginx (SIGTERM)
killall -9 nginx                 # Kill with SIGKILL
pkill nginx                      # Like killall but uses regex
pkill -u aditya                  # Kill all processes owned by aditya
pkill -HUP sshd                  # Reload sshd config

# Graceful shutdown pattern: SIGTERM first, then SIGKILL if needed
kill -TERM "$pid"
sleep 5
kill -0 "$pid" 2>/dev/null && kill -KILL "$pid"
```

**Why SIGKILL is a last resort:** SIGKILL cannot be caught or ignored by the process — the kernel enforces it. This means the process has no chance to clean up (flush buffers, release locks, close connections). Always try SIGTERM first and give the process a few seconds to exit gracefully.

### 7.5 Process States and /proc

Every process's state is visible in `/proc/<PID>/`:
```bash
cat /proc/1234/status            # Human-readable status
cat /proc/1234/cmdline           # Command line arguments (null-separated)
cat /proc/1234/environ           # Environment variables
ls -la /proc/1234/fd/            # Open file descriptors
cat /proc/1234/maps              # Memory map
```

The `/proc` filesystem is virtual — it is generated by the kernel on demand. No files are stored on disk. Reading from `/proc` queries live kernel state.

```bash
cat /proc/loadavg                # 1, 5, 15 minute load averages
cat /proc/meminfo                # Detailed memory statistics
cat /proc/cpuinfo                # CPU details
cat /proc/version                # Kernel version string
cat /proc/uptime                 # Seconds since boot
```

### 7.6 Background Jobs and Job Control

The shell has a concept of *jobs* — commands it is managing. A job can be in the foreground (receiving keyboard input) or background (running independently).

```bash
command &                        # Start in background immediately
jobs                             # List current jobs with job numbers
jobs -l                          # Include PIDs
fg                               # Bring most recent background job to foreground
fg %2                            # Bring job number 2 to foreground
bg                               # Resume most recent stopped job in background
bg %2                            # Resume job 2 in background
Ctrl+Z                           # Suspend current foreground job (SIGSTOP)
Ctrl+C                           # Interrupt (kill) current foreground job (SIGINT)
```

**Job survival after logout:**
```bash
nohup command &                  # Immune to SIGHUP; output goes to nohup.out
nohup command > mylog.txt 2>&1 & # Explicit output file

disown %1                        # Detach job from shell after the fact
disown -h %1                     # Mark as immune to SIGHUP but keep in job table
```

**`tmux` and `screen`** are a better long-term solution — they create persistent terminal sessions that survive disconnection:
```bash
tmux new -s mysession            # Create named session
tmux attach -t mysession         # Reattach to existing session
tmux ls                          # List sessions
# Inside tmux:
# Ctrl+B then D: detach (leave running)
# Ctrl+B then C: new window
# Ctrl+B then %: split vertically
# Ctrl+B then ": split horizontally
```

### 7.7 Process Priority: nice and renice

The kernel scheduler assigns CPU time to processes based on their *niceness* value, ranging from -20 (highest priority) to +19 (lowest priority). Normal priority is 0.

```bash
nice -n 10 command               # Start with niceness +10 (lower priority)
nice -n -5 command               # Start with niceness -5 (higher, requires root for negative)
renice 10 -p 1234                # Change running process priority
renice 10 -u aditya              # Lower priority of all aditya's processes
```

Regular users can only increase niceness (lower priority). Only root can decrease niceness (increase priority) or set negative values.

### 7.8 Resource Limits: ulimit

`ulimit` controls the resources a shell and its children can use:

```bash
ulimit -a                        # Show all limits
ulimit -n 65536                  # Increase max open file descriptors
ulimit -u 1024                   # Max user processes
ulimit -v 1048576                # Max virtual memory in KB (1 GB)
ulimit -s unlimited              # Unlimited stack size
```

Permanent limits are set in `/etc/security/limits.conf` or files in `/etc/security/limits.d/`:
```
# /etc/security/limits.d/90-nofile.conf
* soft nofile 65536
* hard nofile 65536
www-data soft nproc 1024
```

For systemd services, use `LimitNOFILE=` in the unit file (see Chapter 11).

---

## Chapter 8: Storage, Partitions, and Filesystems

### 8.1 The Storage Stack

Understanding what happens between a write in your program and data reaching persistent storage:

```
Application
    │ write("data")
    ▼
VFS (Virtual Filesystem Switch) — uniform API across all filesystem types
    │
    ▼
Filesystem (ext4, Btrfs, XFS…) — translates to blocks, manages metadata
    │
    ▼
Block layer — I/O scheduling, caching (page cache)
    │
    ▼
Device driver (SCSI, NVMe, virtio…)
    │
    ▼
Physical medium (SSD, HDD, NVMe, network block device)
```

### 8.2 Block Devices and Naming

Block devices appear as files in `/dev/`:
```
/dev/sda          — first SCSI/SATA disk
/dev/sda1         — first partition of /dev/sda
/dev/sda2         — second partition of /dev/sda
/dev/sdb          — second SCSI/SATA disk
/dev/nvme0n1      — first NVMe drive
/dev/nvme0n1p1    — first partition of the NVMe drive
/dev/vda          — virtual disk (in a KVM VM)
/dev/xvda         — Xen virtual disk (older AWS EC2 instances)
```

**Important:** Device names are *not stable* across reboots — a USB drive that was `/dev/sdb` might become `/dev/sdc` next boot. Use UUIDs or labels for persistent references.

```bash
lsblk                            # Block device tree with mount points
lsblk -f                         # Include filesystem type and UUID
blkid                            # UUID, labels, filesystem types for all devices
blkid /dev/sda1                  # Just for one device
```

### 8.3 Partitioning

Modern systems use GPT (GUID Partition Table) rather than the old MBR (Master Boot Record). GPT supports disks larger than 2 TB and up to 128 partitions. UEFI systems require GPT.

**`fdisk`** — interactive partitioning tool (use for MBR and GPT):
```bash
sudo fdisk /dev/sdb              # Open disk interactively
# Inside fdisk:
# p — print current partition table
# n — new partition
# d — delete partition
# t — change partition type
# w — write changes to disk (DESTRUCTIVE — no undo)
# q — quit without saving
```

**`gdisk`** — GPT-specific (cleaner for modern systems):
```bash
sudo gdisk /dev/sdb
```

**`parted`** — scriptable partitioner:
```bash
sudo parted /dev/sdb print                          # View partition table
sudo parted /dev/sdb mklabel gpt                    # Create GPT label
sudo parted /dev/sdb mkpart primary ext4 1MiB 50GiB # Create partition
```

**Non-interactive partitioning** (for scripts):
```bash
sudo parted -s /dev/sdb mklabel gpt
sudo parted -s /dev/sdb mkpart primary ext4 1MiB 100%
```

### 8.4 Creating Filesystems

After partitioning, format each partition with a filesystem:

```bash
sudo mkfs.ext4 /dev/sdb1             # ext4 — safe general-purpose default
sudo mkfs.ext4 -L "mydrive" /dev/sdb1  # With label
sudo mkfs.xfs /dev/sdb1              # XFS — high performance, RHEL default
sudo mkfs.btrfs /dev/sdb1            # Btrfs — copy-on-write, snapshots
sudo mkfs.fat -F32 /dev/sdb1         # FAT32 — for EFI partitions or USB drives
sudo mkswap /dev/sdb2                # Swap partition
```

### 8.5 Mounting and /etc/fstab

**Manual mounting:**
```bash
sudo mount /dev/sdb1 /mnt/data           # Mount by device
sudo mount -t ext4 /dev/sdb1 /mnt/data  # Explicit filesystem type
sudo mount UUID=abc-123 /mnt/data        # Mount by UUID
sudo mount -o ro /dev/sdb1 /mnt/data    # Mount read-only
sudo mount -o remount,rw /mnt/data      # Remount with different options
sudo umount /mnt/data                    # Unmount
sudo umount -l /mnt/data                 # Lazy unmount (waits for last access)
```

**Persistent mounts in `/etc/fstab`:**
```
# <device>                          <mount>    <fs>    <options>        <dump> <pass>
UUID=3e6be9de-...                   /          ext4    defaults,noatime    0      1
UUID=a7c4f9b2-...                   /home      ext4    defaults,noatime    0      2
UUID=9f3b2d1e-...                   none       swap    sw                  0      0
//192.168.1.5/share  /mnt/nas  cifs  credentials=/etc/samba/creds,uid=1000  0  0
```

Field meanings:
- **dump**: 0 = don't include in dump backups (almost always 0)
- **pass**: 0 = skip fsck; 1 = check first (root only); 2 = check after root

After editing `/etc/fstab`:
```bash
sudo mount -a              # Mount all entries in fstab (test without rebooting)
sudo systemd-analyze verify  # Verify fstab syntax via systemd
```

### 8.6 ext4 Management

```bash
# File system information
sudo tune2fs -l /dev/sda1              # Detailed ext4 metadata
sudo dumpe2fs -h /dev/sda1             # Superblock information

# Resize (ext4 can be grown online; shrinking requires unmounting)
sudo resize2fs /dev/sda1               # Grow to fill partition
sudo resize2fs /dev/sda1 50G           # Resize to 50G

# Check and repair (must be unmounted or read-only)
sudo fsck.ext4 -n /dev/sdb1            # Dry-run check (safe)
sudo fsck.ext4 /dev/sdb1               # Check and repair
sudo e2fsck -f -y /dev/sdb1            # Force check, answer yes to all

# Reserved blocks (ext4 reserves 5% for root by default)
sudo tune2fs -m 1 /dev/sdb1            # Reduce reserved to 1% (good for data drives)
```

### 8.7 LVM — Logical Volume Manager

LVM sits between physical disks and filesystems, adding a flexible abstraction layer. It lets you resize volumes without repartitioning, span volumes across disks, and take snapshots.

**Concepts:**
- **PV (Physical Volume):** a disk or partition initialized for LVM (`/dev/sdb`)
- **VG (Volume Group):** a pool of storage made from one or more PVs
- **LV (Logical Volume):** a virtual partition carved from a VG, formatted and mounted normally

```bash
# Set up LVM
sudo pvcreate /dev/sdb /dev/sdc           # Initialize PVs
sudo vgcreate mydata /dev/sdb /dev/sdc    # Create VG named "mydata"
sudo lvcreate -L 100G -n webdata mydata   # Create 100G LV named "webdata"
sudo mkfs.ext4 /dev/mydata/webdata        # Format it
sudo mount /dev/mydata/webdata /var/www   # Mount it

# Inspect
sudo pvs                                  # PV summary
sudo vgs                                  # VG summary
sudo lvs                                  # LV summary
sudo pvdisplay; sudo vgdisplay; sudo lvdisplay  # Detailed info

# Resize LV (extend online with ext4 or XFS)
sudo lvextend -L +50G /dev/mydata/webdata      # Extend by 50G
sudo resize2fs /dev/mydata/webdata             # Grow filesystem to fill (ext4)
sudo xfs_growfs /var/www                       # For XFS filesystems

# Shrink (must unmount, shrink filesystem first, then LV)
sudo umount /var/www
sudo e2fsck -f /dev/mydata/webdata
sudo resize2fs /dev/mydata/webdata 80G         # Shrink filesystem to 80G
sudo lvreduce -L 80G /dev/mydata/webdata       # Shrink LV to match
```

### 8.8 Swap

Swap is disk space used as overflow for physical RAM and for hibernation. Modern systems with ample RAM use swap rarely, but it prevents out-of-memory (OOM) kills when memory is tight.

```bash
swapon --show                    # View swap devices and usage
free -h                          # RAM and swap usage

# Create a swap file (useful when no swap partition exists)
sudo fallocate -l 4G /swapfile   # Allocate 4 GB file
sudo chmod 600 /swapfile         # Security: only root can read
sudo mkswap /swapfile            # Format as swap
sudo swapon /swapfile            # Enable
# Add to /etc/fstab for persistence:
echo '/swapfile none swap sw 0 0' | sudo tee -a /etc/fstab

# Tune swappiness: 0 = use swap only when absolutely necessary, 100 = aggressive
cat /proc/sys/vm/swappiness      # Current value (default: 60)
sudo sysctl vm.swappiness=10     # Reduce for desktop/interactive systems
# Make permanent:
echo 'vm.swappiness=10' | sudo tee /etc/sysctl.d/99-swappiness.conf
```

### 8.9 Disk Health with SMART

SMART (Self-Monitoring, Analysis, and Reporting Technology) is built into most modern HDDs and SSDs. Check it before trusting a disk for important data.

```bash
sudo apt install smartmontools
sudo smartctl -a /dev/sda        # Full SMART report
sudo smartctl -H /dev/sda        # Health summary (PASSED/FAILED)
sudo smartctl -t short /dev/sda  # Run short self-test
sudo smartctl -t long /dev/sda   # Run long self-test (takes hours)
sudo smartctl -l selftest /dev/sda   # View self-test results
```

Key SMART attributes to watch: **Reallocated_Sector_Ct** (bad sectors), **Current_Pending_Sector** (sectors waiting to be remapped), **Offline_Uncorrectable** (unrecoverable errors). Non-zero values on any of these are a warning sign.

### 8.10 Btrfs Snapshots (Modern Practice)

Btrfs (B-tree filesystem) supports copy-on-write snapshots that are nearly instant and space-efficient. Ubuntu Desktop 24.04 uses Btrfs by default.

```bash
# List subvolumes (Btrfs organizes data into subvolumes)
sudo btrfs subvolume list /

# Create a snapshot (instant — only metadata is written initially)
sudo btrfs subvolume snapshot / /snapshots/root-before-upgrade

# Read-only snapshot (safe backup point)
sudo btrfs subvolume snapshot -r / /snapshots/root-$(date +%Y%m%d)

# Delete old snapshot
sudo btrfs subvolume delete /snapshots/root-20250101

# Btrfs balance (rebalance data after adding drives)
sudo btrfs balance start /

# Check filesystem
sudo btrfs check /dev/sda2
```

**Timeshift** is the recommended GUI/CLI tool for managing Btrfs snapshots on desktop Ubuntu as a system rollback mechanism:
```bash
sudo apt install timeshift
sudo timeshift --create --comments "Before upgrade"
sudo timeshift --restore   # Interactive restore from snapshot
```

---

# PART V — SYSTEM ADMINISTRATION

---

## Chapter 9: Software Management — apt, snap, and Flatpak

### 9.1 How Package Management Works

A **package** is a compressed archive containing compiled binaries, configuration files, documentation, and metadata (name, version, dependencies, maintainer). The **package manager** handles downloading, installing, upgrading, and removing packages — crucially, it also resolves *dependencies* (the other packages a program needs to function).

On Debian and Ubuntu, packages come in the `.deb` format and are managed by `dpkg` (the low-level tool) and `apt` (the high-level tool that handles repositories and dependencies). On Red Hat/Fedora/RHEL/AlmaLinux, packages are `.rpm` files managed by `rpm` and `dnf` (formerly `yum`).

**Repositories** are collections of packages served over HTTP. Your system has a list of configured repositories in `/etc/apt/sources.list` and `/etc/apt/sources.list.d/`.

### 9.2 apt — The Debian/Ubuntu Package Manager

```bash
# Update the package index (always run before installing)
sudo apt update

# Install packages
sudo apt install nginx
sudo apt install -y nginx               # Non-interactive (say yes automatically)
sudo apt install nginx=1.24.0-1ubuntu3  # Install specific version

# Remove packages
sudo apt remove nginx           # Remove package, keep config files
sudo apt purge nginx            # Remove package AND config files
sudo apt autoremove             # Remove packages no longer needed by anything

# Upgrade
sudo apt upgrade                # Upgrade all packages (conservative — won't remove)
sudo apt full-upgrade           # Upgrade + handle dependency changes (may remove)
sudo apt upgrade nginx          # Upgrade one package

# Search
apt search nginx                # Search by name/description
apt show nginx                  # Show detailed package information
apt list --installed            # List all installed packages
apt list --installed | grep nginx
apt list --upgradable           # List packages with available upgrades

# Inspect installed packages
dpkg -l                         # List all installed packages (raw)
dpkg -l | grep "^ii" | wc -l   # Count installed packages
dpkg -L nginx                   # Files installed by a package
dpkg -S /usr/sbin/nginx         # Which package owns this file
dpkg -s nginx                   # Package status and info
```

**Non-interactive installs** (for scripts and automation):
```bash
export DEBIAN_FRONTEND=noninteractive
sudo apt install -y tzdata postfix   # Suppresses configuration dialogs
```

### 9.3 Package Sources and PPAs

**Viewing sources:**
```bash
cat /etc/apt/sources.list
ls /etc/apt/sources.list.d/
```

A sources line looks like:
```
deb https://deb.debian.org/debian bookworm main contrib non-free
```
Components: `main` (fully free), `contrib` (free but depends on non-free), `non-free` (proprietary).

**Adding a third-party repository (PPA — Personal Package Archive):**
```bash
# Modern method (Ubuntu 22.04+): using signed-by keyring
sudo curl -fsSL https://packagecloud.io/AtomEditor/atom/gpgkey \
  | sudo gpg --dearmor -o /usr/share/keyrings/atom.gpg
echo "deb [signed-by=/usr/share/keyrings/atom.gpg] https://packagecloud.io/AtomEditor/atom/any/ any main" \
  | sudo tee /etc/apt/sources.list.d/atom.list
sudo apt update && sudo apt install atom

# Ubuntu PPA shortcut (adds key automatically, but less transparent)
sudo add-apt-repository ppa:ondrej/nginx
sudo apt update && sudo apt install nginx
```

**Security note:** Only add repositories from sources you trust. A repository can serve any packages — including malicious ones — and they run as root during installation.

### 9.4 Holding and Pinning Packages

To prevent a package from being upgraded (e.g., you need a specific version):
```bash
sudo apt-mark hold nginx         # Hold at current version
sudo apt-mark unhold nginx       # Remove hold
apt-mark showhold                # Show held packages
```

### 9.5 Automatic Security Updates

For servers, enabling unattended security upgrades is essential:
```bash
sudo apt install unattended-upgrades
sudo dpkg-reconfigure --priority=low unattended-upgrades

# Configuration
cat /etc/apt/apt.conf.d/50unattended-upgrades
# Edit to control what gets auto-updated, and whether to auto-reboot

# Test (dry run)
sudo unattended-upgrades --dry-run --debug
```

The default configuration updates only security packages from the official Ubuntu security repository. For critical servers, add application packages to the auto-update list carefully — updates can cause service restarts.

### 9.6 Snap

Snap is Ubuntu's universal package format. A snap is a self-contained bundle that includes the application and all its dependencies, isolated from the system. Snaps update automatically in the background.

```bash
snap find vscode                 # Search snap store
snap install code --classic      # Install VS Code
snap install --channel=edge code # Install from edge channel
snap list                        # Installed snaps
snap info code                   # Details, versions, channels
snap refresh                     # Update all snaps
snap refresh code                # Update specific snap
snap remove code                 # Remove snap
snap revert code                 # Roll back to previous version
snap changes                     # History of snap operations
```

**Channels:** `stable` (default), `candidate`, `beta`, `edge` — progressively less stable.

**Confinement:** Most snaps run in strict confinement (cannot access the host system except through declared interfaces). `--classic` disables confinement for applications that need full system access (editors, IDEs, developer tools).

Snap services run as systemd units under the `snap.` prefix:
```bash
systemctl status snap.code.code.service
```

### 9.7 Flatpak

Flatpak is the community alternative to Snap, widely supported across distributions (not just Ubuntu). Apps are distributed via repositories called *remotes*; Flathub (`flathub.org`) is the primary remote.

```bash
sudo apt install flatpak
flatpak remote-add --if-not-exists flathub https://dl.flathub.org/repo/flathub.flatpakrepo

flatpak search gimp              # Search Flathub
flatpak install flathub org.gimp.GIMP
flatpak run org.gimp.GIMP        # Launch
flatpak list                     # Installed apps
flatpak update                   # Update all
flatpak remove org.gimp.GIMP     # Remove
flatpak history                  # Operation history
```

### 9.8 Building from Source

When no package exists, build from source:
```bash
# Standard autotools pattern (configure + make)
./configure --prefix=/usr/local
make -j$(nproc)          # Parallel build using all CPU cores
sudo make install

# CMake pattern
mkdir build && cd build
cmake .. -DCMAKE_INSTALL_PREFIX=/usr/local -DCMAKE_BUILD_TYPE=Release
make -j$(nproc)
sudo make install

# Clean up build dependencies afterwards
sudo apt autoremove
```

Always check whether a package or snap exists before compiling from source — security patches reach packages automatically but not manually installed source builds.

---

## Chapter 10: Networking — Concepts and Configuration

### 10.1 The Network Stack

Linux networking follows the TCP/IP model, implemented in kernel layers:

```
Application layer    — HTTP, SSH, DNS, SMTP (programs)
Transport layer      — TCP (reliable streams), UDP (datagrams)
Network layer        — IP, ICMP, routing
Data link layer      — Ethernet, Wi-Fi (managed by drivers)
Physical layer       — cables, radio waves (hardware)
```

System calls like `socket()`, `bind()`, `connect()`, `send()`, `recv()` are the API between user-space programs and the kernel's network stack.

### 10.2 Interfaces and Addresses

```bash
# Modern tools (iproute2 package — the standard since ~2010)
ip addr show                     # All interfaces with addresses
ip addr show eth0                # One interface
ip link show                     # Link-layer state (up/down, MAC address)
ip -br addr                      # Brief, color output

# Add/remove addresses (temporary — lost on reboot)
sudo ip addr add 192.168.1.100/24 dev eth0
sudo ip addr del 192.168.1.100/24 dev eth0
sudo ip link set eth0 up         # Bring interface up
sudo ip link set eth0 down       # Bring interface down
```

Interface naming in modern Linux uses **predictable names** based on hardware location:
- `eth0` — traditional naming (may still appear in VMs)
- `enp3s0` — PCI Ethernet: bus 3, slot 0
- `ens3` — PCI Ethernet, slot 3
- `wlan0` / `wlp2s0` — wireless interface
- `lo` — loopback interface (always `127.0.0.1`)

### 10.3 Routing

```bash
ip route show                    # Routing table
ip route show default            # Just the default gateway
ip route get 8.8.8.8             # Which route would be used for this destination

# Add/remove routes (temporary)
sudo ip route add 10.0.0.0/8 via 192.168.1.1 dev eth0
sudo ip route del 10.0.0.0/8
sudo ip route add default via 192.168.1.1    # Set default gateway
```

### 10.4 Persistent Network Configuration

**Netplan (Ubuntu 17.10+):** Ubuntu uses Netplan as the network configuration frontend. It generates configuration for either `networkd` (server) or `NetworkManager` (desktop).

Configuration files are YAML, stored in `/etc/netplan/`:
```yaml
# /etc/netplan/00-config.yaml
network:
  version: 2
  renderer: networkd        # or NetworkManager
  ethernets:
    enp3s0:
      dhcp4: true           # DHCP
  # For static address:
  # enp3s0:
  #   dhcp4: false
  #   addresses:
  #     - 192.168.1.50/24
  #   routes:
  #     - to: default
  #       via: 192.168.1.1
  #   nameservers:
  #     addresses: [8.8.8.8, 1.1.1.1]
```

```bash
sudo netplan try             # Apply configuration with 120-second rollback
sudo netplan apply           # Apply without rollback
sudo netplan generate        # Generate backend configuration without applying
```

**NetworkManager (desktop/laptop):**
```bash
nmcli device status          # List interfaces and status
nmcli connection show        # List saved connections
nmcli connection up "MyWifi"  # Activate a connection
nmcli dev wifi list          # Scan for Wi-Fi networks
nmcli dev wifi connect "SSID" password "passphrase"
nmcli connection modify eth0 ipv4.method manual ipv4.addresses "192.168.1.50/24"
nmtui                        # Text-based UI (easier for interactive use)
```

### 10.5 DNS Resolution

```bash
# Query DNS
dig google.com               # Full DNS query output
dig @8.8.8.8 google.com A    # Query specific nameserver for A record
dig google.com MX            # Mail exchange records
dig -x 8.8.8.8               # Reverse lookup (IP to name)
nslookup google.com          # Simpler alternative
host google.com              # Simple forward/reverse lookup

# System resolution
cat /etc/resolv.conf         # Configured nameservers (often managed by systemd-resolved)
resolvectl status            # systemd-resolved status and per-interface DNS
resolvectl query google.com  # Query through systemd-resolved

# /etc/nsswitch.conf controls resolution order:
grep hosts /etc/nsswitch.conf
# hosts: files dns   → /etc/hosts is checked first, then DNS

cat /etc/hosts               # Static hostname mappings
```

**Configuring DNS nameservers (Netplan):**
```yaml
nameservers:
  addresses: [1.1.1.1, 1.0.0.1]   # Cloudflare DNS
  search: [example.com]            # Search domain
```

### 10.6 Sockets and Open Connections

```bash
# ss — modern replacement for netstat
ss -tlnp                         # TCP listening sockets with PIDs
ss -ulnp                         # UDP listening sockets
ss -tlnp | grep :80              # Who is listening on port 80?
ss -s                            # Socket statistics summary
ss -ta                           # All TCP sockets (established, listening, etc.)
ss -tn dst 192.168.1.5          # Connections to specific remote host

# lsof — list open files (including network sockets)
sudo lsof -i :80                 # What's using port 80
sudo lsof -i TCP                 # All TCP connections
sudo lsof -i -P -n               # All network connections, no hostname resolution
sudo lsof -p 1234               # Open files for PID 1234
```

### 10.7 Connectivity Diagnostics

```bash
# Basic reachability
ping -c 4 8.8.8.8                # ICMP echo test (4 packets)
ping -c 4 google.com             # Tests DNS AND reachability
ping6 ::1                        # IPv6 loopback test

# Path tracing
traceroute 8.8.8.8               # Hop-by-hop path
traceroute -T 8.8.8.8            # TCP-based traceroute (bypasses ICMP blocks)
mtr 8.8.8.8                      # Live continuous traceroute
mtr --report 8.8.8.8             # Report mode (takes ~10 seconds, then exits)

# HTTP testing
curl -I https://example.com      # HTTP headers only (HEAD request)
curl -sS https://api.example.com/health   # Silent, show only errors
curl -w "\n%{http_code}\n" https://example.com  # With HTTP status code
curl -o /dev/null -w "%{time_total}\n" https://example.com  # Response time
wget -q --spider https://example.com     # Check URL without downloading

# TLS certificate inspection
echo | openssl s_client -connect example.com:443 -servername example.com 2>/dev/null \
  | openssl x509 -noout -dates   # Certificate validity dates
```

### 10.8 Network Performance

```bash
# Bandwidth test
iperf3 -s                        # Start iperf3 server
iperf3 -c server_ip              # Run client, measure bandwidth to server
iperf3 -c server_ip -R           # Reverse (server to client)
iperf3 -c server_ip -t 30 -P 4  # 30 seconds, 4 parallel streams

# Interface statistics
ip -s link show eth0             # TX/RX bytes and error counters
cat /proc/net/dev                # All interface statistics
sar -n DEV 1 5                   # Network statistics, 5 samples (sysstat package)

# Per-process network usage
sudo nethogs eth0                # Real-time bandwidth per process
sudo iftop -i eth0               # Real-time bandwidth per connection
```

### 10.9 Hostname and /etc/hosts

```bash
hostname                         # Current hostname
hostname -f                      # Fully qualified domain name (FQDN)
sudo hostnamectl set-hostname myserver.example.com   # Set persistently

# /etc/hosts: static name→IP mappings, checked before DNS
cat /etc/hosts
# Add custom mappings:
echo "192.168.1.100  devserver dev" | sudo tee -a /etc/hosts
```

### 10.10 Network Namespaces and Virtual Interfaces

Linux supports network namespaces — isolated network stacks with their own interfaces, routing tables, and firewall rules. Containers use this heavily.

```bash
# View namespaces
ip netns list

# Create a namespace and a veth pair
sudo ip netns add myns
sudo ip link add veth0 type veth peer name veth1
sudo ip link set veth1 netns myns

# Execute a command in a namespace
sudo ip netns exec myns ip addr show
sudo ip netns exec myns bash         # Shell inside the namespace
```

---

# PART VI — SERVICES AND OBSERVABILITY

---

## Chapter 11: Systemd — Services, Timers, and the Boot Process

### 11.1 Why systemd

Before systemd (pre-2011), Linux used SysV init: a shell-script-based system that started services sequentially, had no dependency tracking, and offered no standard way to restart crashed processes. systemd replaced this with a parallel, dependency-aware, event-driven init system that is now standard on virtually every major Linux distribution.

systemd's key advantages:
- Starts services in parallel, dramatically reducing boot time
- Automatically restarts crashed services
- Captures all service output to a structured, indexed journal
- Manages sockets, devices, mounts, and timers — not just services
- Provides a uniform interface: `systemctl` and `journalctl` for everything

### 11.2 Units

Everything systemd manages is a **unit**. Units are described by unit files and identified by a name and type:

| Type | Extension | Purpose |
|------|-----------|---------|
| Service | `.service` | A process (daemon or oneshot command) |
| Socket | `.socket` | Activates a service when a socket receives a connection |
| Timer | `.timer` | Runs a service on a schedule (cron replacement) |
| Target | `.target` | Group of units; synchronization point (like a runlevel) |
| Mount | `.mount` | Filesystem mount point |
| Device | `.device` | A udev-detected hardware device |
| Path | `.path` | Watches for filesystem changes |
| Slice | `.slice` | cgroups hierarchy for resource limiting |

Unit files live in:
- `/lib/systemd/system/` — installed by packages (do not edit)
- `/etc/systemd/system/` — administrator overrides (edit here)
- `~/.config/systemd/user/` — user-level units (no root needed)

### 11.3 systemctl — Managing Units

```bash
# Status
systemctl status nginx            # Status, recent logs, PID, resource usage
systemctl is-active nginx         # Prints "active" or "inactive" (useful in scripts)
systemctl is-enabled nginx        # Is it set to start at boot?
systemctl is-failed nginx         # Did it fail?

# Lifecycle
systemctl start nginx             # Start now
systemctl stop nginx              # Stop now
systemctl restart nginx           # Stop then start
systemctl reload nginx            # Send SIGHUP (reload config without restart)
systemctl reload-or-restart nginx # Reload if supported, else restart

# Boot configuration
systemctl enable nginx            # Start at boot (creates symlink)
systemctl disable nginx           # Don't start at boot
systemctl enable --now nginx      # Enable AND start immediately
systemctl disable --now nginx     # Disable AND stop immediately
systemctl mask nginx              # Prevent starting (even manually)
systemctl unmask nginx            # Undo mask

# Listing units
systemctl list-units              # All active units
systemctl list-units --type=service   # Only services
systemctl list-units --state=failed   # Failed units
systemctl list-unit-files --type=service  # All service unit files + enabled/disabled state
```

### 11.4 Targets (Boot Runlevels)

Targets are synchronization points that group units:

```bash
systemctl get-default              # Current default target
sudo systemctl set-default multi-user.target   # Boot to text (no GUI)
sudo systemctl set-default graphical.target    # Boot to GUI
sudo systemctl isolate rescue.target           # Switch to rescue mode now
```

Common targets and their SysV equivalents:

| Target | SysV Runlevel | Meaning |
|--------|--------------|---------|
| `poweroff.target` | 0 | Shut down |
| `rescue.target` | 1 | Single-user recovery mode |
| `multi-user.target` | 3 | Full system, no GUI |
| `graphical.target` | 5 | Full system with GUI |
| `reboot.target` | 6 | Reboot |

### 11.5 Writing Service Unit Files

```bash
sudo vim /etc/systemd/system/myapp.service
```

```ini
[Unit]
Description=My Web Application
Documentation=https://example.com/docs
After=network-online.target postgresql.service
Requires=postgresql.service
Wants=network-online.target

[Service]
Type=simple               # Process stays in foreground; systemd tracks PID directly
User=myapp
Group=myapp
WorkingDirectory=/opt/myapp
EnvironmentFile=/etc/myapp/env    # Load environment variables from file
ExecStart=/opt/myapp/bin/server --config /etc/myapp/config.yaml
ExecReload=/bin/kill -HUP $MAINPID
ExecStop=/bin/kill -TERM $MAINPID

Restart=on-failure        # Restart if process exits with non-zero status
RestartSec=5s             # Wait 5 seconds before restarting
StartLimitBurst=5         # Only try restarting 5 times...
StartLimitIntervalSec=60s # ...within a 60-second window

# Resource limits
LimitNOFILE=65536         # Max open file descriptors (overrides ulimit)
MemoryMax=1G              # OOM kill if this process uses more than 1GB
CPUQuota=50%              # Limit to 50% of one CPU core

# Hardening
NoNewPrivileges=yes       # Process cannot gain new privileges
PrivateTmp=yes            # Isolated /tmp
ProtectSystem=strict      # /usr, /boot, /etc read-only
ProtectHome=yes           # No access to /home
ReadWritePaths=/var/lib/myapp /var/log/myapp

StandardOutput=journal    # Capture stdout to journal
StandardError=journal

[Install]
WantedBy=multi-user.target   # Start when reaching multi-user.target
```

```bash
sudo systemctl daemon-reload        # Required after creating/editing unit files
sudo systemctl enable --now myapp   # Enable and start
sudo systemctl status myapp         # Verify it's running
```

**Service types:**
- `simple` (default): ExecStart is the main process. systemd tracks it directly.
- `exec`: Like simple, but systemd waits until exec() succeeds before marking it started.
- `forking`: Process forks and the parent exits. Use with traditional daemons. Specify `PIDFile=`.
- `notify`: Process sends `sd_notify()` when ready. Most reliable for start ordering.
- `oneshot`: Single run to completion. systemd considers it active after ExecStart exits.

### 11.6 Unit Overrides (Drop-in Files)

Instead of editing a package-provided unit file (which would be overwritten on upgrade), create an override:

```bash
sudo systemctl edit nginx           # Opens editor for override file
# This creates /etc/systemd/system/nginx.service.d/override.conf
```

Or manually:
```bash
sudo mkdir -p /etc/systemd/system/nginx.service.d/
sudo tee /etc/systemd/system/nginx.service.d/limits.conf << EOF
[Service]
LimitNOFILE=65536
MemoryMax=2G
EOF
sudo systemctl daemon-reload
sudo systemctl restart nginx
```

```bash
systemctl cat nginx                  # Shows effective unit file with overrides applied
```

### 11.7 Timers — systemd's cron Replacement

Timers run a service unit on a schedule. Every `.timer` unit activates a `.service` unit with the same name.

**Example: Run cleanup daily at 3am:**

```ini
# /etc/systemd/system/cleanup.service
[Unit]
Description=Clean up old temp files

[Service]
Type=oneshot
ExecStart=/usr/local/bin/cleanup.sh
User=root
```

```ini
# /etc/systemd/system/cleanup.timer
[Unit]
Description=Daily cleanup timer

[Timer]
OnCalendar=*-*-* 03:00:00    # Every day at 3:00 AM
Persistent=true               # Run if missed (e.g., system was off)
RandomizedDelaySec=10m        # Add up to 10-minute random delay (spread load)

[Install]
WantedBy=timers.target
```

```bash
sudo systemctl enable --now cleanup.timer
systemctl list-timers                    # See all timers and next run times
systemctl status cleanup.timer
```

**Calendar format examples:**
```
daily                          # Every day at midnight
weekly                         # Every Monday at midnight
monthly                        # First day of month at midnight
hourly                         # Every hour on the hour
*-*-* 09:30:00                 # Every day at 9:30 AM
Mon *-*-* 08:00:00             # Every Monday at 8 AM
*-*-1,15 12:00:00              # 1st and 15th of each month at noon
2026-06-01 00:00:00            # Once: June 1, 2026
```

### 11.8 User Services

Systemd supports per-user services that don't need root:
```bash
mkdir -p ~/.config/systemd/user/
# Create unit file there, then:
systemctl --user enable --now myservice
systemctl --user status myservice
journalctl --user -u myservice

# Enable user service to run even when not logged in (requires root once):
sudo loginctl enable-linger $USER
```

### 11.9 systemd at Boot: The Full Picture

```bash
# Analyze boot time
systemd-analyze                        # Total: firmware + loader + kernel + userspace
systemd-analyze blame                  # Which units took the longest
systemd-analyze critical-chain         # The chain that determined total boot time
systemd-analyze plot > /tmp/boot.svg   # Full waterfall diagram

# Power management
systemctl poweroff                     # Shutdown
systemctl reboot                       # Reboot
systemctl suspend                      # Suspend to RAM
systemctl hibernate                    # Suspend to disk
systemctl hybrid-sleep                 # Suspend + hibernate simultaneously
```

---

## Chapter 12: Logs, Monitoring, and Observability

### 12.1 The Two Logging Systems

Modern Linux has two parallel logging mechanisms:

1. **systemd journal** (`journald`): structured binary log that captures everything systemd starts. Fast to query, indexed, with metadata. The primary source for service debugging.
2. **Syslog** (`rsyslog` or `syslog-ng`): traditional text-based logging. Applications write to `/dev/log`; rsyslog routes to files in `/var/log/`. Many applications still use this interface.

On Ubuntu, both run simultaneously. The journal captures everything; rsyslog receives a copy and writes to `/var/log/`.

### 12.2 journalctl — Querying the Journal

```bash
journalctl                           # All journal entries (oldest first)
journalctl -r                        # Reverse (newest first)
journalctl -f                        # Follow live (like tail -f)
journalctl -n 50                     # Last 50 lines
journalctl -b                        # This boot only
journalctl -b -1                     # Previous boot
journalctl -b -2                     # Two boots ago
journalctl --list-boots              # List available boots

# Filter by unit
journalctl -u nginx                  # All logs for nginx
journalctl -u nginx -f               # Follow nginx logs
journalctl -u nginx -u postgresql    # Multiple units

# Filter by time
journalctl --since "2026-06-01 10:00"
journalctl --since "1 hour ago"
journalctl --since today
journalctl --until "2026-06-01 12:00"
journalctl --since "2026-06-01 10:00" --until "2026-06-01 12:00"

# Filter by priority
journalctl -p err                    # Errors and above
journalctl -p warning..err           # Warnings and errors only
# Priorities: emerg(0), alert(1), crit(2), err(3), warning(4), notice(5), info(6), debug(7)

# Filter by identifier
journalctl -t nginx                  # SYSLOG_IDENTIFIER = nginx
journalctl _PID=1234                 # Specific PID
journalctl _UID=1000                 # Specific user

# Output formats
journalctl -u nginx -o json          # JSON output (one object per line)
journalctl -u nginx -o json-pretty   # Pretty-printed JSON
journalctl -u nginx -o short-iso     # ISO timestamps
journalctl -u nginx -o cat           # Just the message, no metadata

# Journal size management
journalctl --disk-usage              # How much disk the journal uses
sudo journalctl --vacuum-size=500M   # Trim to 500 MB
sudo journalctl --vacuum-time=30d    # Remove entries older than 30 days
```

**Configure journal retention** in `/etc/systemd/journald.conf`:
```ini
[Journal]
SystemMaxUse=1G          # Total journal size limit
SystemKeepFree=500M      # Keep at least this much free
MaxRetentionSec=30d      # Delete entries older than 30 days
Compress=yes             # Compress journal files
```

### 12.3 Traditional Log Files

```bash
/var/log/syslog           # General system messages (Ubuntu/Debian)
/var/log/messages         # General system messages (RHEL/CentOS)
/var/log/auth.log         # Authentication events (SSH logins, sudo, su)
/var/log/kern.log         # Kernel messages
/var/log/dpkg.log         # Package install/remove history
/var/log/apt/             # APT operation logs
/var/log/nginx/           # Nginx access and error logs
/var/log/apache2/         # Apache access and error logs
/var/log/mail.log         # Mail server logs
/var/log/ufw.log          # UFW firewall log
```

```bash
tail -f /var/log/syslog                    # Live view
tail -f /var/log/nginx/error.log           # Nginx errors live
grep "ERROR" /var/log/syslog | tail -50    # Last 50 errors
zcat /var/log/syslog.2.gz | grep "OOM"    # Search compressed rotated log
```

**Log rotation:** `logrotate` automatically rotates, compresses, and removes old log files. Configuration is in `/etc/logrotate.conf` and `/etc/logrotate.d/`.

### 12.4 Resource Monitoring

**`top` and `htop`:**
```bash
top                              # CPU, memory, load average, per-process stats
# Key bindings inside top:
# P — sort by CPU, M — sort by memory, T — sort by time
# k — kill, r — renice, 1 — show per-CPU, q — quit

htop                             # Enhanced: scroll, mouse, tree view, color
# F5 — tree view, F6 — sort, F9 — kill, F10 — quit
```

**CPU:**
```bash
mpstat 1 5                       # CPU stats per core, 5 samples (sysstat)
sar -u 1 5                       # CPU utilization (sysstat)
sar -q 1 5                       # Load average history
vmstat 1 5                       # CPU, memory, I/O, system in one view
cat /proc/loadavg                # Raw: 1min 5min 15min running/total lastpid
```

Load average represents the average number of processes in a runnable or uninterruptible state. A load of `1.0` on a single-core system means 100% utilization. On a 4-core system, `4.0` is full utilization. Rule of thumb: load average > number of CPUs sustained = bottleneck.

**Memory:**
```bash
free -h                          # RAM and swap usage (human-readable)
cat /proc/meminfo                # Detailed breakdown
# Key values: MemTotal, MemFree, MemAvailable (most important), Cached, Buffers
# MemAvailable ≠ MemFree: available includes reclaimable cache
vmstat -s                        # Memory statistics summary
```

**Disk I/O:**
```bash
iostat -xz 1 5                   # Per-device I/O statistics
# Key columns: %util (device utilization), await (average I/O wait ms)
# %util > 80% sustained = disk bottleneck
iotop -ao                        # Per-process I/O (accumulated)
sudo iotop                       # Live per-process I/O (interactive)
```

**Network:**
```bash
sar -n DEV 1 5                   # Network interface statistics
ifstat                           # Per-interface throughput (apt install ifstat)
sudo nethogs eth0                # Per-process bandwidth
nload eth0                       # Real-time bandwidth graph (apt install nload)
```

### 12.5 System-Wide Dashboard

```bash
# All-in-one system overview
glances                          # Python-based; CPU, mem, disk, network, processes
# Install: pip3 install glances --break-system-packages
# or: sudo apt install glances

# Traditional: collect all metrics at once
echo "=== Date ===" && date
echo "=== Uptime/Load ===" && uptime
echo "=== Memory ===" && free -h
echo "=== Disk ===" && df -h
echo "=== Top Processes ===" && ps aux --sort=-%cpu | head -6
```

### 12.6 Alerting with Auditd

`auditd` is the Linux kernel audit system — it can log virtually any system event: file accesses, system calls, user logins, privilege escalations.

```bash
sudo apt install auditd audispd-plugins

# Add audit rules
sudo auditctl -w /etc/passwd -p rwa -k user_changes    # Watch passwd file
sudo auditctl -w /etc/sudoers -p rwa -k sudo_changes   # Watch sudoers
sudo auditctl -a always,exit -F arch=b64 -S execve -k exec_tracking  # Track all execs

# Query the audit log
sudo ausearch -k user_changes                          # By key
sudo ausearch -f /etc/passwd                           # By file
sudo ausearch --start today -m USER_AUTH               # Today's auth events
sudo aureport                                          # Summary report
sudo aureport --login                                  # Login summary
sudo aureport --failed                                 # Failed event summary
```

Persistent rules go in `/etc/audit/rules.d/audit.rules`. A starting-point hardening ruleset is available from the Linux Audit project on GitHub.

### 12.7 eBPF Observability (Modern Approach)

eBPF (extended Berkeley Packet Filter) is a kernel technology that allows running sandboxed programs inside the kernel without modifying kernel source. It is the foundation of modern observability tooling.

```bash
# BCC tools (apt install bpfcc-tools linux-headers-$(uname -r))
sudo execsnoop-bpfcc              # Trace every exec() call system-wide
sudo opensnoop-bpfcc              # Trace every open() call
sudo tcpconnect-bpfcc             # Trace every outgoing TCP connection
sudo tcpaccept-bpfcc              # Trace every incoming TCP connection
sudo biolatency-bpfcc             # Block I/O latency histogram
sudo runqlat-bpfcc                # CPU scheduler run queue latency

# bpftrace — eBPF scripting language
sudo apt install bpftrace
sudo bpftrace -e 'tracepoint:syscalls:sys_enter_execve { printf("%s\n", str(args->filename)); }'
# Prints every program name that gets executed, in real time
```

eBPF tools are especially valuable for diagnosing performance problems that don't show up in traditional metrics — scheduler latency, specific syscall patterns, micro-second I/O issues.

---

# PART VII — SECURITY

---

## Chapter 13: Linux Security Fundamentals

### 13.1 The Security Mindset

Security is not a product you install — it is a practice you maintain. The fundamental principle is **defense in depth**: layer multiple independent controls so that defeating one does not compromise the entire system. A corollary is the **principle of least privilege**: every user, service, and process should have exactly the permissions it needs and nothing more.

In 2026, Linux servers face a hostile internet. Automated scanners probe every public IP for open ports within minutes of allocation. Exploitation cycles run in hours after a vulnerability is published. The baseline is: assume the network is untrusted, patch continuously, minimize attack surface, and monitor everything.

### 13.2 Kernel Security Mechanisms

**Namespaces** isolate resources: PID namespaces (processes can't see each other), network namespaces (isolated network stacks), mount namespaces (isolated filesystem views), user namespaces (map UIDs independently). Containers are built on top of these.

**cgroups (control groups)** limit and account for resource usage by groups of processes. They prevent a single process from consuming all CPU, memory, or I/O. systemd uses cgroups for every service.

**Capabilities** divide root's omnipotence into ~40 distinct privileges. Instead of giving a process full root, you grant only what it needs: `CAP_NET_BIND_SERVICE` (bind ports < 1024), `CAP_SYS_ADMIN` (various admin ops), `CAP_NET_RAW` (raw sockets for ping).

```bash
# View capabilities of a process
cat /proc/1234/status | grep Cap
capsh --decode=0000000000003000   # Decode capability bitmask

# Set capabilities on a binary (so it doesn't need SUID root)
sudo setcap cap_net_bind_service=ep /usr/bin/node    # node can bind port 80/443
getcap /usr/bin/node                                  # View capabilities
sudo setcap -r /usr/bin/node                          # Remove capabilities
```

**Seccomp (Secure Computing Mode)** filters syscalls a process can make. Docker uses seccomp profiles to block ~44 dangerous syscalls from containers. systemd services can use `SystemCallFilter=` to restrict syscalls.

**Landlock** (kernel 5.13+) is an unprivileged sandboxing mechanism for restricting filesystem access of individual programs, without requiring root configuration.

### 13.3 Mandatory Access Control: AppArmor and SELinux

Standard Unix DAC (Discretionary Access Control) lets the owner of a file decide who can access it. MAC (Mandatory Access Control) adds a system-wide policy that constrains even root.

**AppArmor** (Ubuntu default): profiles define what files, capabilities, and network operations a program is allowed. Profiles can be in *enforcing* (violations blocked and logged) or *complain* mode (violations logged only).

```bash
sudo apt install apparmor apparmor-utils

# Status
sudo apparmor_status
aa-status                          # Same

# Profiles
ls /etc/apparmor.d/                # Installed profiles
sudo aa-enforce /etc/apparmor.d/usr.sbin.nginx    # Enable enforcing mode
sudo aa-complain /etc/apparmor.d/usr.sbin.nginx   # Complain mode (testing)
sudo aa-disable /etc/apparmor.d/usr.sbin.nginx    # Disable profile

# Generate a profile for a new application
sudo aa-genprof /usr/local/bin/myapp    # Interactive profile generator
sudo aa-logprof                          # Update profile from logged violations

# Reload after editing a profile
sudo apparmor_parser -r /etc/apparmor.d/usr.sbin.nginx
```

**SELinux** (RHEL/Fedora default): more powerful and complex than AppArmor. Every object (file, process, socket) has a *label* (security context), and a policy defines what labels are allowed to interact. SELinux is a type enforcement system; violations generate AVC (Access Vector Cache) denials.

```bash
# Status (RHEL/Fedora)
getenforce                         # Enforcing / Permissive / Disabled
sestatus                           # Detailed status and policy info
sudo setenforce 0                  # Set permissive mode (temporary, for testing)
sudo setenforce 1                  # Re-enable enforcing

# Context of files and processes
ls -Z /var/www/html/               # File context
ps auxZ | grep nginx               # Process context
id -Z                              # Your current context

# Fix mislabeled files
sudo restorecon -Rv /var/www/html/  # Restore correct labels recursively

# Diagnose denials
sudo ausearch -m AVC -ts recent    # Recent AVC denials from audit log
sudo sealert -a /var/log/audit/audit.log  # Human-readable analysis
```

**Best practice 2026:** Run AppArmor (Ubuntu) or SELinux (RHEL) in *enforcing* mode, never permissive in production. Use complain mode temporarily when developing profiles, then switch to enforcing.

### 13.4 System Hardening Checklist

**User accounts:**
```bash
# List users with UID 0 (should only be root)
awk -F: '$3 == 0 {print $1}' /etc/passwd

# Lock accounts that don't need login access
sudo usermod -L username           # Lock the account
sudo usermod -s /usr/sbin/nologin username  # Change shell to nologin

# Password aging policy
sudo chage -M 90 -W 14 username    # Max 90 days, warn 14 days before expiry
sudo chage -l username             # View current policy

# Remove or disable unnecessary system accounts
cat /etc/passwd | grep -v nologin | grep -v false  # Users that can log in
```

**Minimize installed software:**
```bash
# Remove unnecessary packages
sudo apt remove --purge telnet ftp rsh-client

# List installed services that start at boot
systemctl list-unit-files --type=service --state=enabled
# Disable anything you don't recognize or don't need:
sudo systemctl disable --now avahi-daemon  # mDNS — not needed on servers
sudo systemctl disable --now cups          # Printing — not needed on servers
```

**File permissions audit:**
```bash
# Find world-writable files (security risk)
find / -xdev -type f -perm -o+w 2>/dev/null | grep -v /proc | grep -v /sys

# Find SUID/SGID binaries (potential privilege escalation)
find / -xdev \( -perm -4000 -o -perm -2000 \) -type f 2>/dev/null

# Ensure sensitive files have correct permissions
stat /etc/passwd        # Should be 644
stat /etc/shadow        # Should be 640 (root:shadow)
stat /etc/sudoers       # Should be 440
```

**Kernel hardening via sysctl:**
```bash
# /etc/sysctl.d/99-hardening.conf

# Disable IP forwarding (unless this is a router)
net.ipv4.ip_forward = 0

# Ignore ICMP redirects (prevent routing table poisoning)
net.ipv4.conf.all.accept_redirects = 0
net.ipv6.conf.all.accept_redirects = 0

# Ignore source routing
net.ipv4.conf.all.accept_source_route = 0

# Enable SYN flood protection
net.ipv4.tcp_syncookies = 1

# Randomize virtual address space (ASLR)
kernel.randomize_va_space = 2

# Restrict ptrace (prevent process hijacking)
kernel.yama.ptrace_scope = 1

# Hide kernel pointers in /proc
kernel.kptr_restrict = 2

# Restrict dmesg access
kernel.dmesg_restrict = 1
```

```bash
sudo sysctl --system   # Apply all sysctl.d files without rebooting
```

### 13.5 Patch Management

Unpatched systems are the most common attack vector. Best practice is to patch aggressively and continuously.

```bash
# Check for available security updates
apt list --upgradable 2>/dev/null | grep -i security

# Apply only security updates
sudo unattended-upgrades --dry-run
sudo unattended-upgrades

# Check for kernel update (requires reboot)
dpkg -l | grep linux-image-$(uname -r)
# If the installed version differs from running version, a reboot is needed

# Check if reboot is required
cat /var/run/reboot-required 2>/dev/null && echo "REBOOT REQUIRED"
```

For systems that cannot tolerate reboots, **livepatch** (Ubuntu) applies kernel security patches without rebooting:
```bash
sudo snap install canonical-livepatch
sudo canonical-livepatch enable <token>
canonical-livepatch status
```

### 13.6 Intrusion Detection

```bash
# Rootkit detection
sudo apt install rkhunter chkrootkit
sudo rkhunter --check --sk          # Check for rootkits (-sk: skip keypress)
sudo chkrootkit                     # Alternative rootkit scanner

# File integrity monitoring
sudo apt install aide
sudo aideinit                       # Initialize database (run once after clean install)
sudo aide --check                   # Check for changes

# Fail2ban: ban IPs that fail authentication repeatedly
sudo apt install fail2ban
sudo systemctl enable --now fail2ban
sudo fail2ban-client status         # Overall status
sudo fail2ban-client status sshd    # SSH jail status
sudo fail2ban-client set sshd banip 1.2.3.4    # Manual ban
sudo fail2ban-client set sshd unbanip 1.2.3.4  # Unban
```

---

## Chapter 14: Firewalls, SSH, and Network Security

### 14.1 UFW — The Ubuntu Firewall

UFW (Uncomplicated Firewall) is a frontend to iptables/nftables designed for ease of use. It is the recommended firewall tool on Ubuntu.

```bash
sudo ufw status verbose            # Current rules and status
sudo ufw enable                    # Enable firewall
sudo ufw disable                   # Disable firewall (dangerous on production)

# Default policies (set these first)
sudo ufw default deny incoming     # Block all inbound by default
sudo ufw default allow outgoing    # Allow all outbound by default
sudo ufw default deny forward      # Block forwarding (unless this is a router)

# Allow specific services
sudo ufw allow ssh                 # Port 22 (by service name)
sudo ufw allow 22/tcp              # Equivalent
sudo ufw allow 80/tcp              # HTTP
sudo ufw allow 443/tcp             # HTTPS
sudo ufw allow 8080                # Custom port (both TCP and UDP)

# Restrict by source IP
sudo ufw allow from 192.168.1.0/24 to any port 22    # SSH only from LAN
sudo ufw allow from 10.0.0.5 to any port 5432         # PostgreSQL from one IP

# Deny specific traffic
sudo ufw deny 23/tcp               # Block telnet

# Application profiles (pre-defined port sets)
sudo ufw app list                  # Available profiles
sudo ufw allow "Nginx Full"        # Allows 80 and 443
sudo ufw allow "OpenSSH"

# Delete rules
sudo ufw status numbered           # Show rules with numbers
sudo ufw delete 5                  # Delete rule number 5
sudo ufw delete allow 80/tcp       # Delete by rule definition

# Rate limiting (blocks IPs making more than 6 connections/30 seconds)
sudo ufw limit ssh                 # Basic brute force protection
```

### 14.2 iptables / nftables — The Underlying Engine

UFW generates iptables (or nftables on newer kernels) rules. For complex scenarios, you may need to work directly with them.

```bash
# View rules
sudo iptables -L -n -v             # All rules with packet/byte counts
sudo iptables -L -n -v --line-numbers   # With line numbers for deletion
sudo iptables -t nat -L -n -v      # NAT table rules
sudo ip6tables -L -n -v            # IPv6 rules

# nftables (modern replacement for iptables, default on Debian 12+)
sudo nft list ruleset              # All nftables rules
sudo nft list table inet filter    # Specific table
```

For complex persistent rules, use `nftables` with `/etc/nftables.conf` rather than `iptables` commands (which don't persist without additional tooling).

### 14.3 SSH — Secure Shell

SSH is the primary tool for remote Linux administration. The default configuration is reasonably secure but benefits from hardening.

**Connecting:**
```bash
ssh user@hostname                  # Connect (default port 22)
ssh -p 2222 user@hostname          # Custom port
ssh -i ~/.ssh/mykey user@hostname  # Specific private key
ssh -A user@hostname               # Agent forwarding (use carefully)
ssh -X user@hostname               # X11 forwarding
ssh -L 8080:localhost:80 user@host # Local port forwarding
ssh -R 2222:localhost:22 user@host # Remote port forwarding (reverse tunnel)
ssh -D 1080 user@host              # SOCKS proxy
```

### 14.4 SSH Key Authentication

Password authentication over SSH is weaker than key authentication. Use keys.

**Generating a key pair:**
```bash
# Ed25519 is the recommended algorithm in 2026 — small, fast, secure
ssh-keygen -t ed25519 -C "aditya@myserver"
# Saves: ~/.ssh/id_ed25519 (private key — NEVER share)
#        ~/.ssh/id_ed25519.pub (public key — safe to share)

# RSA 4096 (for compatibility with older systems)
ssh-keygen -t rsa -b 4096 -C "aditya@myserver"
```

**Deploying the public key:**
```bash
ssh-copy-id -i ~/.ssh/id_ed25519.pub user@server    # Automatic method
# Manual: append ~/.ssh/id_ed25519.pub to ~/.ssh/authorized_keys on server
```

**SSH agent** (so you only enter your passphrase once per session):
```bash
eval "$(ssh-agent -s)"              # Start agent
ssh-add ~/.ssh/id_ed25519           # Load key (prompts for passphrase once)
ssh-add -l                          # List loaded keys
```

### 14.5 Hardening sshd

Edit `/etc/ssh/sshd_config`:

```bash
# Disable root login — always
PermitRootLogin no

# Disable password authentication — always (use keys only)
PasswordAuthentication no
PubkeyAuthentication yes

# Disable challenge-response (keyboard-interactive) auth
ChallengeResponseAuthentication no
KbdInteractiveAuthentication no    # Modern Ubuntu equivalent

# Restrict who can log in
AllowUsers aditya deploy           # Whitelist specific users
AllowGroups ssh-users              # Or whitelist a group

# Restrict access to specific IP ranges (in sshd_config)
# Better to use ufw rules, but can combine both

# Idle timeout (disconnect after 15 minutes of inactivity)
ClientAliveInterval 300
ClientAliveCountMax 3

# Limit authentication attempts
MaxAuthTries 3

# Disable X11 forwarding (unless needed)
X11Forwarding no

# Disable agent forwarding (unless needed)
AllowAgentForwarding no

# Use modern algorithms only
Protocol 2
KexAlgorithms curve25519-sha256,curve25519-sha256@libssh.org
HostKeyAlgorithms ssh-ed25519,rsa-sha2-512,rsa-sha2-256
Ciphers chacha20-poly1305@openssh.com,aes256-gcm@openssh.com
MACs hmac-sha2-256-etm@openssh.com,hmac-sha2-512-etm@openssh.com

# Logging
LogLevel VERBOSE                   # Log key fingerprints on login

# Disable TCP forwarding if not needed
AllowTcpForwarding no
```

```bash
sudo sshd -t                        # Test configuration syntax
sudo systemctl restart sshd         # Apply changes
```

**Two-factor authentication for SSH:**
```bash
sudo apt install libpam-google-authenticator
google-authenticator                # Set up for your user (generates QR code)

# Edit /etc/pam.d/sshd — add:
auth required pam_google_authenticator.so

# Edit /etc/ssh/sshd_config:
AuthenticationMethods publickey,keyboard-interactive
KbdInteractiveAuthentication yes
```

### 14.6 SSH Config File

`~/.ssh/config` provides per-host SSH configuration, dramatically simplifying connections:

```
# ~/.ssh/config

Host myserver
    HostName 203.0.113.10
    User aditya
    IdentityFile ~/.ssh/id_ed25519
    Port 22

Host bastion
    HostName bastion.example.com
    User aditya
    IdentityFile ~/.ssh/id_ed25519

Host internal-server
    HostName 10.0.1.50
    User aditya
    ProxyJump bastion         # Connect via bastion host
    IdentityFile ~/.ssh/id_ed25519

Host *
    ServerAliveInterval 60    # Keep connection alive
    ServerAliveCountMax 3
    AddKeysToAgent yes
    IdentitiesOnly yes
```

With this config: `ssh myserver` — instead of `ssh -i ~/.ssh/id_ed25519 -p 22 aditya@203.0.113.10`.

### 14.7 TLS Certificates with Let's Encrypt

For any public web service, use TLS. Let's Encrypt provides free, automated certificates.

```bash
sudo apt install certbot python3-certbot-nginx

# Obtain and install certificate (Nginx)
sudo certbot --nginx -d example.com -d www.example.com

# Standalone mode (when no web server is running)
sudo certbot certonly --standalone -d example.com

# Certificates are stored in:
ls /etc/letsencrypt/live/example.com/
# fullchain.pem, privkey.pem, cert.pem, chain.pem

# Auto-renewal (certbot installs a systemd timer automatically)
systemctl status certbot.timer
sudo certbot renew --dry-run         # Test renewal
```

### 14.8 Scanning Your Own System

Regularly scan your system from the outside to see what attackers see:

```bash
# Port scanning (scan your own IP — scanning others without permission is illegal)
nmap -sV -p 1-65535 localhost       # All ports with version detection
nmap -A localhost                    # OS detection + scripts
nmap --script vuln localhost         # Vulnerability scanning scripts

# Check for open ports from inside
ss -tlnp                             # What's listening?

# OpenVAS / Greenbone (full vulnerability scanner)
sudo apt install openvas
sudo gvm-setup                       # Initial setup (takes 30+ minutes)
```

---

# PART VIII — AUTOMATION

---

## Chapter 15: Bash Scripting — Variables, Control Flow, and Functions

### 15.1 Why Bash Scripting

Shell scripts automate repetitive tasks, enforce consistency, and document procedures as executable code. A script is a plain text file containing shell commands. Its main advantage over other languages is direct access to every Unix tool without glue code — you can call `grep`, `awk`, `systemctl`, `curl`, or any command in one line.

Bash scripting has sharp edges, especially around word splitting and globbing. This chapter teaches safe patterns from the start.

### 15.2 Script Structure and Headers

Every Bash script should start with:

```bash
#!/usr/bin/env bash
#
# deploy.sh — Deploy the application
# Usage: ./deploy.sh [--dry-run] <environment>
#
# Description:
#   Pulls latest code, runs migrations, and restarts the service.
#
# Author: Aditya
# Date: 2026-06-01

set -euo pipefail
# -e: exit immediately if any command fails
# -u: treat unset variables as errors (prevents "$UNSET_VAR" = "")
# -o pipefail: pipeline fails if any component fails (not just the last)

# Optional: print each command before executing (useful for debugging)
# set -x
```

**`#!/usr/bin/env bash`** is preferred over `#!/bin/bash` because it finds bash wherever it is installed — important for portability and virtual environments.

Make the script executable:
```bash
chmod +x deploy.sh
./deploy.sh
```

### 15.3 Variables

```bash
# Assignment: no spaces around =
name="Aditya"
count=42
path="/var/log/app"

# Reading variables: always double-quote to prevent word splitting
echo "$name"
echo "Count: $count"
echo "Path: $path"

# Readonly variables (constants)
readonly MAX_RETRIES=5
declare -r API_BASE="https://api.example.com"

# Integer arithmetic
count=$((count + 1))
doubled=$((count * 2))
remainder=$((17 % 5))

# String operations
greeting="Hello, World"
echo "${#greeting}"              # Length: 13
echo "${greeting:0:5}"          # Substring: Hello
echo "${greeting/World/Bash}"   # Replace: Hello, Bash
echo "${greeting,,}"            # Lowercase
echo "${greeting^^}"            # Uppercase
```

### 15.4 Safe Variable Patterns

```bash
# Default value if unset or empty
log_dir="${LOG_DIR:-/var/log/myapp}"

# Abort with message if unset or empty
config="${CONFIG_FILE:?ERROR: CONFIG_FILE must be set}"

# Use value only if set
extra="${EXTRA_ARGS:+--extra $EXTRA_ARGS}"   # empty string if EXTRA_ARGS unset

# Parameter expansion for paths
full_path="/home/aditya/documents/report.pdf"
echo "${full_path##*/}"          # Filename: report.pdf
echo "${full_path%/*}"           # Directory: /home/aditya/documents
echo "${full_path%.*}"           # Without extension: /home/aditya/documents/report
echo "${full_path##*.}"          # Extension only: pdf
```

### 15.5 Control Flow

**if/elif/else:**
```bash
if [[ -f "$config" ]]; then
    echo "Config found: $config"
elif [[ -d "$config" ]]; then
    echo "That's a directory, not a file"
else
    echo "Config not found: $config" >&2
    exit 1
fi
```

**Test operators in `[[ ]]`:**

| Operator | Tests |
|----------|-------|
| `-f file` | File exists and is a regular file |
| `-d file` | File exists and is a directory |
| `-e file` | File exists (any type) |
| `-r file` | File exists and is readable |
| `-w file` | File exists and is writable |
| `-x file` | File exists and is executable |
| `-s file` | File exists and has size > 0 |
| `-z "$str"` | String is empty |
| `-n "$str"` | String is non-empty |
| `"$a" == "$b"` | Strings are equal |
| `"$a" != "$b"` | Strings are not equal |
| `"$a" =~ regex` | String matches regex |
| `$a -eq $b` | Numbers are equal |
| `$a -ne $b` | Numbers are not equal |
| `$a -lt $b` | Less than |
| `$a -gt $b` | Greater than |
| `$a -le $b` | Less than or equal |
| `$a -ge $b` | Greater than or equal |
| `! condition` | Logical NOT |
| `cond1 && cond2` | Logical AND |
| `cond1 \|\| cond2` | Logical OR |

**case statement:**
```bash
case "$environment" in
    production|prod)
        server="prod.example.com"
        ;;
    staging)
        server="staging.example.com"
        ;;
    development|dev|"")
        server="localhost"
        ;;
    *)
        echo "Unknown environment: $environment" >&2
        exit 1
        ;;
esac
```

**for loops:**
```bash
# Iterate over list
for name in Alice Bob Charlie; do
    echo "Hello, $name"
done

# Iterate over files (safe — handles spaces in names)
for file in /var/log/*.log; do
    [[ -e "$file" ]] || continue    # Skip if glob didn't match anything
    echo "Processing: $file"
done

# Iterate over array
fruits=("apple" "banana" "cherry")
for fruit in "${fruits[@]}"; do
    echo "$fruit"
done

# C-style for loop
for (( i=0; i<10; i++ )); do
    echo "Iteration $i"
done

# Iterate over command output (SAFE pattern using process substitution)
while IFS= read -r line; do
    echo "Line: $line"
done < <(find /etc -name "*.conf" 2>/dev/null)
```

**while and until:**
```bash
# While: repeat while condition is true
count=0
while [[ $count -lt 5 ]]; do
    echo "Count: $count"
    (( count++ ))
done

# Until: repeat until condition is true (opposite of while)
until systemctl is-active --quiet myapp; do
    echo "Waiting for myapp to start..."
    sleep 2
done
echo "myapp is running"

# Read from stdin line by line
while IFS= read -r line; do
    process_line "$line"
done < input.txt
```

### 15.6 Functions

```bash
# Define a function
greet() {
    local name="$1"           # Always use 'local' for function variables
    local greeting="${2:-Hello}"   # Default value for second argument
    echo "$greeting, $name!"
}

# Call it
greet "Aditya"               # Hello, Aditya!
greet "Aditya" "Good morning"  # Good morning, Aditya!

# Return values
# Functions return status codes (0-255) via 'return'
# Return data via stdout — capture with $()
get_timestamp() {
    date +"%Y-%m-%d %H:%M:%S"
}
timestamp=$(get_timestamp)

# Functions can set global variables as a side-effect
parse_args() {
    DRY_RUN=false
    VERBOSE=false
    while [[ $# -gt 0 ]]; do
        case "$1" in
            --dry-run) DRY_RUN=true ;;
            --verbose) VERBOSE=true ;;
            *) echo "Unknown option: $1" >&2; return 1 ;;
        esac
        shift
    done
}
```

### 15.7 Input Validation

```bash
# Validate required argument
if [[ $# -lt 1 ]]; then
    echo "Usage: $0 <environment>" >&2
    exit 1
fi

# Validate against allowed values
environment="$1"
case "$environment" in
    production|staging|development) ;;
    *) echo "Invalid environment: $environment" >&2; exit 1 ;;
esac

# Validate numeric input
if ! [[ "$port" =~ ^[0-9]+$ ]] || (( port < 1 || port > 65535 )); then
    echo "Invalid port: $port" >&2
    exit 1
fi

# Validate file path is inside allowed directory
validate_path() {
    local input_path="$1"
    local allowed_base="$2"
    local real_path
    real_path=$(realpath -m "$input_path")
    if [[ "$real_path" != "$allowed_base"/* ]]; then
        echo "Error: path outside allowed directory: $real_path" >&2
        return 1
    fi
    echo "$real_path"
}
safe_path=$(validate_path "$user_path" "/var/data") || exit 1
```

### 15.8 Arrays

```bash
# Indexed arrays
fruits=("apple" "banana" "cherry")
echo "${fruits[0]}"              # apple (first element)
echo "${fruits[-1]}"             # cherry (last element)
echo "${fruits[@]}"              # All elements
echo "${#fruits[@]}"             # Count: 3
fruits+=("date")                 # Append
unset 'fruits[1]'               # Remove element (creates sparse array)

# Associative arrays (bash 4+)
declare -A config
config["host"]="localhost"
config["port"]="5432"
config["dbname"]="mydb"

echo "${config[host]}"
for key in "${!config[@]}"; do
    echo "$key = ${config[$key]}"
done
```

### 15.9 Here Documents and Here Strings

```bash
# Here-doc: multi-line string to stdin
cat << 'EOF'
This is a literal multi-line string.
$variables are not expanded when the delimiter is quoted.
EOF

cat << EOF
Hello $USER — this string DOES expand variables.
Today is $(date +%Y-%m-%d).
EOF

# Here-doc to a command
sudo tee /etc/myapp/config.conf << EOF
server_host = 127.0.0.1
server_port = 8080
log_level = info
EOF

# Indented here-doc (remove leading tabs with <<-)
if [[ "$condition" ]]; then
    cat <<- EOF
	This text has leading tabs removed.
	Useful inside indented blocks.
	EOF
fi

# Here-string: single-line stdin
grep "pattern" <<< "some text to search in"
base64 --decode <<< "SGVsbG8gV29ybGQ="
```

### 15.10 Practical Script: Deployment Example

```bash
#!/usr/bin/env bash
# deploy.sh — Deploy application to a given environment
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
APP_DIR="/opt/myapp"
SERVICE_NAME="myapp"

usage() {
    cat << EOF
Usage: $0 [--dry-run] <environment>
  environment: production | staging | development
  --dry-run:   Show what would happen without making changes
EOF
    exit 1
}

log() { echo "[$(date +%T)] $*"; }
die() { echo "[ERROR] $*" >&2; exit 1; }

# Parse arguments
DRY_RUN=false
ENVIRONMENT=""
while [[ $# -gt 0 ]]; do
    case "$1" in
        --dry-run) DRY_RUN=true ;;
        production|staging|development) ENVIRONMENT="$1" ;;
        *) usage ;;
    esac
    shift
done

[[ -n "$ENVIRONMENT" ]] || usage

run() {
    if [[ "$DRY_RUN" == true ]]; then
        echo "[DRY-RUN] $*"
    else
        "$@"
    fi
}

log "Starting deployment to $ENVIRONMENT"

# Pull latest code
log "Pulling latest code..."
run git -C "$APP_DIR" pull --ff-only

# Run database migrations
log "Running migrations..."
run sudo -u myapp "$APP_DIR/bin/migrate" --env "$ENVIRONMENT"

# Restart service
log "Restarting $SERVICE_NAME..."
run sudo systemctl restart "$SERVICE_NAME"

# Wait for service to be healthy
log "Waiting for health check..."
for i in {1..30}; do
    if curl -sf "http://localhost:8080/health" > /dev/null 2>&1; then
        log "Service is healthy."
        exit 0
    fi
    sleep 2
done

die "Service did not become healthy after 60 seconds"
```

---

## Chapter 16: Advanced Scripting — Error Handling, Testing, and Best Practices

### 16.1 Robust Error Handling

```bash
#!/usr/bin/env bash
set -euo pipefail

# Trap: run cleanup function on exit (normal or error)
cleanup() {
    local exit_code=$?
    # Remove temp files regardless of how script exits
    [[ -n "${tmpfile:-}" ]] && rm -f "$tmpfile"
    [[ -n "${tmpdir:-}" ]] && rm -rf "$tmpdir"
    if [[ $exit_code -ne 0 ]]; then
        echo "[ERROR] Script failed on line $LINENO with exit code $exit_code" >&2
    fi
    exit $exit_code
}
trap cleanup EXIT

# Trap ERR for additional diagnostics
trap 'echo "[ERROR] Command failed: ${BASH_COMMAND}" >&2' ERR

# Create temp files safely
tmpfile=$(mktemp /tmp/myapp.XXXXXX)
tmpdir=$(mktemp -d /tmp/myapp_dir.XXXXXX)
```

### 16.2 Capturing Exit Codes

```bash
# Check exit code
command
status=$?
if [[ $status -ne 0 ]]; then
    echo "command failed with status $status" >&2
    exit 1
fi

# Capture output AND exit code (with set -e, you need a workaround)
output=$(command) || {
    echo "command failed" >&2
    exit 1
}

# Capture both stdout and stderr separately
stdout=$(command 2>/tmp/stderr_capture)
stderr=$(cat /tmp/stderr_capture)

# Run a command and continue even if it fails (disable -e temporarily)
set +e
result=$(risky_command)
status=$?
set -e
if [[ $status -ne 0 ]]; then
    handle_failure "$status" "$result"
fi
```

### 16.3 Logging Patterns

```bash
#!/usr/bin/env bash
set -euo pipefail

# Configurable log level
LOG_LEVEL="${LOG_LEVEL:-INFO}"    # DEBUG, INFO, WARN, ERROR

declare -A LOG_LEVELS=([DEBUG]=0 [INFO]=1 [WARN]=2 [ERROR]=3)
CURRENT_LEVEL="${LOG_LEVELS[$LOG_LEVEL]}"

log() {
    local level="$1"
    shift
    local message="$*"
    local level_num="${LOG_LEVELS[$level]:-1}"

    if (( level_num >= CURRENT_LEVEL )); then
        local timestamp
        timestamp="$(date '+%Y-%m-%dT%H:%M:%S')"
        if [[ "$level" == "ERROR" || "$level" == "WARN" ]]; then
            echo "[$timestamp] [$level] $message" >&2
        else
            echo "[$timestamp] [$level] $message"
        fi
    fi
}

log DEBUG "This is debug output"
log INFO  "Application starting"
log WARN  "Configuration uses deprecated option"
log ERROR "Database connection failed"
```

### 16.4 Idempotency

Scripts should be safe to run multiple times — running a script twice should produce the same result as running it once.

```bash
# Idempotent directory creation
mkdir -p /opt/myapp/data              # -p: no error if exists, create parents

# Idempotent file write (only write if content changed)
desired_content="server_port=8080"
actual_content=$(cat /etc/myapp/config 2>/dev/null || true)
if [[ "$actual_content" != "$desired_content" ]]; then
    echo "$desired_content" > /etc/myapp/config
    log INFO "Config updated"
else
    log DEBUG "Config unchanged"
fi

# Idempotent user creation
if ! id myapp &>/dev/null; then
    useradd -r -s /usr/sbin/nologin myapp
fi

# Idempotent package installation
if ! dpkg -l nginx 2>/dev/null | grep -q "^ii"; then
    apt-get install -y nginx
fi

# Idempotent service enablement
if ! systemctl is-enabled --quiet myapp; then
    systemctl enable myapp
fi
```

### 16.5 Parallel Execution

```bash
# Run multiple commands in parallel, collect results
pids=()

command1 &
pids+=($!)

command2 &
pids+=($!)

command3 &
pids+=($!)

# Wait for all and check exit codes
all_ok=true
for pid in "${pids[@]}"; do
    if ! wait "$pid"; then
        echo "Command with PID $pid failed" >&2
        all_ok=false
    fi
done

"$all_ok" || exit 1

# GNU parallel for processing a list
find /var/log -name "*.log" | parallel -j4 gzip {}
cat urls.txt | parallel -j8 curl -sSf {}
```

### 16.6 ShellCheck — Static Analysis

ShellCheck is the essential linter for shell scripts. It catches bugs, bad practices, and portability issues before they cause problems.

```bash
sudo apt install shellcheck

# Check a script
shellcheck myscript.sh

# Common issues ShellCheck catches:
# SC2086: Double quote to prevent globbing and word splitting
# SC2046: Quote this to prevent word splitting
# SC2006: Use $(...) notation instead of legacy backtick
# SC2164: Use 'cd ... || exit' in case cd fails
# SC2024: sudo doesn't affect redirections
```

Use ShellCheck in CI/CD pipelines:
```yaml
# In GitHub Actions / GitLab CI
- name: ShellCheck
  run: shellcheck scripts/*.sh
```

### 16.7 Testing Shell Scripts with bats-core

`bats-core` (Bash Automated Testing System) provides a framework for writing unit tests for Bash scripts.

```bash
# Install
sudo apt install bats
# or
git clone https://github.com/bats-core/bats-core.git && cd bats-core && ./install.sh /usr/local

# test/test_deploy.bats
@test "deploy fails with no arguments" {
    run ./deploy.sh
    [ "$status" -eq 1 ]
    [[ "$output" =~ "Usage:" ]]
}

@test "deploy runs with dry-run flag" {
    run ./deploy.sh --dry-run development
    [ "$status" -eq 0 ]
    [[ "$output" =~ "DRY-RUN" ]]
}

@test "validate_path rejects paths outside allowed base" {
    source ./deploy.sh   # Load functions
    run validate_path "/etc/passwd" "/var/data"
    [ "$status" -eq 1 ]
}

# Run tests
bats test/
```

### 16.8 Script Style Guide (Google Shell Style, 2026)

These rules are extracted from Google's Shell Style Guide and current community best practices:

```bash
# File naming: lowercase with underscores or dashes
deploy_app.sh
check-health.sh

# Function naming: lowercase with underscores
get_config_value() { ... }
validate_input() { ... }

# Constants: uppercase with underscores (at top of script)
readonly MAX_RETRIES=5
readonly DEFAULT_PORT=8080

# Variables: lowercase with underscores
local user_name
local config_path

# Indentation: 2 spaces (not tabs, not 4 spaces)
if [[ -f "$file" ]]; then
  process_file "$file"
fi

# Line length: 80 characters soft limit, 100 hard limit
# Break long commands with backslash continuation
long_command \
  --option1 value1 \
  --option2 value2

# Always use [[ ]] not [ ] for conditionals
# Always use $(command) not `command`
# Always quote variable expansions: "$var" not $var
# Use -- to signal end of options: rm -- "$file"
```

### 16.9 Environment and Configuration Best Practices

```bash
# Load configuration from a file safely
load_config() {
    local config_file="$1"
    [[ -f "$config_file" ]] || { echo "Config not found: $config_file" >&2; return 1; }
    # Source only if it passes a basic safety check
    if grep -qP '[;&|`$()]' "$config_file"; then
        echo "Dangerous characters in config file: $config_file" >&2
        return 1
    fi
    # shellcheck source=/dev/null
    source "$config_file"
}

# Prefer environment variables for secrets — never hardcode
database_password="${DB_PASSWORD:?DB_PASSWORD must be set}"
api_key="${API_KEY:?API_KEY must be set}"

# Configuration hierarchy (later sources override earlier)
load_config "/etc/myapp/defaults.conf" || true
load_config "/etc/myapp/config.conf"
load_config "${CONFIG_FILE:-/etc/myapp/local.conf}" || true
```

---

# PART IX — MODERN LINUX

---

## Chapter 17: Containers with Docker and Podman

### 17.1 What Containers Are

A container is a process (or group of processes) running with isolated namespaces and limited resources via cgroups. Containers share the host kernel — they are not virtual machines. This makes them lightweight (seconds to start, megabytes of overhead) compared to VMs (minutes to start, gigabytes of overhead).

The isolation is provided by:
- **Linux namespaces:** PID, mount, network, IPC, UTS (hostname), user — each container gets its own
- **cgroups:** limit CPU, memory, I/O, and network for the container
- **Layered filesystems (OverlayFS):** container filesystem is built from stacked read-only image layers plus a writable top layer

An **image** is a read-only snapshot of a filesystem and configuration. A **container** is a running instance of an image. One image can spawn many containers.

### 17.2 Docker vs Podman

**Docker** requires a background daemon (`dockerd`) running as root. Historically the standard tool. The Docker daemon has full root access, which is a security concern.

**Podman** is daemonless — containers are direct child processes of the user who starts them. This means rootless containers, better systemd integration, and no single privileged daemon. Podman is the preferred tool on RHEL/Fedora and increasingly on Ubuntu as of 2025-2026.

Both tools speak the same OCI (Open Container Initiative) image format and largely the same command interface (`podman` is often aliased to `docker`).

This chapter covers both; differences are noted.

### 17.3 Installing Docker and Podman

**Docker:**
```bash
# Official Docker installation (Ubuntu)
curl -fsSL https://get.docker.com | sh   # Convenience script
# Or manual:
sudo apt-get install -y docker-ce docker-ce-cli containerd.io docker-buildx-plugin

sudo systemctl enable --now docker
sudo usermod -aG docker $USER    # Add yourself to docker group (re-login required)
```

**Podman:**
```bash
sudo apt install podman             # Ubuntu 22.04+
podman --version

# Rootless setup
sudo usermod --add-subuids 100000-165535 $USER
sudo usermod --add-subgids 100000-165535 $USER
```

### 17.4 Running Containers

```bash
# Pull an image
docker pull nginx:latest
docker pull ubuntu:24.04
docker pull alpine:3.19            # Tiny image (~5 MB)

# Run a container
docker run nginx                   # Run in foreground (Ctrl+C to stop)
docker run -d nginx                # Detached (background)
docker run -d --name mynginx nginx # Named container
docker run -it ubuntu:24.04 bash   # Interactive terminal

# Common run options
docker run \
  -d \                             # Detached
  --name webapp \                  # Name
  -p 8080:80 \                     # Port mapping: host:container
  -v /data/web:/var/www/html \     # Volume mount: host:container
  -e DATABASE_URL="postgres://..." \ # Environment variable
  --memory 512m \                  # Memory limit
  --cpus 0.5 \                     # CPU limit (0.5 cores)
  --restart unless-stopped \       # Restart policy
  nginx:latest
```

**Port mapping:** `-p 8080:80` means host port 8080 maps to container port 80. Traffic to `localhost:8080` reaches the nginx inside the container on port 80.

### 17.5 Managing Containers

```bash
docker ps                          # Running containers
docker ps -a                       # All containers (including stopped)
docker ps --format "table {{.Names}}\t{{.Status}}\t{{.Ports}}"

docker stop mynginx                # Send SIGTERM (graceful stop)
docker kill mynginx                # Send SIGKILL (immediate)
docker start mynginx               # Start a stopped container
docker restart mynginx             # Stop then start
docker rm mynginx                  # Delete container (must be stopped)
docker rm -f mynginx               # Force delete running container

# Execute a command in a running container
docker exec mynginx nginx -t             # Run command and exit
docker exec -it mynginx bash            # Interactive shell in running container
docker exec -it mynginx sh              # sh (for Alpine/minimal images without bash)

# View logs
docker logs mynginx                      # All logs since container started
docker logs -f mynginx                   # Follow (like tail -f)
docker logs --tail 50 mynginx            # Last 50 lines
docker logs --since 1h mynginx           # Logs from last hour

# Inspect container
docker inspect mynginx                   # Full JSON details
docker inspect mynginx --format '{{.NetworkSettings.IPAddress}}'  # Container IP
docker stats                             # Live CPU/memory usage (all containers)
docker stats mynginx                     # One container
```

### 17.6 Images

```bash
docker images                      # List local images
docker image ls                    # Same
docker pull nginx:1.26             # Pull specific version
docker rmi nginx:latest            # Remove image (must have no containers using it)
docker image prune                 # Remove unused images
docker image prune -a              # Remove ALL unused images (free disk space)
docker system prune                # Remove stopped containers, unused images, networks
docker system df                   # Disk usage by Docker

# Search Docker Hub
docker search nginx
```

### 17.7 Writing a Dockerfile

A `Dockerfile` is a recipe for building a custom image:

```dockerfile
# Use a specific version — never 'latest' in production
FROM ubuntu:24.04

# Set build-time arguments
ARG APP_VERSION=1.0.0

# Set environment variables (available at runtime too)
ENV APP_ENV=production \
    PORT=8080

# Install dependencies
# Combine RUN commands to minimize layers; clean up in same layer
RUN apt-get update && \
    apt-get install -y --no-install-recommends \
        python3 python3-pip curl && \
    apt-get clean && \
    rm -rf /var/lib/apt/lists/*

# Create non-root user for running the app
RUN groupadd -r appuser && useradd -r -g appuser appuser

# Set working directory
WORKDIR /app

# Copy dependency files first (better layer caching)
COPY requirements.txt .
RUN pip3 install --no-cache-dir -r requirements.txt

# Copy application code
COPY --chown=appuser:appuser . .

# Switch to non-root user
USER appuser

# Document the port the app listens on
EXPOSE 8080

# Health check
HEALTHCHECK --interval=30s --timeout=5s --retries=3 \
    CMD curl -f http://localhost:8080/health || exit 1

# Default command
CMD ["python3", "-m", "uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8080"]
```

```bash
# Build the image
docker build -t myapp:1.0.0 .
docker build -t myapp:1.0.0 -f Dockerfile.prod .    # Custom Dockerfile name

# Multi-stage build (common pattern: separate build and runtime environments)
```

**Multi-stage build example** (keeps final image small):
```dockerfile
# Stage 1: Build
FROM golang:1.22 AS builder
WORKDIR /app
COPY go.mod go.sum ./
RUN go mod download
COPY . .
RUN CGO_ENABLED=0 GOOS=linux go build -o server .

# Stage 2: Runtime (tiny final image)
FROM alpine:3.19
RUN apk --no-cache add ca-certificates
WORKDIR /app
COPY --from=builder /app/server .
USER nobody
EXPOSE 8080
CMD ["./server"]
```

### 17.8 Volumes and Data Persistence

Container filesystems are ephemeral — data is lost when a container is removed. Use volumes for persistent data.

```bash
# Named volumes (managed by Docker/Podman)
docker volume create mydata
docker volume ls
docker volume inspect mydata
docker run -d -v mydata:/var/lib/postgresql/data postgres:16
docker volume rm mydata             # Only when no container uses it

# Bind mounts (map host directory)
docker run -d -v /opt/myapp/data:/app/data myapp    # Host path : Container path
docker run -d -v "$(pwd)":/app myapp                # Current directory

# Read-only bind mount
docker run -d -v /etc/myapp/config:/app/config:ro myapp
```

### 17.9 Networks

```bash
docker network ls                  # List networks
docker network inspect bridge      # Default network details
docker network create mynet        # Create custom bridge network
docker run -d --network mynet --name db postgres
docker run -d --network mynet --name app myapp
# On 'mynet', containers can reach each other by name: app → db on port 5432

docker network connect mynet existing_container   # Add container to network
docker network disconnect mynet container         # Remove from network
```

On a user-defined network (not the default `bridge`), containers automatically resolve each other by name via Docker's embedded DNS. This is the recommended pattern for multi-container applications.

### 17.10 Podman Quadlet — Containers as systemd Services

Quadlet (introduced in Podman 4.4, stable in 5.x) is the modern way to run containers as systemd services without writing unit files manually.

Create a `.container` file in `/etc/containers/systemd/` (system) or `~/.config/containers/systemd/` (user):

```ini
# /etc/containers/systemd/webapp.container
[Unit]
Description=Web Application
After=network-online.target

[Container]
Image=docker.io/myorg/webapp:1.0.0
PublishPort=8080:8080
Volume=/opt/webapp/data:/app/data:Z
Environment=APP_ENV=production
EnvironmentFile=/etc/webapp/env

# Auto-update: if image digest changes, pull and restart
AutoUpdate=registry

[Service]
Restart=on-failure
RestartSec=5s

[Install]
WantedBy=multi-user.target
```

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now webapp.service   # Podman auto-generated unit name
sudo systemctl status webapp.service
journalctl -u webapp.service -f
```

**Rootless with lingering:**
```bash
# For user-level containers that survive logout:
sudo loginctl enable-linger $USER
systemctl --user daemon-reload
systemctl --user enable --now webapp.service
```

### 17.11 Container Security Best Practices

```bash
# Run as non-root (always — set in Dockerfile with USER, or at runtime)
docker run --user 1000:1000 nginx

# Read-only root filesystem
docker run --read-only --tmpfs /tmp nginx

# Drop all capabilities, add only what's needed
docker run --cap-drop ALL --cap-add NET_BIND_SERVICE nginx

# No new privileges
docker run --security-opt no-new-privileges nginx

# Limit resources
docker run --memory 512m --cpus 0.5 nginx

# Scan images for vulnerabilities
sudo apt install trivy
trivy image nginx:latest            # Scan for known CVEs in image layers
trivy fs .                          # Scan local files/Dockerfile
```

---

## Chapter 18: Performance Tuning and Troubleshooting

### 18.1 Performance Methodology

Random tuning without measurement is guesswork. The **USE Method** (Utilization, Saturation, Errors) by Brendan Gregg is a systematic framework:

For every resource (CPU, memory, disk, network, …):
1. **Utilization:** What percentage of time is the resource busy? (High = potentially bottlenecked)
2. **Saturation:** Is there a queue or wait? (Nonzero = bottleneck confirmed)
3. **Errors:** Are there error events? (Any = investigate)

Always measure first, then change one thing at a time, and measure again to confirm improvement.

### 18.2 CPU Performance

```bash
# Current CPU usage
top                              # Interactive
mpstat -P ALL 1 5                # Per-core utilization, 5 samples
sar -u ALL 1 5                   # Similar to mpstat

# Load average interpretation
uptime
# load average: 1.23, 0.98, 0.75  (1-min, 5-min, 15-min)
# Compare to nproc: 'load > nproc' means saturation

nproc                            # Number of logical CPUs

# CPU frequency scaling
cat /sys/devices/system/cpu/cpu0/cpufreq/scaling_governor
# performance: maximum frequency always (best throughput)
# powersave: minimum frequency (best power)
# schedutil: kernel-managed (modern default, good balance)

# Set to performance for throughput-critical workloads:
echo performance | sudo tee /sys/devices/system/cpu/cpu*/cpufreq/scaling_governor

# Persistent with cpupower (apt install linux-tools-common)
sudo cpupower frequency-set -g performance
```

**CPU profiling:**
```bash
# perf — the Linux profiler
sudo apt install linux-perf
sudo perf stat ls                           # Count CPU events for one command
sudo perf record -g -p 1234 sleep 30       # Profile PID 1234 for 30 seconds
sudo perf report                            # Interactive report

# flamegraph (install from github.com/brendangregg/FlameGraph)
sudo perf record -F 99 -g -p 1234 sleep 30
sudo perf script | stackcollapse-perf.pl | flamegraph.pl > flame.svg
```

### 18.3 Memory Performance

```bash
# Current memory usage
free -h
# Key: MemAvailable — what's actually usable for new processes (includes reclaimable cache)
vmstat -s | head -20

# Memory pressure indicators
cat /proc/vmstat | grep pgmajfault    # Major page faults (reading from swap) — bad if high
sar -B 1 5                           # Paging statistics

# OOM killer events (killed processes when memory exhausted)
dmesg | grep -i "oom\|killed process"
journalctl -k | grep -i oom

# Per-process memory
ps aux --sort=-%mem | head -15
cat /proc/1234/status | grep VmRSS    # RSS for specific process
```

**Memory tuning:**
```bash
# Swappiness: lower = keep data in RAM longer (0-100)
sudo sysctl -w vm.swappiness=10        # Good for servers and desktops

# Dirty ratio: percentage of RAM that can hold dirty (unwritten) pages
# Lower = more frequent writes to disk, less data loss on crash
sudo sysctl -w vm.dirty_ratio=10
sudo sysctl -w vm.dirty_background_ratio=5

# Drop caches (rarely needed; Linux reclaims automatically, but useful for benchmarking)
sudo sync && sudo sysctl -w vm.drop_caches=3   # Drop pagecache, dentries, inodes
```

### 18.4 Disk I/O Performance

```bash
# Current I/O
iostat -xz 1 5
# Key columns: %util (utilization), await (average wait ms), r/s, w/s
# %util > 80% = saturated disk

iotop -ao                            # Accumulated per-process I/O
sudo iotop                           # Live per-process I/O

# Disk benchmark (use on empty/test disk only)
sudo hdparm -Tt /dev/sda             # Cached and buffered read speed
sudo fio --name=randread --ioengine=libaio --iodepth=16 \
  --rw=randread --bs=4k --direct=1 --size=1G --numjobs=4 \
  --runtime=60 --filename=/dev/sdb
```

**I/O scheduler:**
```bash
cat /sys/block/sda/queue/scheduler   # Current scheduler
# For NVMe / SSDs: 'none' or 'mq-deadline' is typically best
# For HDDs: 'bfq' or 'mq-deadline'
echo mq-deadline | sudo tee /sys/block/sda/queue/scheduler

# Persistent via udev rule:
echo 'ACTION=="add|change", KERNEL=="sd*|nvme*", ATTR{queue/scheduler}="mq-deadline"' \
  | sudo tee /etc/udev/rules.d/60-scheduler.rules
```

### 18.5 Network Performance

```bash
# TCP connection states
ss -s                                # Summary of TCP states

# TCP tuning for high-concurrency servers
# /etc/sysctl.d/99-network-tuning.conf
net.core.somaxconn = 65535           # Max connections queued for accept()
net.ipv4.tcp_max_syn_backlog = 65535 # SYN backlog
net.core.netdev_max_backlog = 65535  # Packet queue

# TCP receive/send buffer sizes
net.ipv4.tcp_rmem = 4096 87380 16777216
net.ipv4.tcp_wmem = 4096 65536 16777216

# Enable BBR congestion control (significantly better for WAN)
net.core.default_qdisc = fq
net.ipv4.tcp_congestion_control = bbr
# Verify:
sysctl net.ipv4.tcp_congestion_control   # Should show 'bbr'

sudo sysctl --system                 # Apply all sysctl.d files
```

### 18.6 Kernel Parameters and /proc/sys

`/proc/sys/` provides a live view of kernel parameters. Every file is a tunable:

```bash
cat /proc/sys/kernel/hostname         # Read
echo "newname" > /proc/sys/kernel/hostname  # Write (as root, temporary)

sysctl -a                             # All tunable parameters
sysctl kernel.hostname                # Read one
sudo sysctl -w kernel.hostname=newname # Write one (temporary)

# Permanent: /etc/sysctl.d/99-custom.conf
# kernel.hostname = newname
# Apply without rebooting:
sudo sysctl --system
```

### 18.7 Troubleshooting Methodology

When something is broken, apply a systematic approach rather than random changes:

1. **Define the problem precisely.** "It's slow" is not a problem statement. "The API endpoint `/search` returns in >2s, up from 100ms yesterday" is.
2. **When did it start?** Changes, deployments, updates around that time are prime suspects.
3. **Gather data.** Check logs, metrics, and resource usage before changing anything.
4. **Form a hypothesis.** Identify the most likely cause.
5. **Test the hypothesis.** Make one change; measure whether it fixes the problem.
6. **Document.** Record what you tried and what worked.

### 18.8 Diagnostic Toolkit

```bash
# "The system is slow" checklist
uptime                               # Load average
free -h                              # Memory pressure
iostat -xz 1 3                       # I/O utilization
ss -s                                # Socket state summary
journalctl -p err -b                 # Errors this boot
dmesg | tail -50                     # Recent kernel messages

# Process troubleshooting
strace -p 1234                       # System calls made by process
strace -c command                    # Summary of syscalls and time
ltrace command                       # Library calls
lsof -p 1234                         # Open files/sockets for process

# Network troubleshooting
tcpdump -i eth0 -nn port 80          # Capture HTTP traffic
tcpdump -i eth0 -nn -w /tmp/capture.pcap  # Write to file for Wireshark
ss -tlnp                             # Check who's listening where
ss -ta state established dst :5432  # Established connections to Postgres

# Disk troubleshooting
dmesg | grep -i "error\|fail\|ata\|scsi"  # Disk errors in kernel log
sudo smartctl -H /dev/sda            # SMART health status
sudo badblocks -n /dev/sdb1          # Non-destructive read-write test (SLOW)
```

### 18.9 Common Problems and Solutions

**"Connection refused" to a service:**
```bash
systemctl status myservice           # Is it running?
ss -tlnp | grep 8080                 # Is it listening on the right port?
ufw status                           # Is the firewall blocking it?
journalctl -u myservice -n 50        # Any startup errors?
```

**"Disk is full":**
```bash
df -h                                # Which filesystem?
du -sh /* 2>/dev/null | sort -rh | head -20   # Largest directories
journalctl --disk-usage              # Journal consuming space?
sudo journalctl --vacuum-size=500M   # Trim journal
find /var/log -name "*.log" -size +100M       # Large log files
```

**"Out of memory" / OOM kills:**
```bash
journalctl -k | grep -i oom          # OOM killer log entries
free -h                              # Current memory state
ps aux --sort=-%mem | head -15       # Biggest memory users
# Solutions: add swap, reduce service memory usage, add RAM
sudo sysctl -w vm.swappiness=10      # Use swap more aggressively
```

**"Too many open files":**
```bash
# Error: "Too many open files" (EMFILE or ENFILE)
ulimit -n                            # Current per-process limit
cat /proc/sys/fs/file-max            # System-wide limit
cat /proc/sys/fs/file-nr             # Current: open, unused, max
lsof -p 1234 | wc -l                 # How many files does process have open?
# Solution: increase LimitNOFILE in service unit file or /etc/security/limits.conf
```

**High CPU usage — finding the cause:**
```bash
top                                  # Identify PID
ps aux --sort=-%cpu | head -10
sudo perf top                        # Real-time function-level profiling
sudo strace -p <PID> -c sleep 10    # What syscalls is it making?
```

### 18.10 The /proc Filesystem as a Diagnostic Tool

```bash
# Process memory map (what's loaded where)
cat /proc/1234/maps

# File descriptors (what files/sockets are open)
ls -la /proc/1234/fd/
ls -la /proc/1234/fd/ | grep socket   # Open sockets

# Command line and environment
tr '\0' ' ' < /proc/1234/cmdline     # Command line
tr '\0' '\n' < /proc/1234/environ    # Environment variables

# Kernel state
cat /proc/loadavg                    # Load averages + task count
cat /proc/net/dev                    # Network interface statistics
cat /proc/net/tcp                    # TCP socket table (raw hex)
cat /proc/interrupts                 # IRQ counts per CPU (useful for NIC tuning)
cat /proc/slabinfo                   # Kernel slab allocator (memory debugging)
```

### 18.11 Performance Tuning with tuned

`tuned` is a daemon that applies predefined performance profiles:

```bash
sudo apt install tuned
sudo systemctl enable --now tuned

tuned-adm list                       # Available profiles
tuned-adm active                     # Current profile
sudo tuned-adm profile throughput-performance  # For throughput-focused servers
sudo tuned-adm profile latency-performance    # For low-latency workloads
sudo tuned-adm profile virtual-guest          # For VMs
sudo tuned-adm recommend                       # Let tuned recommend a profile
```

---

## Appendix A: Essential Command Quick Reference

```bash
# Navigation
pwd / cd / ls -lah / tree -L 2

# Files
cp -a / mv / rm -- / touch / mkdir -p / find / locate / stat

# Text
cat / less / head / tail -f / grep -rn / sed -i / awk -F: '{print $1}' / cut / sort / uniq -c / wc -l

# Permissions
chmod 755 / chown user:group / getfacl / setfacl / umask

# Processes
ps aux / top / htop / kill -TERM / pkill / pgrep / nice / renice / nohup / tmux

# Disk
df -h / du -sh / lsblk / blkid / fdisk / mkfs.ext4 / mount / umount / fsck

# Network
ip addr / ip route / ss -tlnp / ping / traceroute / dig / curl / ssh / scp / rsync

# Services
systemctl status/start/stop/restart/enable/disable / journalctl -u / -f / -b / -p err

# Packages
apt update && apt upgrade / apt install / apt remove --purge / dpkg -l / dpkg -L

# Security
ufw status / ufw allow / fail2ban-client status / ssh-keygen / ssh-copy-id / sudo -l

# Performance
top / htop / vmstat 1 / iostat -xz 1 / sar / perf stat / strace -c
```

---

## Appendix B: Glossary

| Term | Definition |
|------|-----------|
| **ACL** | Access Control List — per-user/group permissions beyond owner/group/others |
| **AppArmor** | Mandatory Access Control system used by default on Ubuntu |
| **Btrfs** | B-tree filesystem with copy-on-write, snapshots, and RAID |
| **cgroups** | Control Groups — kernel mechanism for limiting process resources |
| **DHCP** | Dynamic Host Configuration Protocol — automatic IP address assignment |
| **EFI/UEFI** | Modern firmware replacing BIOS; required for GPT and Secure Boot |
| **ext4** | Fourth extended filesystem — the most widely used Linux filesystem |
| **FQDN** | Fully Qualified Domain Name — complete hostname including domain |
| **GPT** | GUID Partition Table — modern disk partitioning standard |
| **GRUB** | GNU GRand Unified Bootloader — loads the Linux kernel at startup |
| **inode** | Kernel data structure holding file metadata (not the name) |
| **LVM** | Logical Volume Manager — flexible disk abstraction layer |
| **MAC** | Mandatory Access Control — system-wide policy overriding DAC |
| **MBR** | Master Boot Record — legacy disk partitioning and bootloader scheme |
| **namespace** | Kernel isolation primitive; basis for containers |
| **NTP** | Network Time Protocol — synchronizes system clocks |
| **OOM** | Out of Memory — when the kernel kills processes to free RAM |
| **PID** | Process ID — unique identifier for a running process |
| **POSIX** | Portable Operating System Interface — standard defining Unix-like APIs |
| **RAID** | Redundant Array of Independent Disks — data redundancy/performance |
| **SELinux** | Security-Enhanced Linux — MAC system used by RHEL/Fedora |
| **SUID** | Set User ID — file permission bit causing execution as the file's owner |
| **syscall** | System call — the only interface between user programs and the kernel |
| **systemd** | Modern init system and service manager; PID 1 on most distributions |
| **TTY** | Teletypewriter — a terminal device; `/dev/tty` in Linux |
| **UID/GID** | User/Group ID — numeric identifiers for users and groups |
| **VFS** | Virtual Filesystem Switch — kernel abstraction making all filesystems look the same |

---

*Linux: The Complete Guide — Edition 2026*
*Written for Icebreaker. Reference the companion `linux_cabp.md` for safety-critical agentic execution patterns.*
