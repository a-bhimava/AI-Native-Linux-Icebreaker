#!/usr/bin/env bash
# deploy.sh — Deploy the Icebreaker stack directly to the GCP VM.
#
# Unlike rebuild.sh (which builds an ISO), this script installs the full
# Icebreaker stack natively on the VM: XFCE desktop, XRDP remote access,
# all binaries, systemd services, models, and GUI apps.
#
# Usage:
#   bash deploy.sh [OPTIONS]
#
# Options:
#   --skip-build        Skip binary compilation (Stages 1-3); re-deploy only
#   --skip-desktop      Skip XFCE/XRDP installation
#   --gemini-key=KEY    Write Gemini API key to locations.env on the VM
#
# First deploy: ~15 min (packages + build + copy models).
# Subsequent:   ~3 min  (rebuild + redeploy + restart).
#
# To connect after deploy:
#   1. SSH tunnel:  gcloud compute ssh VM -- -L 3389:localhost:3389
#   2. RDP client:  connect to localhost:3389 (user: icebreaker / pass: icebreaker)
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/../.." && pwd)"
CX_DIR="${REPO_ROOT}/cx-distro"

# ── GCP VM config ──────────────────────────────────────────────────────
VM_NAME="${VM_NAME:-icebreaker-phase2-vm}"
VM_ZONE="${VM_ZONE:-us-west4-b}"
VM_PROJECT="${VM_PROJECT:-project-12486d7e-4046-45bd-8b4}"

REPO_DIR_ON_VM="~/icebreaker"

# ── Parse args ─────────────────────────────────────────────────────────
SKIP_BUILD=0
SKIP_DESKTOP=0
GEMINI_KEY=""

for arg in "$@"; do
    case "$arg" in
        --skip-build)   SKIP_BUILD=1 ;;
        --skip-desktop) SKIP_DESKTOP=1 ;;
        --gemini-key=*) GEMINI_KEY="${arg#--gemini-key=}" ;;
        --help|-h)
            head -22 "${BASH_SOURCE[0]}" | grep '^#' | sed 's/^# \?//'
            exit 0
            ;;
        *)
            echo "Unknown arg: $arg" >&2
            exit 1
            ;;
    esac
done

SSH_CMD="gcloud compute ssh ${VM_NAME} --zone=${VM_ZONE} --project=${VM_PROJECT} --ssh-flag=-oServerAliveInterval=30 --ssh-flag=-oServerAliveCountMax=10"
SCP_CMD="gcloud compute scp --zone=${VM_ZONE} --project=${VM_PROJECT}"

echo "=== Icebreaker VM Deploy ==="
echo "VM:   ${VM_NAME} (${VM_ZONE})"
echo "Skip build:   $([ $SKIP_BUILD -eq 1 ] && echo yes || echo no)"
echo "Skip desktop: $([ $SKIP_DESKTOP -eq 1 ] && echo yes || echo no)"
echo ""

# ── Step 1: Start VM if stopped ───────────────────────────────────────
echo "[1/12] Checking VM status..."
VM_STATUS=$(gcloud compute instances describe "${VM_NAME}" \
    --zone="${VM_ZONE}" --project="${VM_PROJECT}" \
    --format="get(status)" 2>/dev/null || echo "UNKNOWN")

if [ "$VM_STATUS" = "TERMINATED" ] || [ "$VM_STATUS" = "STOPPED" ]; then
    echo "  VM is ${VM_STATUS}, starting..."
    gcloud compute instances start "${VM_NAME}" \
        --zone="${VM_ZONE}" --project="${VM_PROJECT}"
    echo "  Waiting 30s for boot..."
    sleep 30
elif [ "$VM_STATUS" = "RUNNING" ]; then
    echo "  VM is already running."
else
    echo "  VM status: ${VM_STATUS}" >&2
    exit 1
fi

# ── Step 2: Wait for SSH ──────────────────────────────────────────────
echo "[2/12] Waiting for SSH..."
for i in $(seq 1 30); do
    if ${SSH_CMD} --command="echo ok" 2>/dev/null | grep -q ok; then
        echo "  SSH ready."
        break
    fi
    if [ "$i" -eq 30 ]; then
        echo "  SSH not available after 30 attempts." >&2
        exit 1
    fi
    sleep 5
done

