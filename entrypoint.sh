#!/usr/bin/env bash
# Install dependencies and launch either experiment from a fresh instance.
# VENV overrides the experiment's virtual environment. CPU_ONLY=1 installs CPU torch.
# Leakage installs the Claude Code and Codex CLIs by default. Either experiment accepts
# --install-agents and --no-install-agents, or INSTALL_AGENTS=1/0.
set -euo pipefail
cd "$(dirname "$0")"

usage() {
  echo "Usage: $0 leakage [runner options] | steering [stage] [steering options]"
  echo "Examples: $0 leakage --smoke"
  echo "          $0 steering baseline --model qwen3_8b"
  echo "Use <experiment> --help for its options."
}

case "${1:-}" in
  leakage)
    module=belief_leakage.runner
    VENV="${VENV:-.venv-experiment}"
    default_agents=1
    ;;
  steering)
    module=steering.run
    VENV="${VENV:-.venv-steering}"
    default_agents=0
    ;;
  -h|--help) usage; exit 0 ;;
  *) usage >&2; exit 2 ;;
esac
experiment=$1
shift
NODE_VERSION=v22.12.0
LOCAL="$HOME/.local"
PATH_LINE='export PATH="$HOME/.local/bin:$HOME/.local/node/bin:$PATH"'

install_agent_clis() {
  # Called as `install_agent_clis || ...`, where bash ignores set -e: each step checks itself.
  # Node.js (for npm) unless the machine already has it: the official Linux build into ~/.local/node.
  if ! command -v npm >/dev/null 2>&1; then
    local arch
    case "$(uname -m)" in x86_64) arch=x64 ;; aarch64 | arm64) arch=arm64 ;; *) echo "unsupported CPU $(uname -m)"; return 1 ;; esac
    mkdir -p "$LOCAL" || return 1
    curl -fsSL "https://nodejs.org/dist/$NODE_VERSION/node-$NODE_VERSION-linux-$arch.tar.xz" | tar -xJ -C "$LOCAL" || return 1
    ln -sfn "$LOCAL/node-$NODE_VERSION-linux-$arch" "$LOCAL/node" || return 1
  fi
  export PATH="$LOCAL/bin:$LOCAL/node/bin:$PATH"
  npm install --global --prefix "$LOCAL" @anthropic-ai/claude-code @openai/codex || return 1
  command -v claude >/dev/null && command -v codex >/dev/null || return 1
  grep -qxF "$PATH_LINE" "$HOME/.bashrc" 2>/dev/null || echo "$PATH_LINE" >> "$HOME/.bashrc"
  echo "installed: claude $(claude --version | head -1); codex $(codex --version | head -1)"
}

runner_args=()
for arg in "$@"; do
    case "$arg" in
        --install-agents) INSTALL_AGENTS=1 ;;
        --no-install-agents) INSTALL_AGENTS=0 ;;
        *) runner_args+=("$arg") ;;
    esac
done
set -- "${runner_args[@]}"

if [ "${INSTALL_AGENTS:-$default_agents}" = 1 ]; then
  install_agent_clis || echo "WARNING: installing Claude Code / Codex failed; continuing without them" >&2
fi

if ! command -v uv >/dev/null 2>&1; then
  curl -LsSf https://astral.sh/uv/install.sh | sh
  export PATH="$HOME/.local/bin:$HOME/.cargo/bin:$PATH"
fi
[ -x "$VENV/bin/python" ] || uv venv --python 3.11 "$VENV"
if [ "${CPU_ONLY:-0}" = 1 ]; then
  uv pip install --python "$VENV/bin/python" torch==2.6.0 --index-url https://download.pytorch.org/whl/cpu
fi
if [ "$experiment" = steering ]; then
  uv pip install --python "$VENV/bin/python" -r requirements/steering.txt
elif [ "${CPU_ONLY:-0}" = 1 ]; then
  uv pip install --python "$VENV/bin/python" -r requirements/experiment.txt
elif [ "$(uname -s)-$(uname -m)" = Linux-x86_64 ]; then
  # Every package pinned, vLLM's dependencies at versions from its release (requirements/lock-cuda.txt).
  uv pip install --python "$VENV/bin/python" -r requirements/lock-cuda.txt
  export HF_HUB_ENABLE_HF_TRANSFER=1
else
  uv pip install --python "$VENV/bin/python" -r requirements/experiment.txt -r requirements/gpu.txt
  export HF_HUB_ENABLE_HF_TRANSFER=1
fi

export PYTHONPATH="$PWD${PYTHONPATH:+:$PYTHONPATH}"
export TOKENIZERS_PARALLELISM=false
exec "$VENV/bin/python" -m "$module" "$@"
