#!/usr/bin/env bash
# Start/health/stop a real vLLM OpenAI-compatible server on AutoDL.
# This script never fabricates model output or benchmark results.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
STATE_DIR="${AI6G_VLLM_STATE_DIR:-$SCRIPT_DIR/state}"
mkdir -p "$STATE_DIR"

ACTION="${1:-help}"
shift || true
MODEL_KEY="qwen2.5-1.5b"
PORT=""
HOST="${AI6G_VLLM_HOST:-0.0.0.0}"
MODEL_PATH=""
SERVED_NAME=""
GPU_MEM="${AI6G_VLLM_GPU_MEMORY_UTILIZATION:-0.90}"
TP_SIZE="${AI6G_VLLM_TENSOR_PARALLEL_SIZE:-1}"
MAX_MODEL_LEN=""
CUDA_DEVICES="${CUDA_VISIBLE_DEVICES:-}"

while [[ $# -gt 0 ]]; do
  case "$1" in
    --model) MODEL_KEY="$2"; shift 2 ;;
    --port) PORT="$2"; shift 2 ;;
    --host) HOST="$2"; shift 2 ;;
    --model-path) MODEL_PATH="$2"; shift 2 ;;
    --served-model-name) SERVED_NAME="$2"; shift 2 ;;
    --gpu-memory-utilization) GPU_MEM="$2"; shift 2 ;;
    --tensor-parallel-size) TP_SIZE="$2"; shift 2 ;;
    --max-model-len) MAX_MODEL_LEN="$2"; shift 2 ;;
    --cuda-devices) CUDA_DEVICES="$2"; shift 2 ;;
    -h|--help) ACTION="help"; shift ;;
    *) echo "Unknown argument: $1" >&2; exit 2 ;;
  esac
done

case "$MODEL_KEY" in
  qwen2.5-1.5b|1.5b)
    DEFAULT_MODEL="Qwen/Qwen2.5-1.5B-Instruct"; DEFAULT_PORT=8001; DEFAULT_NAME="qwen2.5-1.5b"; DEFAULT_LEN=4096 ;;
  qwen2.5-7b|7b)
    DEFAULT_MODEL="Qwen/Qwen2.5-7B-Instruct"; DEFAULT_PORT=8002; DEFAULT_NAME="qwen2.5-7b"; DEFAULT_LEN=4096 ;;
  *) echo "Unsupported model key: $MODEL_KEY (use qwen2.5-1.5b or qwen2.5-7b)" >&2; exit 2 ;;
esac

PORT="${PORT:-$DEFAULT_PORT}"
MODEL_PATH="${MODEL_PATH:-$DEFAULT_MODEL}"
SERVED_NAME="${SERVED_NAME:-$DEFAULT_NAME}"
MAX_MODEL_LEN="${MAX_MODEL_LEN:-$DEFAULT_LEN}"
PID_FILE="$STATE_DIR/vllm-${PORT}.pid"
LOG_FILE="$STATE_DIR/vllm-${PORT}.log"
BASE_URL="http://127.0.0.1:${PORT}"

usage() {
  cat <<EOF
Usage:
  $0 start  [--model qwen2.5-1.5b|qwen2.5-7b] [--port N] [--model-path PATH]
            [--host HOST] [--served-model-name NAME] [--gpu-memory-utilization F]
            [--tensor-parallel-size N] [--max-model-len N] [--cuda-devices IDS]
  $0 health [--port N]
  $0 stop   [--port N]

Environment:
  AI6G_VLLM_STATE_DIR, AI6G_VLLM_HOST, AI6G_VLLM_GPU_MEMORY_UTILIZATION,
  AI6G_VLLM_TENSOR_PARALLEL_SIZE, CUDA_VISIBLE_DEVICES

The start command requires a vLLM installation and a model that AutoDL can
retrieve or that is already present at --model-path. No API key is written here.
EOF
}

is_vllm_pid() {
  local pid="$1"
  [[ "$pid" =~ ^[0-9]+$ ]] || return 1
  kill -0 "$pid" 2>/dev/null || return 1
  ps -p "$pid" -o args= 2>/dev/null | grep -q 'vllm.*api_server'
}

health() {
  command -v curl >/dev/null 2>&1 || { echo "curl is required for health checks" >&2; return 2; }
  echo "GET ${BASE_URL}/health"
  curl --fail --silent --show-error --max-time 10 "${BASE_URL}/health" >/dev/null
  echo "GET ${BASE_URL}/v1/models"
  curl --fail --silent --show-error --max-time 10 "${BASE_URL}/v1/models"
  echo
  echo "health check passed for ${BASE_URL}"
}

case "$ACTION" in
  help) usage ;;
  health) health ;;
  stop)
    if [[ ! -f "$PID_FILE" ]]; then echo "No PID file: $PID_FILE"; exit 0; fi
    pid="$(cat "$PID_FILE")"
    if is_vllm_pid "$pid"; then
      echo "Stopping vLLM PID $pid"
      kill "$pid"
      for _ in {1..20}; do is_vllm_pid "$pid" || break; sleep 1; done
      if is_vllm_pid "$pid"; then echo "vLLM did not stop after 20s; inspect $LOG_FILE" >&2; exit 1; fi
    else
      echo "PID file does not refer to a live vLLM server; refusing to kill PID $pid" >&2
    fi
    rm -f "$PID_FILE"
    ;;
  start)
    command -v python >/dev/null 2>&1 || { echo "python is required" >&2; exit 1; }
    python -c 'import vllm' >/dev/null 2>&1 || { echo "vLLM is not installed in this environment" >&2; exit 1; }
    if [[ -f "$PID_FILE" ]] && is_vllm_pid "$(cat "$PID_FILE")"; then
      echo "vLLM already running; use health or stop first: PID $(cat "$PID_FILE")" >&2
      exit 1
    fi
    cmd=(python -m vllm.entrypoints.openai.api_server
      --host "$HOST" --port "$PORT" --model "$MODEL_PATH"
      --served-model-name "$SERVED_NAME" --dtype auto
      --tensor-parallel-size "$TP_SIZE" --gpu-memory-utilization "$GPU_MEM"
      --max-model-len "$MAX_MODEL_LEN")
    echo "Starting real vLLM server: ${cmd[*]}"
    echo "Log: $LOG_FILE"
    if [[ -n "$CUDA_DEVICES" ]]; then
      CUDA_VISIBLE_DEVICES="$CUDA_DEVICES" nohup "${cmd[@]}" >"$LOG_FILE" 2>&1 &
    else
      nohup "${cmd[@]}" >"$LOG_FILE" 2>&1 &
    fi
    pid=$!
    echo "$pid" > "$PID_FILE"
    echo "Started PID $pid; run '$0 health --port $PORT' after the server reports ready."
    ;;
  *) echo "Unknown action: $ACTION" >&2; usage; exit 2 ;;
esac