# ── Step 3: Deploy repo via tarball ───────────────────────────────────
echo "[3/12] Deploying repo to VM..."
TARBALL="/tmp/icebreaker-repo.tar.gz"
git -C "${REPO_ROOT}" archive --format=tar.gz HEAD > "${TARBALL}"
TARBALL_SIZE=$(du -h "${TARBALL}" | awk '{print $1}')
echo "  Tarball: ${TARBALL_SIZE}"

${SCP_CMD} "${TARBALL}" "${VM_NAME}:/tmp/icebreaker-repo.tar.gz" 2>/dev/null

${SSH_CMD} --command="
    mkdir -p ${REPO_DIR_ON_VM}
    cd ${REPO_DIR_ON_VM}
    tar xzf /tmp/icebreaker-repo.tar.gz
    rm /tmp/icebreaker-repo.tar.gz
    echo '  Files: '\$(find . -type f | wc -l)
" 2>/dev/null
rm -f "${TARBALL}"

# ── Step 4: Link model files ─────────────────────────────────────────
echo "[4/12] Linking model files into repo tree..."
${SSH_CMD} --command="
    mkdir -p ${REPO_DIR_ON_VM}/models
    for gguf in ~/models/*.gguf; do
        [ -f \"\$gguf\" ] || continue
        BASENAME=\$(basename \"\$gguf\")
        TARGET=\"${REPO_DIR_ON_VM}/models/\${BASENAME}\"
        if [ ! -f \"\$TARGET\" ]; then
            ln -sf \"\$gguf\" \"\$TARGET\"
            echo \"  Linked: \${BASENAME}\"
        else
            echo \"  Already present: \${BASENAME}\"
        fi
    done
    if [ ! -f ${REPO_DIR_ON_VM}/models/checksums.sha256 ]; then
        cd ${REPO_DIR_ON_VM}/models
        sha256sum *.gguf > checksums.sha256 2>/dev/null || true
    fi
" 2>/dev/null

# ── Step 5: Install build dependencies ────────────────────────────────
echo "[5/12] Ensuring build dependencies..."
${SSH_CMD} --command="
    if ! command -v cmake >/dev/null 2>&1; then
        echo '  Installing build deps...'
        sudo apt-get update -qq
        sudo apt-get install -y -qq \
            build-essential cmake git python3-venv python3-pip \
            2>&1 | tail -3
    else
        echo '  Build deps already installed.'
    fi
" 2>/dev/null

# ── Step 6: Install Rust toolchain ────────────────────────────────────
echo "[6/12] Ensuring Rust toolchain..."
${SSH_CMD} --command="
    if [ -f ~/.cargo/env ]; then source ~/.cargo/env; fi
    if command -v rustup >/dev/null 2>&1; then
        echo '  Rust already installed:' \$(rustc --version)
    else
        echo '  Installing Rust...'
        curl --proto '=https' --tlsv1.2 -sSf https://sh.rustup.rs | sh -s -- -y --default-toolchain stable --profile minimal
        source ~/.cargo/env
        rustc --version
    fi
" 2>/dev/null

# ── Step 7: Install desktop + remote access ───────────────────────────
if [ "$SKIP_DESKTOP" -eq 0 ]; then
    echo "[7/12] Installing desktop + XRDP..."
    ${SSH_CMD} --command="
        SENTINEL=/var/lib/icebreaker/.desktop-installed
        if [ -f \"\$SENTINEL\" ]; then
            echo '  Desktop already installed (sentinel exists). Skipping.'
        else
            echo '  Installing XFCE + XRDP (this takes a few minutes)...'
            sudo apt-get update -qq

            # XFCE desktop (same packages as ISO vm profile).
            sudo apt-get install -y --no-install-recommends \
                xfce4 xfce4-terminal lightdm lightdm-gtk-greeter \
                thunar mousepad zenity dbus-x11 \
                2>&1 | tail -5

            # GTK4 + LibAdwaita for Icebreaker GUI apps.
            sudo apt-get install -y --no-install-recommends \
                python3-gi gir1.2-gtk-4.0 gir1.2-adw-1 \
                libadwaita-1-0 adwaita-icon-theme \
                2>&1 | tail -3

            # XRDP for remote desktop.
            sudo apt-get install -y --no-install-recommends xrdp \
                2>&1 | tail -3

            # ── Create icebreaker user ────────────────────────────────
            if ! id icebreaker >/dev/null 2>&1; then
                echo '  Creating icebreaker user...'
                sudo groupadd -rf autologin
                sudo useradd -m -s /bin/bash -G sudo,autologin icebreaker
                echo 'icebreaker:icebreaker' | sudo chpasswd
                echo 'icebreaker ALL=(ALL) NOPASSWD:ALL' | sudo tee /etc/sudoers.d/icebreaker >/dev/null
            fi

            # ── Configure LightDM auto-login ──────────────────────────
            sudo mkdir -p /etc/lightdm/lightdm.conf.d
            sudo tee /etc/lightdm/lightdm.conf.d/50-autologin.conf >/dev/null <<'LDMCFG'
[Seat:*]
autologin-user=icebreaker
autologin-user-timeout=0
user-session=xfce
greeter-session=lightdm-gtk-greeter
LDMCFG

            # ── Configure XRDP for XFCE ──────────────────────────────
            # XRDP session should start XFCE.
            sudo tee /home/icebreaker/.xsession >/dev/null <<'XSESS'
#!/bin/sh
exec startxfce4
XSESS
            sudo chown icebreaker:icebreaker /home/icebreaker/.xsession
            sudo chmod 755 /home/icebreaker/.xsession

            # Add xrdp user to ssl-cert group for TLS.
            sudo usermod -aG ssl-cert xrdp 2>/dev/null || true

            # Enable XRDP (LightDM not needed — no physical display on GCP).
            sudo systemctl enable xrdp 2>/dev/null || true

            # Write sentinel.
            sudo mkdir -p /var/lib/icebreaker
            sudo touch \"\$SENTINEL\"
            echo '  Desktop + XRDP installation complete.'
        fi
    " 2>/dev/null
else
    echo "[7/12] Skipping desktop installation (--skip-desktop)."
fi

# ── Step 8: Build binaries ────────────────────────────────────────────
if [ "$SKIP_BUILD" -eq 0 ]; then
    echo "[8/12] Building binaries on VM..."
    ${SSH_CMD} --command="
        cd ${REPO_DIR_ON_VM}
        if [ -f ~/.cargo/env ]; then source ~/.cargo/env; fi
        BUILD_DIR=${REPO_DIR_ON_VM}/cx-distro/.build

        # Fix ownership if previous sudo build.sh left root-owned files.
        for d in \"\$BUILD_DIR\" \
                 ${REPO_DIR_ON_VM}/src/mcpd/target \
                 ${REPO_DIR_ON_VM}/dual-brain/build \
                 ${REPO_DIR_ON_VM}/dual-brain/icebreaker_controller.egg-info; do
            if [ -d \"\$d\" ]; then
                sudo chown -R \$(whoami):\$(whoami) \"\$d\"
            fi
        done
        mkdir -p \"\$BUILD_DIR\"

        set -e

        # ── Stage 1: mcpd ─────────────────────────────────────────────
        echo '  [Stage 1] Building mcpd...'
        cd ${REPO_DIR_ON_VM}/src/mcpd
        cargo build --release 2>&1 | tail -3
        MCPD_BIN=target/release/mcpd
        [ -f \"\$MCPD_BIN\" ] || { echo 'FATAL: mcpd binary not found' >&2; exit 1; }
        if strings \"\$MCPD_BIN\" | grep -q MCPD_FS_TEST_ROOTS; then
            echo 'FATAL: mcpd compiled with test-only features' >&2
            exit 1
        fi
        cp \"\$MCPD_BIN\" \"\$BUILD_DIR/mcpd\"
        echo \"  mcpd OK (\$(du -h \"\$BUILD_DIR/mcpd\" | awk '{print \$1}'))\"

        # ── Stage 2: llama-server ─────────────────────────────────────
        echo '  [Stage 2] Building llama-server...'
        LLAMA_COMMIT=\$(cat ${REPO_DIR_ON_VM}/cx-distro/LLAMA_CPP_COMMIT | tr -d '[:space:]')
        LLAMA_SRC=\"\$BUILD_DIR/llama.cpp\"

        if [ -d \"\${LLAMA_SRC}/.git\" ]; then
            cd \"\$LLAMA_SRC\" && git fetch origin
        else
            rm -rf \"\$LLAMA_SRC\"
            git clone --filter=blob:none https://github.com/ggerganov/llama.cpp \"\$LLAMA_SRC\"
            cd \"\$LLAMA_SRC\"
        fi
        git checkout \"\$LLAMA_COMMIT\" 2>/dev/null

        # Only clean if the commit changed (avoids 8-min full recompile).
        if [ -f build/.llama_commit ] && [ \"\$(cat build/.llama_commit)\" = \"\$LLAMA_COMMIT\" ]; then
            echo '    Reusing existing build (commit unchanged).'
        else
            rm -rf build
        fi

        cmake -B build \
            -DGGML_CUDA=OFF -DGGML_METAL=OFF \
            -DCMAKE_BUILD_TYPE=Release \
            -DLLAMA_BUILD_TESTS=OFF -DLLAMA_BUILD_EXAMPLES=OFF \
            -DLLAMA_BUILD_SERVER=ON \
            2>&1 | tail -3
        cmake --build build --target llama-server -j\$(nproc) 2>&1 | tail -5
        LLAMA_BIN=build/bin/llama-server
        [ -f \"\$LLAMA_BIN\" ] || { echo 'FATAL: llama-server binary not found' >&2; exit 1; }
        echo \"\$LLAMA_COMMIT\" > build/.llama_commit
        cp \"\$LLAMA_BIN\" \"\$BUILD_DIR/llama-server\"
        echo \"  llama-server OK (\$(du -h \"\$BUILD_DIR/llama-server\" | awk '{print \$1}'))\"

        # ── Stage 3: Python venv ──────────────────────────────────────
        echo '  [Stage 3] Building Python venv...'
        VENV_DIR=\"\$BUILD_DIR/venv\"
        rm -rf \"\$VENV_DIR\"
        python3 -m venv --system-site-packages \"\$VENV_DIR\"
        \"\$VENV_DIR/bin/pip\" install --no-cache-dir ${REPO_DIR_ON_VM}/dual-brain/ 2>&1 | tail -5
        # Copy data files into the installed package — pip doesn't ship them.
        CONTROLLER_PKG=\$(find \"\$VENV_DIR\" -path '*/site-packages/controller/model_registry.py' -exec dirname {} \; | head -1)
        if [ -n \"\$CONTROLLER_PKG\" ]; then
            cp ${REPO_DIR_ON_VM}/dual-brain/controller/catalogue.toml \"\$CONTROLLER_PKG/catalogue.toml\"
            cp -a ${REPO_DIR_ON_VM}/dual-brain/controller/schemas  \"\$CONTROLLER_PKG/schemas\"
            cp -a ${REPO_DIR_ON_VM}/dual-brain/controller/prompts  \"\$CONTROLLER_PKG/prompts\"
            cp -a ${REPO_DIR_ON_VM}/dual-brain/controller/grammars \"\$CONTROLLER_PKG/grammars\"
        fi

        \"\$VENV_DIR/bin/python3\" -m controller --help >/dev/null 2>&1 || {
            echo 'FATAL: venv broken' >&2; exit 1
        }
        echo \"  venv OK (\$(du -sh \"\$VENV_DIR\" | awk '{print \$1}'))\"
    " 2>/dev/null
