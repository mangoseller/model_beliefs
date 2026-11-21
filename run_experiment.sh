#!/usr/bin/env bash
# Run the whole curse-of-knowledge experiment on a fresh GPU instance (e.g. vast.ai) and package it.
#
#   git clone --branch claude/bold-archimedes-8vdm0o https://github.com/mangoseller/model_beliefs && cd model_beliefs
#   nohup bash run_experiment.sh > run.out 2>&1 &      # everything; then download runs/<run_id>.tar.gz
#   bash run_experiment.sh --core --no-install-agents  # main experiment only, skip coding-agent installation
#   bash run_experiment.sh --smoke                     # 20 CKD cases, 3 samples: a quick full check first
#   bash run_experiment.sh --run-id <run_id>           # resume an interrupted run (nothing is regenerated)
#   bash run_experiment.sh --help                      # every option (second_order/runner.py)
#
# Needs: Linux, an NVIDIA GPU with CUDA 12 drivers (48 GB or more runs Qwen3 14B in bf16 comfortably;
# 24 GB is enough for Qwen3 8B alone), about 120 GB of free disk, internet access.
# The two datasets ship in datasets/; the run checks them against their SHA-256 sums.
#
# It also installs the Claude Code and Codex CLIs (`claude`, `codex`) into ~/.local/bin and puts
# them on PATH, here and in ~/.bashrc for later shells. Log in with `claude` / `codex`, or set
# ANTHROPIC_API_KEY / OPENAI_API_KEY. A failed install only warns; the experiment still runs.
#
# Environment: CPU_ONLY=1 skips torch/vLLM (synthetic model only); INSTALL_AGENTS=0 skips the
# Claude Code and Codex install; HF_TOKEN for gated models; VENV=<dir> picks the virtual
# environment (default .venv-experiment).
set -euo pipefail
cd "$(dirname "$0")"
VENV="${VENV:-.venv-experiment}"
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

if [ "${INSTALL_AGENTS:-1}" = 1 ]; then
  install_agent_clis || echo "WARNING: installing Claude Code / Codex failed; continuing without them" >&2
fi

if ! command -v uv >/dev/null 2>&1; then
  curl -LsSf https://astral.sh/uv/install.sh | sh
  export PATH="$HOME/.local/bin:$HOME/.cargo/bin:$PATH"
fi
[ -x "$VENV/bin/python" ] || uv venv --python 3.11 "$VENV"
if [ "${CPU_ONLY:-0}" = 1 ]; then
  uv pip install --python "$VENV/bin/python" -r requirements/experiment.txt
  # The latent fits need torch even without a GPU: the CPU build of the same version.
  uv pip install --python "$VENV/bin/python" torch==2.6.0 --index-url https://download.pytorch.org/whl/cpu
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
exec "$VENV/bin/python" -m second_order.runner "$@"
