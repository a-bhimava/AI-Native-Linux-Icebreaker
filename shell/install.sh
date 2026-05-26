#!/usr/bin/env bash
# install.sh — Install the Privileged Brain shell trigger into your shell config.
# Adds a source line to ~/.zshrc (or ~/.bashrc for bash users).
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SHELL_NAME="$(basename "$SHELL")"

echo "========================================"
echo " Privileged Brain — Shell Trigger Setup"
echo "========================================"
echo ""
echo "Script dir : $SCRIPT_DIR"
echo "Shell      : $SHELL_NAME"
echo ""

# ── Verify Ollama is running ──────────────────────────────────────────────────
if ! curl -s --max-time 3 http://localhost:11434/api/tags &>/dev/null; then
    echo "WARNING: Ollama is not running at localhost:11434."
    echo "         Start it with: ollama serve"
    echo "         The trigger will still install but won't work until Ollama is up."
    echo ""
fi

# ── Check which model is available ───────────────────────────────────────────
if ollama list 2>/dev/null | grep -q "privileged-brain"; then
    echo "Model: privileged-brain  ✓  (fine-tuned — best quality)"
elif ollama list 2>/dev/null | grep -q "qwen2.5-coder"; then
    echo "Model: qwen2.5-coder:1.5b  ✓  (base model — will improve after fine-tuning)"
else
    echo "WARNING: No compatible model found in Ollama."
    echo "         Run: ollama pull qwen2.5-coder:1.5b"
fi
echo ""

# ── Install based on shell ────────────────────────────────────────────────────
case "$SHELL_NAME" in
  zsh)
    RC_FILE="$HOME/.zshrc"
    SOURCE_LINE="source \"${SCRIPT_DIR}/pb_trigger.zsh\""
    ;;
  bash)
    RC_FILE="$HOME/.bashrc"
    SOURCE_LINE="source \"${SCRIPT_DIR}/pb_trigger.bash\""
    ;;
  *)
    echo "ERROR: Unsupported shell: $SHELL_NAME"
    echo "       Only zsh and bash are supported."
    exit 1
    ;;
esac

# ── Add source line if not already present ────────────────────────────────────
if grep -qF "$SCRIPT_DIR/pb_trigger" "$RC_FILE" 2>/dev/null; then
    echo "Already installed in $RC_FILE — skipping."
else
    echo "" >> "$RC_FILE"
    echo "# Privileged Brain ^ trigger" >> "$RC_FILE"
    echo "$SOURCE_LINE" >> "$RC_FILE"
    echo "Added to $RC_FILE:"
    echo "  $SOURCE_LINE"
fi

echo ""
echo "========================================"
echo " Installation complete!"
echo "========================================"
echo ""
echo " Reload your shell:"
echo "   source $RC_FILE"
echo ""
echo " Then try:"
echo "   ^show disk usage"
echo "   ^list all running docker containers"
echo "   ^find files larger than 100MB in home"
echo ""
echo " Commands:"
echo "   python3 $SCRIPT_DIR/pb_cache.py stats   ← cache stats"
echo "   python3 $SCRIPT_DIR/pb_audit.py list    ← recent commands"
echo "   python3 $SCRIPT_DIR/pb_audit.py tail    ← live log"
echo ""