else
    echo "[8/12] Skipping build (--skip-build)."
fi

# ── Step 9: Deploy artifacts ─────────────────────────────────────────
# Split into sub-steps to avoid SSH timeout on long commands.
echo "[9/12] Deploying artifacts to system paths..."

# 9a: Config, systemd units, CLI
${SSH_CMD} --command="
    REPO=${REPO_DIR_ON_VM}
    CX=\${REPO}/cx-distro

    sudo install -Dm644 \"\${CX}/distro/controller.toml\" /etc/icebreaker/controller.toml
    sudo install -Dm644 \"\${CX}/distro/locations.env\"   /etc/icebreaker/locations.env

    for unit in \${REPO}/dual-brain/controller/systemd/*.service \
                \${REPO}/dual-brain/controller/systemd/*.socket; do
        [ -f \"\$unit\" ] || continue
        sudo install -Dm644 \"\$unit\" \"/etc/systemd/system/\$(basename \"\$unit\")\"
    done

    sudo install -Dm644 \"\${REPO}/dual-brain/controller/systemd/icebreaker.sysusers.d.conf\" \
        /etc/sysusers.d/icebreaker.conf
    sudo install -Dm644 \"\${REPO}/dual-brain/controller/systemd/icebreaker.tmpfiles.d.conf\" \
        /etc/tmpfiles.d/icebreaker.conf

    sudo install -Dm755 \"\${CX}/distro/icebreaker-cli\" /usr/bin/icebreaker
    echo '  Config + systemd + CLI installed.'
" 2>/dev/null

# 9b: Binaries and shared libs
${SSH_CMD} --command="
    REPO=${REPO_DIR_ON_VM}
    BUILD=\${REPO}/cx-distro/.build
    CX=\${REPO}/cx-distro

    sudo install -Dm755 \"\${BUILD}/mcpd\"         /usr/libexec/icebreaker/mcpd
    sudo install -Dm755 \"\${BUILD}/llama-server\"  /usr/libexec/icebreaker/llama-server
    for so in \${BUILD}/llama.cpp/build/bin/lib*.so*; do
        [ -f \"\$so\" ] || [ -L \"\$so\" ] || continue
        sudo cp -a \"\$so\" /usr/libexec/icebreaker/
    done
    echo '/usr/libexec/icebreaker' | sudo tee /etc/ld.so.conf.d/icebreaker.conf >/dev/null
    sudo ldconfig
    echo '  Installed llama-server + shared libs.'

    sudo install -Dm755 \"\${REPO}/dual-brain/scripts/start-pbd\" /usr/libexec/icebreaker/start-pbd
    sudo install -Dm755 \"\${REPO}/dual-brain/scripts/start-qbd\" /usr/libexec/icebreaker/start-qbd
    sudo install -Dm755 \"\${CX}/distro/first-boot\"       /usr/libexec/icebreaker/first-boot
    sudo install -Dm755 \"\${CX}/distro/safe-mode\"         /usr/libexec/icebreaker/safe-mode
    sudo install -Dm755 \"\${CX}/distro/wait-for-sockets\"  /usr/libexec/icebreaker/wait-for-sockets
    echo '  Binaries + scripts installed.'
" 2>/dev/null

# 9c: Shared data (schemas, prompts, grammars)
${SSH_CMD} --command="
    REPO=${REPO_DIR_ON_VM}

    sudo install -Dm644 \"\${REPO}/dual-brain/controller/catalogue.toml\" \
        /usr/share/icebreaker/catalogue.toml
    sudo install -Dm644 \"\${REPO}/dual-brain/controller/grammars/qb_intent.gbnf\" \
        /usr/share/icebreaker/grammars/qb_intent.gbnf

    for schema in \${REPO}/dual-brain/controller/schemas/*.json \
                  \${REPO}/src/mcpd/schemas/*.json; do
        [ -f \"\$schema\" ] || continue
        sudo install -Dm644 \"\$schema\" \
            \"/usr/share/icebreaker/schemas/\$(basename \"\$schema\")\"
    done

    for prompt in \${REPO}/dual-brain/controller/prompts/*.txt; do
        [ -f \"\$prompt\" ] || continue
        sudo install -Dm644 \"\$prompt\" \
            \"/usr/share/icebreaker/prompts/\$(basename \"\$prompt\")\"
    done
    echo '  Shared data installed.'
" 2>/dev/null

# 9d: Python venv (large — separate SSH to avoid timeout)
${SSH_CMD} --command="
    BUILD=${REPO_DIR_ON_VM}/cx-distro/.build
    sudo rm -rf /opt/icebreaker/venv
    sudo mkdir -p /opt/icebreaker
    sudo cp -a \"\${BUILD}/venv\" /opt/icebreaker/venv
    echo '  Venv deployed.'
" 2>/dev/null

# 9e: Model files (largest — checksums skip unchanged files)
${SSH_CMD} --command="
    REPO=${REPO_DIR_ON_VM}
    sudo mkdir -p /var/lib/icebreaker/models
    sudo install -Dm644 \"\${REPO}/models/checksums.sha256\" \
        /var/lib/icebreaker/models/checksums.sha256

    for gguf in \${REPO}/models/*.gguf; do
        [ -f \"\$gguf\" ] || continue
        BASENAME=\$(basename \"\$gguf\")
        DEST=\"/var/lib/icebreaker/models/\${BASENAME}\"
        if [ -f \"\$DEST\" ]; then
            EXISTING=\$(sha256sum \"\$DEST\" | awk '{print \$1}')
            SOURCE=\$(sha256sum \"\$gguf\" | awk '{print \$1}')
            if [ \"\$EXISTING\" = \"\$SOURCE\" ]; then
                echo \"  Model \${BASENAME}: unchanged, skipping copy.\"
                continue
            fi
        fi
        echo \"  Copying model: \${BASENAME} (\$(du -h \"\$gguf\" | awk '{print \$1}'))...\"
        sudo cp \"\$gguf\" \"\$DEST\"
        sudo chmod 644 \"\$DEST\"
    done

    # model_registry resolves checksums.sha256 relative to the installed
    # package (__file__/../../models/). Symlink models + checksums there
    # so resolve_to_file() works with the distro venv paths.
    REG_MODELS_DIR=\$(dirname \$(/opt/icebreaker/venv/bin/python3 -c \
        'from pathlib import Path; import controller.model_registry as m; print(Path(m.__file__).parent.parent.parent / \"models\")'))
    REG_MODELS_DIR=\${REG_MODELS_DIR}/models
    sudo mkdir -p \"\${REG_MODELS_DIR}\"
    sudo cp /var/lib/icebreaker/models/checksums.sha256 \"\${REG_MODELS_DIR}/checksums.sha256\"
    for gguf in /var/lib/icebreaker/models/*.gguf; do
        [ -f \"\$gguf\" ] || continue
        sudo ln -sf \"\$gguf\" \"\${REG_MODELS_DIR}/\$(basename \"\$gguf\")\"
    done
    echo '  Models deployed.'
" 2>/dev/null

# 9f: Desktop files + wallpaper
${SSH_CMD} --command="
    CX=${REPO_DIR_ON_VM}/cx-distro

    for desktop_file in \${CX}/distro/*.desktop; do
        [ -f \"\$desktop_file\" ] || continue
        sudo install -Dm644 \"\$desktop_file\" \
            \"/usr/share/applications/\$(basename \"\$desktop_file\")\"
    done

    sudo install -Dm644 \"\${CX}/distro/icebreaker-wallpaper.png\" \
        /usr/share/backgrounds/icebreaker-wallpaper.png
    sudo install -Dm644 \"\${CX}/distro/99_icebreaker.gschema.override\" \
        /usr/share/glib-2.0/schemas/99_icebreaker.gschema.override

    # XFCE xfconf defaults
    for xfconf_file in xfce4-desktop.xml xfce4-panel.xml xsettings.xml; do
        if [ -f \"\${CX}/distro/\${xfconf_file}\" ]; then
            sudo install -Dm644 \"\${CX}/distro/\${xfconf_file}\" \
                \"/etc/xdg/xfce4/xfconf/xfce-perchannel-xml/\${xfconf_file}\"
        fi
    done

    # Chatbot autostart
    if [ -f \"\${CX}/distro/icebreaker-chatbot-autostart.desktop\" ]; then
        sudo install -Dm644 \"\${CX}/distro/icebreaker-chatbot-autostart.desktop\" \
            /etc/xdg/autostart/icebreaker-chatbot.desktop
    fi

    echo '  Desktop files + wallpaper + XFCE configs installed.'
" 2>/dev/null

# ── Step 10: System setup ─────────────────────────────────────────────
echo "[10/12] System setup..."
GEMINI_SETUP=""
if [ -n "$GEMINI_KEY" ]; then
    GEMINI_SETUP="
        sudo sed -i 's/^#GEMINI_API_KEY=.*/GEMINI_API_KEY=${GEMINI_KEY}/' /etc/icebreaker/locations.env
        if ! grep -q '^GEMINI_API_KEY=' /etc/icebreaker/locations.env; then
            echo 'GEMINI_API_KEY=${GEMINI_KEY}' | sudo tee -a /etc/icebreaker/locations.env >/dev/null
        fi
        echo '  GEMINI_API_KEY configured.'
    "
