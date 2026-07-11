# Icebreaker ISO Rebuild Instructions (July 2, 2026)

**Target:** GCC VM
**Objective:** Build the final uncompressed Icebreaker ISO with the Privileged Brain (PB) loaded and full GNOME desktop environment.

> **Note to AI Agent:** The codebase on the `iterative-build-v1` branch has already been patched locally to disable `icebreaker-qbd.service` (loading only PB) and to remove `mksquashfs` compression. Your job is strictly to execute the build on the remote GCC VM and retrieve the final ISO.

## Step 1: Connect to the GCC VM
Connect to the already-running GCC VM using the `gcloud` CLI.
VM Instance Name: `icebreaker-pharoject-12486d7e-4046-45bd-8b4`

```bash
gcloud compute ssh icebreaker-pharoject-12486d7e-4046-45bd-8b4
```

## Step 2: Navigate to the Codebase
The code from the `iterative-build-v1` branch has already been pushed to origin and rsynced to the VM.

```bash
cd /home/aditya/icebreaker/cx-distro
```

## Step 3: Run the Build (No Errors Allowed)

**CRITICAL BUILD RULES:**
1. **Full Desktop Version:** You MUST pass `--profile=desktop`. The stripped-down `vm` profile does not contain `gnome-terminal` and will crash the AI terminal on boot.
2. **Cargo PATH & sudo:** The `.build/` dir has cached `mcpd` and `llama-server` binaries. However, `cargo` is not in the PATH under sudo. 
   **You MUST do ONE of the following:**
   - Either use `--skip-to=2` (if `mcpd` exists)
   - Or set `PATH=/home/aditya/.cargo/bin:$PATH` before calling `sudo -E bash build.sh`

Run the build using this command:
```bash
sudo env PATH="/home/aditya/.cargo/bin:$PATH" bash build.sh --profile=desktop --force
```

## Step 4: Retrieve the Final ISO (v5)
The build process will produce a heavily uncompressed ISO file at `/home/aditya/icebreaker/cx-distro/icebreaker.iso`. 
It must be downloaded back to the macOS host into the `ISO` directory and named as **v5**.

1. Exit the SSH session and run the download command from the local Mac terminal:
```bash
gcloud compute scp icebreaker-pharoject-12486d7e-4046-45bd-8b4:/home/aditya/icebreaker/cx-distro/icebreaker.iso /Users/aditya/Documents/Icebreaker/ISO/icebreaker_v5_full.iso
```

2. **Final Verification:** Ensure the file successfully landed exactly at:
`/Users/aditya/Documents/Icebreaker/ISO/icebreaker_v5_full.iso`
Do not leave the ISO in a cache or temp directory. The user relies on this exact output path.
