#!/usr/bin/env bash
# Start the local model server the whole crew shares.
# Settings follow NVIDIA's RTX Spark recipe for Qwen3.8-Flash-Next (Windows, llama.cpp, UD-IQ4_XS, MTP):
#   https://github.com/NVIDIA/local-ai-inference-recipes/tree/main/models/qwen/qwen3.8-flash-next/rtx-spark/windows/llama-cpp/78gb/iq4-xs/vision-mtp
# Changes from the recipe, both for running several agents at once:
#   --parallel $AGENTS with one unified KV pool ($PER_SLOT tokens per agent), instead of one 262K slot
#   no vision projector (the crew reads code, not images)
#
# Usage: AGENTS=4 PER_SLOT=131072 MODEL=flash-next ./serve_model.sh
set -euo pipefail
AGENTS=${AGENTS:-4}
PER_SLOT=${PER_SLOT:-131072}
MODEL=${MODEL:-flash-next}
M=${MODELS_DIR:-/c/Users/ASUS/proart-crew/models}

case "$MODEL" in
  flash-next)
    ARGS=(-m "$M/qwen3.8-flash-next/Qwen3.8-Flash-Next-UD-IQ4_XS-00001-of-00004.gguf" --alias qwen3.8-flash-next
          --spec-type draft-mtp --spec-draft-n-max 3 --spec-draft-model "$M/qwen3.8-flash-next/mtp-Qwen3.8-Flash-Next-Q8_0.gguf"
          --temp 1.0 --top-p 0.95 --top-k 20 --min-p 0.0 --presence-penalty 0.0 --repeat-penalty 1.0) ;;
  coder-next)
    ARGS=(-m "$M/qwen3-coder-next/Qwen3-Coder-Next-Q6_K-00001-of-00004.gguf" --alias qwen3-coder-next
          --temp 1.0 --top-p 0.95 --top-k 40) ;;
  9b)
    ARGS=(-m "$M/qwen3.5-9b/Qwen3.5-9B-UD-Q4_K_XL.gguf" --alias qwen3.5-9b) ;;
  *) echo "unknown MODEL=$MODEL" >&2; exit 2 ;;
esac

# POOL: total tokens in the shared KV pool. Each agent can still grow to PER_SLOT, but the pool is
# smaller than AGENTS*PER_SLOT because agents rarely peak together (measured 23K-62K each, 11/19).
# At ~36 KB/token for this model (12 full-attention layers), 4x131K reserved ~19 GB and pushed
# Windows commit to 130/138 GB. --cache-ram caps llama-server's RAM prompt cache (default 8 GB).
POOL=${POOL:-327680}
exec llama-server.exe "${ARGS[@]}" --host 127.0.0.1 --port 9931 \
  --parallel "$AGENTS" -kvu -c "$POOL" --kv-unified-per-slot "$PER_SLOT" --cache-ram 1024 \
  --threads-http 16 --timeout 900 --threads 8 --poll 0 \
  --batch-size 2048 --ubatch-size 512 -fa on -lzm on -ngl 999 \
  --jinja --metrics --slots