fi

${SSH_CMD} --command="
    # Create system users and groups.
    sudo systemd-sysusers /etc/sysusers.d/icebreaker.conf 2>/dev/null || true

    # Create runtime directories.
    sudo systemd-tmpfiles --create /etc/tmpfiles.d/icebreaker.conf 2>/dev/null || true

    # Add icebreaker user to icebreaker-users group.
    sudo usermod -aG icebreaker-users icebreaker 2>/dev/null || true

    # Compile GSettings schemas.
    sudo glib-compile-schemas /usr/share/glib-2.0/schemas/ 2>/dev/null || true

    ${GEMINI_SETUP}

    echo '  System setup complete.'
" 2>/dev/null

# ── Step 11: Enable + restart services ────────────────────────────────
echo "[11/12] Enabling and restarting services..."
${SSH_CMD} --command="
    sudo systemctl daemon-reload

    # Enable Icebreaker services.
    sudo systemctl enable icebreaker-first-boot.service 2>/dev/null || true
    sudo systemctl enable icebreaker-pbd.service 2>/dev/null || true
    sudo systemctl enable icebreaker-qbd.service 2>/dev/null || true
    sudo systemctl enable icebreaker-controller.service 2>/dev/null || true
    sudo systemctl enable icebreaker-controller.socket 2>/dev/null || true
    sudo systemctl enable xrdp 2>/dev/null || true

    # Remove first-boot sentinel so it re-runs (checksum verify, group add).
    sudo rm -f /var/lib/icebreaker/.first-boot-complete

    # Restart Icebreaker services.
    sudo systemctl restart icebreaker-pbd 2>/dev/null || true
    sudo systemctl restart icebreaker-qbd 2>/dev/null || true

    # Wait for PB socket (model load takes a few seconds), then start controller.
    sleep 8
    sudo systemctl restart icebreaker-controller 2>/dev/null || true

    # Start XRDP (creates virtual X sessions on connect — no physical display needed).
    # LightDM is NOT started: GCP VMs have no physical display, so LightDM fails.
    # XRDP uses Xvnc/xorgxrdp and launches startxfce4 from ~/.xsession.
    sudo systemctl restart xrdp 2>/dev/null || true

    echo '  Services started.'
    sleep 5
    echo ''
    echo '  --- Service Status ---'
    for svc in icebreaker-pbd icebreaker-qbd icebreaker-controller xrdp; do
        STATE=\$(systemctl is-active \$svc 2>/dev/null || echo 'not-found')
        echo \"  \$svc: \$STATE\"
    done
