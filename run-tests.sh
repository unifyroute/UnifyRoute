#!/usr/bin/env bash
# ──────────────────────────────────────────────────────────────────
# run-tests.sh — OPENROUTER Test Suite Runner
#
# Usage:
#   ./run-tests.sh              # run all tests (unit + integration)
#   ./run-tests.sh --unit       # unit tests only (no live server needed)
#   ./run-tests.sh --integration# integration tests only (requires live gateway)
#   ./run-tests.sh -k auth      # run only auth tests
#   ./run-tests.sh -x           # stop on first failure
#   ./run-tests.sh --co         # collect only (dry-run)
#
# Prerequisites:
#   - For integration tests: API gateway must be running on http://localhost:6565
#     (or override: OPENROUTER_BASE_URL=http://host:port ./run-tests.sh)
#   - .admin_token and .api_token files exist at the project root
#     (created automatically by: ./unifyroute create token api and ./unifyroute create token admin)
#
# ──────────────────────────────────────────────────────────────────
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

# Cross-platform: Linux/macOS use bin/, Windows (Git Bash/MSYS) uses Scripts/
if [[ -f ".venv/bin/python" ]]; then
    VENV_PYTHON=".venv/bin/python"
elif [[ -f ".venv/Scripts/python" ]]; then
    VENV_PYTHON=".venv/Scripts/python"
elif [[ -f ".venv/Scripts/python.exe" ]]; then
    VENV_PYTHON=".venv/Scripts/python.exe"
else
    echo "❌  Virtual environment not found at .venv/"
    echo "    Run: ./unifyroute setup"
    exit 1
fi

echo "──────────────────────────────────────────────────"
echo " OPENROUTER Test Suite"
echo "──────────────────────────────────────────────────"

# Parse our custom flags (strip them before passing to pytest)
UNIT_ONLY=false
INTEGRATION_ONLY=false
PYTEST_ARGS=()

for arg in "$@"; do
    case "$arg" in
        --unit)
            UNIT_ONLY=true
            ;;
        --integration)
            INTEGRATION_ONLY=true
            ;;
        *)
            PYTEST_ARGS+=("$arg")
            ;;
    esac
done

# ── Unit tests (no live server required) ──────────────────────────
run_unit() {
    echo ""
    echo "▶  Running unit tests (no server required)..."
    echo ""
    "$VENV_PYTHON" -m pytest \
        tests/test_brain_health.py \
        tests/test_brain_importer.py \
        tests/test_brain_ranker.py \
        tests/test_brain_selector.py \
        tests/test_brain_tester.py \
        tests/test_router_core.py \
        tests/test_shared_security.py \
        tests/test_stream_cost_unit.py \
        "${PYTEST_ARGS[@]+"${PYTEST_ARGS[@]}"}"
}

# ── Integration tests (requires live gateway) ─────────────────────
run_integration() {
    # Check admin token
    if [[ ! -f ".admin_token" ]] && [[ -z "${ADMIN_TOKEN:-}" ]]; then
        echo "⚠️  No admin token found (.admin_token file or ADMIN_TOKEN env var)"
        echo "   Creating test admin token directly in DB..."
        python3 -c '
import os, secrets, hashlib, uuid
raw_token = f"sk-{secrets.token_urlsafe(32)}"
key_hash = hashlib.sha256(raw_token.encode()).hexdigest()
key_id = str(uuid.uuid4())
import sqlite3
conn = sqlite3.connect("data/unifyroute.db")
c = conn.cursor()
c.execute("INSERT INTO gateway_keys (id, label, key_hash, scopes, enabled) VALUES (?, ?, ?, ?, ?)", (key_id, "test-suite-admin", key_hash, "[\"admin\", \"api\"]", 1))
conn.commit()
with open(".admin_token", "w") as f:
    f.write(raw_token)
'
        if [[ $? -ne 0 ]]; then
            echo "❌  Failed to create admin token."
            exit 1
        fi
    fi

    # Auto-create API token if missing
    if [[ ! -f ".api_token" ]] && [[ -z "${API_TOKEN:-}" ]]; then
        echo "ℹ️  No .api_token file found. Creating a temporary API token for tests..."
        ADMIN_TOKEN_VAL="${ADMIN_TOKEN:-$(cat .admin_token)}"
        BASE="${OPENROUTER_BASE_URL:-http://localhost:6565}"

        RESPONSE=$(curl -s -X POST "$BASE/api/admin/keys" \
            -H "Authorization: Bearer $ADMIN_TOKEN_VAL" \
            -H "Content-Type: application/json" \
            -d '{"label":"test-suite-runner","scopes":["api"]}')

        TOKEN=$(echo "$RESPONSE" | python3 -c "import sys,json; d=json.load(sys.stdin); print(d.get('token',''))" 2>/dev/null || true)
        if [[ -z "$TOKEN" ]]; then
            echo "❌  Failed to create API token automatically. Response: $RESPONSE"
            exit 1
        fi

        echo "$TOKEN" > .api_token
        echo "   ✅ Created .api_token"
    fi

    echo ""
    echo "▶  Running integration tests (live gateway required)..."
    echo ""
    "$VENV_PYTHON" -m pytest \
        tests/test_auth.py \
        tests/test_providers.py \
        tests/test_credentials.py \
        tests/test_keys.py \
        tests/test_key_reveal.py \
        tests/test_key_update.py \
        tests/test_admin_models.py \
        tests/test_models_endpoint.py \
        tests/test_chat_completions.py \
        tests/test_routing_config.py \
        tests/test_routing_tiers.py \
        tests/test_brain_api.py \
        tests/test_brain_ranker.py \
        tests/test_wizard_api.py \
        tests/test_gateway_health.py \
        tests/test_oauth_routes.py \
        tests/test_connection_security.py \
        tests/test_quota_pollers.py \
        tests/test_sync_models.py \
        tests/test_fireworks.py \
        "${PYTEST_ARGS[@]+"${PYTEST_ARGS[@]}"}"
}

if [[ "$UNIT_ONLY" == true ]]; then
    run_unit
elif [[ "$INTEGRATION_ONLY" == true ]]; then
    run_integration
else
    # Run everything
    run_unit
    run_integration
fi

echo ""
echo "✅  All requested tests completed."
