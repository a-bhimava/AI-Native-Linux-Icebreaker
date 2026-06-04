#!/usr/bin/env bash
# ─────────────────────────────────────────────────────────────────────────────
# pb-shell.sh — Privileged Brain Shell Integration
# Drop into /etc/profile.d/pb-shell.sh (sourced by every interactive shell)
#
# Features:
#   ai "question"                  — ask anything inline
#   Ctrl+G (bash) / ^G (zsh)       — explain last command's stderr
#   command_not_found_handler       — AI suggests fix for missing commands
#   pb-why                          — explain last non-zero exit
# ─────────────────────────────────────────────────────────────────────────────

# Only activate in interactive shells
[[ $- == *i* ]] || return 0

# ── Core: ai() function ───────────────────────────────────────────────────────
ai() {
  if [ $# -eq 0 ]; then
    echo "Usage: ai \"your question\""
    return 1
  fi
  pb-ask "$*"
}
export -f ai 2>/dev/null || true

# ── pb-why: explain last non-zero exit ───────────────────────────────────────
pb-why() {
  local exit_code="${_PB_LAST_EXIT:-$?}"
  local last_cmd="${_PB_LAST_CMD:-}"
  if [ "$exit_code" -eq 0 ]; then
    echo "Last command succeeded (exit 0)."
    return 0
  fi
  pb-ask "The command \`${last_cmd}\` exited with code ${exit_code}. What does this exit code mean and how do I fix it?"
}
export -f pb-why 2>/dev/null || true

# ── Track last command and exit code ─────────────────────────────────────────
_pb_track() {
  _PB_LAST_EXIT=$?
  # BASH_COMMAND is set before each simple command
  _PB_LAST_CMD="${BASH_COMMAND}"
}

if [ -n "$BASH_VERSION" ]; then
  # bash: use DEBUG trap to capture last command
  trap '_pb_track' DEBUG

  # Ctrl+G → explain last error
  _pb_explain_last() {
    if [ "${_PB_LAST_EXIT:-0}" -ne 0 ]; then
      echo ""
      pb-ask "Command \`${_PB_LAST_CMD}\` failed with exit code ${_PB_LAST_EXIT}. Explain why and give the fix."
    else
      echo ""
      pb-ask "Explain what \`${_PB_LAST_CMD}\` does."
    fi
  }
  bind -x '"\C-g": _pb_explain_last' 2>/dev/null || true

elif [ -n "$ZSH_VERSION" ]; then
  # zsh: use preexec / precmd hooks
  autoload -Uz add-zsh-hook 2>/dev/null

  _pb_preexec() { _PB_LAST_CMD="$1"; }
  _pb_precmd()  { _PB_LAST_EXIT=$?; }
  add-zsh-hook preexec _pb_preexec
  add-zsh-hook precmd  _pb_precmd

  # Ctrl+G widget
  _pb_explain_last_zsh() {
    if [ "${_PB_LAST_EXIT:-0}" -ne 0 ]; then
      print ""
      pb-ask "Command \`${_PB_LAST_CMD}\` failed with exit ${_PB_LAST_EXIT}. Explain and fix."
    else
      print ""
      pb-ask "Explain what \`${_PB_LAST_CMD}\` does."
    fi
    zle reset-prompt
  }
  zle -N _pb_explain_last_zsh
  bindkey '^G' _pb_explain_last_zsh 2>/dev/null || true
fi

# ── command_not_found_handler ─────────────────────────────────────────────────
command_not_found_handler() {
  local cmd="$1"
  shift
  echo "bash: $cmd: command not found"
  echo ""
  pb-ask "The command '$cmd' was not found on this Linux system. What package installs it, or what is the modern alternative?" 2>/dev/null \
    || echo "[pb-ask not available — run: sudo systemctl start pb-daemon]"
  return 127
}

# For zsh
command_not_found_handler() {
  local cmd="$1"
  echo "zsh: command not found: $cmd"
  echo ""
  pb-ask "The command '$cmd' was not found. What package installs it or what is the alternative?" 2>/dev/null || true
  return 127
}

# ── Startup banner ────────────────────────────────────────────────────────────
if command -v pb-ask &>/dev/null; then
  # Only show on first interactive login (check if pb-daemon is alive)
  if [ -S "/run/pb-daemon.sock" ]; then
    printf "\033[32m[Privileged Brain active]\033[0m  Type \033[1mai \"question\"\033[0m or press \033[1mCtrl+G\033[0m after a failed command.\n"
  fi
fi
