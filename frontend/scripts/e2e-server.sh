#!/usr/bin/env bash
# Starts an isolated, deterministic SwarmOS instance for Playwright (fresh DB, no network sync).
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
E2E_DIR="${TMPDIR:-/tmp}/swarmos-e2e"
rm -rf "$E2E_DIR" && mkdir -p "$E2E_DIR/ml"
cp -R "$ROOT/backend/swarmos/ml/configs" "$E2E_DIR/ml/configs"
(
  cd "$ROOT/frontend"
  npx tsc --noEmit --incremental false
  npx vite build --configLoader runner
)
export SWARMOS_DATABASE_URL="sqlite:///$E2E_DIR/e2e.db"
export SWARMOS_ML_ROOT="$E2E_DIR/ml"
export SWARMOS_ML_CONFIGS_DIR="$E2E_DIR/ml/configs"
export SWARMOS_DEV_SEED_PASSWORD="${E2E_PASSWORD:-E2eTestPassw0rd}"
export SWARMOS_SYNC_ENABLED=false
export SWARMOS_DEMO_PILOT_CITIES="bengaluru,mumbai"
export SWARMOS_LOGIN_RATE_LIMIT_PER_MINUTE=1000
SW="$ROOT/backend/.venv/bin/swarmos"
if [[ -x "$SW" ]]; then
  SW_CMD=("$SW")
else
  PYTHON="${SWARMOS_PYTHON:-python3}"
  export PYTHONPATH="$ROOT/backend${PYTHONPATH:+:$PYTHONPATH}"
  SW_CMD=("$PYTHON" -m swarmos.cli)
fi
"${SW_CMD[@]}" init >/dev/null
"${SW_CMD[@]}" ml train complaint_classifier --promote-if-better >/dev/null
exec "${SW_CMD[@]}" serve --port "${E2E_PORT:-8010}"
