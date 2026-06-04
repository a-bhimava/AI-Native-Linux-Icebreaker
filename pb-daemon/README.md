# Privileged Brain — Deep OS Integration

AI system daemon + shell hooks + custom Linux distro.

## Quick install (on any Ubuntu/Debian machine)

```bash
# 1. Build the .deb
bash build_deb.sh

# 2. Install it
sudo dpkg -i privileged-brain-daemon_1.0_amd64.deb
sudo apt-get install -f     # install any missing deps

# 3. Open a new terminal — shell integration is active
ai "list all listening TCP ports"
```

## What you get

| Component | What it does |
|-----------|-------------|
| `pb-daemon` | Watches journald + /proc, calls AI on anomalies |
| `pb-ebpf` | Kernel-level eBPF probes (OOM, crashes) |
| `pb-ask` | CLI to query the model from any script/shell |
| Shell hooks | `ai` command, Ctrl+G, command_not_found |
| systemd units | Auto-start on boot, OnFailure hooks |

## Usage

```bash
# Ask anything
ai "why is my disk full"
ai "show top memory-consuming processes"

# After a failed command, press Ctrl+G → AI explains the error

# Command not found → AI suggests the right package
foobar   # → "Did you mean: foobaz? Install with: apt install ..."

# Explain last failure
pb-why

# Start/stop daemon
sudo systemctl start  pb-daemon
sudo systemctl stop   pb-daemon
sudo systemctl status pb-daemon

# Live AI daemon log
sudo journalctl -u pb-daemon -f

# eBPF kernel probe (run as root, requires bpfcc)
sudo pb-ebpf
```

## Build the full custom ISO

```bash
# On Ubuntu 22.04 (GCP VM works perfectly):
sudo bash distro/build_iso.sh
# → privileged-brain-os-1.0-amd64.iso
```

## Architecture

```
Kernel (eBPF probes) → pb-daemon → Ollama API → privileged-brain model
                              ↑
                     journald / /proc / Unix socket (pb-ask)
                              ↓
                     Shell (ai command, Ctrl+G, command_not_found)
```