" 2>/dev/null

# ── Step 12: Connection instructions ──────────────────────────────────
EXTERNAL_IP=$(gcloud compute instances describe "${VM_NAME}" \
    --zone="${VM_ZONE}" --project="${VM_PROJECT}" \
    --format="get(networkInterfaces[0].accessConfigs[0].natIP)" 2>/dev/null)

echo ""
echo "=== Deployment Complete ==="
echo ""
echo "RDP access (run on your Mac, keep it open):"
echo "  gcloud compute ssh ${VM_NAME} --zone=${VM_ZONE} \\"
echo "    --project=${VM_PROJECT} -- -L 3389:localhost:3389"
echo ""
echo "Then open Microsoft Remote Desktop and connect to:"
echo "  Host:     localhost:3389"
echo "  Username: icebreaker"
echo "  Password: icebreaker"
echo ""
echo "SSH for terminal:"
echo "  gcloud compute ssh ${VM_NAME} --zone=${VM_ZONE} --project=${VM_PROJECT}"
echo ""
echo "Service status:"
echo "  gcloud compute ssh ${VM_NAME} --zone=${VM_ZONE} --project=${VM_PROJECT} \\"
echo "    --command='systemctl status icebreaker-{pbd,qbd,controller}'"
echo ""
if [ -z "$GEMINI_KEY" ]; then
    echo "NOTE: GEMINI_API_KEY not set. QB will return 'not configured' errors."
    echo "  Set it with: bash deploy.sh --gemini-key=YOUR_KEY --skip-build --skip-desktop"
    echo "  Or on the VM: sudo sed -i 's/^#GEMINI_API_KEY=.*/GEMINI_API_KEY=YOUR_KEY/' /etc/icebreaker/locations.env"
    echo "  Then: sudo systemctl restart icebreaker-controller"
    echo ""
fi
echo "REMINDER: The VM bills while running. Stop it when done:"
echo "  gcloud compute instances stop ${VM_NAME} --zone=${VM_ZONE} --project=${VM_PROJECT}"
