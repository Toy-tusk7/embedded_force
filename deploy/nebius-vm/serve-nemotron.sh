#!/usr/bin/env bash
# Serve NVIDIA Nemotron 3 Super with vLLM on a Nebius AI Cloud GPU VM: an OpenAI-compatible API on 127.0.0.1:8001,
# reachable only from the VM itself (EmbedForge runs on the same VM).
#
# Defaults follow the vLLM recipe for NVIDIA-Nemotron-3-Super-120B-A12B: FP8 weights, tensor parallel 4 (4x H100/H200).
# On 2 Blackwell GPUs use the NVFP4 checkpoint:
#   MODEL=nvidia/NVIDIA-Nemotron-3-Super-120B-A12B-NVFP4 TP=2 ./serve-nemotron.sh
set -euo pipefail

MODEL="${MODEL:-nvidia/NVIDIA-Nemotron-3-Super-120B-A12B-FP8}"
TP="${TP:-4}"                       # tensor parallel size = number of GPUs
PORT="${PORT:-8001}"                # 8000 is the EmbedForge IDE
NAME="${NAME:-nemotron-3-super}"    # the model name EmbedForge asks for (SELFHOSTED_MODEL)
MAX_LEN="${MAX_LEN:-131072}"        # context per request; EmbedForge sends about 40k tokens at most

exec vllm serve "$MODEL" \
  --host 127.0.0.1 --port "$PORT" \
  --served-model-name "$NAME" \
  --tensor-parallel-size "$TP" \
  --max-model-len "$MAX_LEN" \
  --kv-cache-dtype fp8 \
  --trust-remote-code \
  --enable-auto-tool-choice \
  --tool-call-parser qwen3_coder \
  --reasoning-parser nemotron_v3
