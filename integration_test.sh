#!/usr/bin/env bash
# CoreMesh credential-free Compose integration validation.

set -Eeuo pipefail

ROOT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)"
cd "$ROOT_DIR"
umask 077

if command -v python3 >/dev/null 2>&1 \
  && python3 -c 'import sys; raise SystemExit(sys.version_info < (3, 8))' >/dev/null 2>&1; then
  PYTHON_BIN=python3
elif command -v python >/dev/null 2>&1 \
  && python -c 'import sys; raise SystemExit(sys.version_info < (3, 8))' >/dev/null 2>&1; then
  PYTHON_BIN=python
else
  echo "error: Python 3 is required for JSON assertions and free-port selection" >&2
  exit 2
fi

for command_name in docker curl; do
  if ! command -v "$command_name" >/dev/null 2>&1; then
    echo "error: $command_name is required" >&2
    exit 2
  fi
done
if ! docker compose version >/dev/null 2>&1; then
  echo "error: Docker Compose v2 is required" >&2
  exit 2
fi
if ! docker info >/dev/null 2>&1; then
  echo "error: the Docker daemon is not available" >&2
  exit 2
fi

PROJECT_NAME="coremesh-it-$(date -u +%Y%m%d%H%M%S)-${RANDOM}"
POSTGRES_PASSWORD="$("$PYTHON_BIN" -c 'import secrets, sys; sys.stdout.write(secrets.token_hex(24))')"
read -r POSTGRES_HOST_PORT REDIS_HOST_PORT REDISINSIGHT_HOST_PORT \
  QDRANT_HTTP_HOST_PORT QDRANT_GRPC_HOST_PORT RUNTIME_HOST_PORT \
  GATEWAY_HOST_PORT FRONTEND_HOST_PORT < <(
  "$PYTHON_BIN" - <<'PY'
import socket
import sys

sockets = []
try:
    for _ in range(8):
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.bind(("127.0.0.1", 0))
        sockets.append(sock)
    sys.stdout.buffer.write(
        (" ".join(str(sock.getsockname()[1]) for sock in sockets) + "\n").encode()
    )
finally:
    for sock in sockets:
        sock.close()
PY
)

export POSTGRES_USER=coremesh
export POSTGRES_PASSWORD
export POSTGRES_DB=coremesh
export COREMESH_BIND_ADDRESS=127.0.0.1
export POSTGRES_HOST_PORT REDIS_HOST_PORT REDISINSIGHT_HOST_PORT
export QDRANT_HTTP_HOST_PORT QDRANT_GRPC_HOST_PORT
export RUNTIME_HOST_PORT GATEWAY_HOST_PORT FRONTEND_HOST_PORT
export VITE_GATEWAY_BASE_URL="http://127.0.0.1:${GATEWAY_HOST_PORT}"
export GATEWAY_ALLOWED_ORIGINS="http://127.0.0.1:${FRONTEND_HOST_PORT}"
export COREMESH_RUNTIME_CONTEXT=./services-runtime
export COREMESH_GATEWAY_CONTEXT=./gateway-proxy
export COREMESH_FRONTEND_CONTEXT=./frontend-ui
export COREMESH_ANALYTICS_CONTEXT=./analytics-workers
export COMPOSE_DISABLE_ENV_FILE=1

# Hermetic runtime policy: no provider credentials or runtime model downloads.
export OPENAI_API_KEY=
export ANTHROPIC_API_KEY=
export OTEL_EXPORTER_OTLP_ENDPOINT=
export RAG_EMBEDDING_PROVIDER=hash
export RAG_RERANKER_PROVIDER=lexical
export SEMANTIC_CACHE_ENABLED=true
export SEMANTIC_CACHE_EMBEDDING_PROVIDER=hash
export ARBITRATION_MODE=deterministic
export OCR_EASYOCR_ENABLED=false
export COREMESH_CHAT_STUB=true
export PRODUCTION_INTERACTION_LOGGING_ENABLED=true
export AUTOPILOT_ENABLED=true
export AUTOPILOT_EXPERIMENT_FLAG=cost_autopilot_routing
export AUTOPILOT_DEBUG=false
export FORENSICS_ENABLED=true

GATEWAY_URL="http://127.0.0.1:${GATEWAY_HOST_PORT}"
FRONTEND_URL="http://127.0.0.1:${FRONTEND_HOST_PORT}"
WORK_DIR="$(mktemp -d "${TMPDIR:-/tmp}/coremesh-it.XXXXXX")"
FAILED=0

compose() {
  docker compose \
    --project-name "$PROJECT_NAME" \
    --profile app \
    --profile analytics \
    "$@"
}

dump_diagnostics() {
  set +e
  echo >&2
  echo "--- CoreMesh integration diagnostics (${PROJECT_NAME}) ---" >&2
  compose ps --all >&2
  compose logs --no-color --tail=250 >&2
}

on_error() {
  local exit_code=$?
  trap - ERR
  FAILED=1
  echo "integration validation failed (exit ${exit_code})" >&2
  dump_diagnostics
  exit "$exit_code"
}

cleanup() {
  local exit_code=$?
  set +e
  if [[ "${KEEP_STACK:-0}" == "1" ]]; then
    echo "KEEP_STACK=1: preserving Compose project ${PROJECT_NAME}"
    echo "Artifacts: ${WORK_DIR}"
    echo "Gateway: ${GATEWAY_URL}"
    echo "Frontend: ${FRONTEND_URL}"
  else
    compose down --volumes --remove-orphans >/dev/null 2>&1
    if [[ -n "$WORK_DIR" && -d "$WORK_DIR" && "$WORK_DIR" == *coremesh-it.* ]]; then
      rm -rf -- "$WORK_DIR"
    fi
  fi
  if [[ "$FAILED" == "0" && "$exit_code" == "0" ]]; then
    echo "CoreMesh integration validation passed (project ${PROJECT_NAME})."
  fi
}

trap on_error ERR
trap cleanup EXIT

wait_for_http() {
  local url=$1
  local attempts=${2:-90}
  local delay=${3:-2}
  local count
  for ((count = 1; count <= attempts; count++)); do
    if curl --silent --show-error --fail "$url" >/dev/null 2>&1; then
      return 0
    fi
    sleep "$delay"
  done
  echo "error: timed out waiting for $url" >&2
  return 1
}

echo "[setup] starting isolated PostgreSQL and applying the upgrade migration twice"
compose up --detach --wait --wait-timeout 180 postgres
compose build log-miner
compose run --rm log-miner migrate
compose run --rm log-miner migrate

echo "[setup] validating bootstrap/upgrade catalog parity"
compose exec -T postgres psql \
  --username "$POSTGRES_USER" \
  --dbname "$POSTGRES_DB" \
  --set ON_ERROR_STOP=1 <<'SQL'
DO $$
DECLARE
    missing_names text[];
BEGIN
    SELECT array_agg(required_name)
      INTO missing_names
      FROM unnest(ARRAY[
          'golden_datasets',
          'production_interaction_logs',
          'log_miner_runs',
          'log_miner_leases',
          'log_miner_embedding_cache',
          'log_miner_candidates',
          'log_miner_candidate_members'
      ]) AS required(required_name)
     WHERE to_regclass(required_name) IS NULL;
    IF missing_names IS NOT NULL THEN
        RAISE EXCEPTION 'missing log-miner tables: %', missing_names;
    END IF;

    SELECT array_agg(required_column)
      INTO missing_names
      FROM (VALUES
          ('golden_datasets', 'source_fingerprint'),
          ('golden_datasets', 'provenance'),
          ('production_interaction_logs', 'redacted_prompt'),
          ('production_interaction_logs', 'min_arbitration_score'),
          ('production_interaction_logs', 'negative_feedback'),
          ('log_miner_runs', 'duplicate_count'),
          ('log_miner_runs', 'purged_count'),
          ('log_miner_candidates', 'member_trace_ids'),
          ('log_miner_candidates', 'provenance'),
          ('log_miner_candidate_members', 'trace_id')
      ) AS required(table_name, required_column)
     WHERE NOT EXISTS (
          SELECT 1
            FROM information_schema.columns AS column_record
           WHERE column_record.table_schema = 'public'
             AND column_record.table_name = required.table_name
             AND column_record.column_name = required.required_column
     );
    IF missing_names IS NOT NULL THEN
        RAISE EXCEPTION 'missing log-miner columns: %', missing_names;
    END IF;

    SELECT array_agg(required_name)
      INTO missing_names
      FROM unnest(ARRAY[
          'production_interaction_feature_scope_nonblank',
          'production_interaction_prompt_nonblank',
          'production_interaction_score_bounds',
          'log_miner_run_status',
          'log_miner_lease_ownership',
          'log_miner_embedding_width',
          'log_miner_candidate_confidence',
          'log_miner_candidate_status'
      ]) AS required(required_name)
     WHERE NOT EXISTS (
          SELECT 1 FROM pg_constraint WHERE conname = required.required_name
     );
    IF missing_names IS NOT NULL THEN
        RAISE EXCEPTION 'missing log-miner constraints: %', missing_names;
    END IF;

    SELECT array_agg(required_name)
      INTO missing_names
      FROM unnest(ARRAY[
          'golden_datasets_source_fingerprint_uq',
          'production_interaction_logs_eligible_idx',
          'production_interaction_logs_prompt_fingerprint_idx',
          'log_miner_embedding_cache_prompt_idx',
          'log_miner_candidates_review_idx',
          'log_miner_candidates_representative_trace_idx',
          'log_miner_candidate_members_trace_idx'
      ]) AS required(required_name)
     WHERE to_regclass(required_name) IS NULL;
    IF missing_names IS NOT NULL THEN
        RAISE EXCEPTION 'missing log-miner indexes: %', missing_names;
    END IF;
END $$;
SQL

echo "[setup] seeding deterministic autopilot experiment"
compose exec -T postgres psql \
  --username "$POSTGRES_USER" \
  --dbname "$POSTGRES_DB" \
  --set ON_ERROR_STOP=1 <<'SQL'
INSERT INTO feature_experiments (
    flag_name,
    rollout_percentage,
    quality_threshold_p10,
    baseline_prompt_version,
    experimental_prompt_version,
    status
) VALUES (
    'cost_autopilot_routing', 100, 0.80, 1, 2, 'running'
)
ON CONFLICT (flag_name) DO UPDATE SET
    rollout_percentage = EXCLUDED.rollout_percentage,
    quality_threshold_p10 = EXCLUDED.quality_threshold_p10,
    baseline_prompt_version = EXCLUDED.baseline_prompt_version,
    experimental_prompt_version = EXCLUDED.experimental_prompt_version,
    status = EXCLUDED.status,
    updated_at = TIMEZONE('utc', NOW());
SQL

echo "[setup] building and starting the complete stack"
compose up --detach --build --wait --wait-timeout 600

echo "[1/7] gateway, frontend, runtime proxy, Redis admission, and experiment routing"
wait_for_http "${GATEWAY_URL}/healthz"
wait_for_http "${GATEWAY_URL}/health"
wait_for_http "${FRONTEND_URL}/"
curl --silent --show-error --fail "${GATEWAY_URL}/health" \
  --output "${WORK_DIR}/runtime-health.json"
"$PYTHON_BIN" - "${WORK_DIR}/runtime-health.json" <<'PY'
import json
import sys

with open(sys.argv[1], encoding="utf-8") as handle:
    payload = json.load(handle)
assert payload == {"status": "ok", "service": "coremesh-runtime"}, payload
PY

curl --silent --show-error --fail \
  --dump-header "${WORK_DIR}/routing.headers" \
  --output "${WORK_DIR}/routing.json" \
  --header 'Content-Type: application/json' \
  --header 'X-Team-ID: coremesh-integration-stable' \
  --data '{"model":"gpt-4o-mini","messages":[{"role":"user","content":"CoreMesh routing probe"}],"temperature":0}' \
  "${GATEWAY_URL}/v1/chat/completions"
"$PYTHON_BIN" - "${WORK_DIR}/routing.headers" <<'PY'
import sys

headers = {}
with open(sys.argv[1], encoding="iso-8859-1") as handle:
    for line in handle:
        if ":" in line:
            key, value = line.split(":", 1)
            headers[key.strip().lower()] = value.strip()
assert headers.get("x-coremesh-experiment-variant") == "experimental", headers
assert headers.get("x-coremesh-prompt-version") == "2", headers
remaining = headers.get("x-ratelimit-remaining", "")
assert remaining.isdigit() and int(remaining) >= 0, headers
assert headers.get("x-coremesh-route") == "primary", headers
PY
RATE_LIMIT_STATE="$(compose exec -T redis redis-cli EXISTS \
  'coremesh:gateway:ratelimit:coremesh-integration-stable' | tr -d '\r\n ')"
[[ "$RATE_LIMIT_STATE" == "1" ]]

echo "[2/7] invoice ingestion with opt-in RAG indexing"
curl --silent --show-error --fail \
  --header 'X-Team-ID: coremesh-integration' \
  --form 'file=@services-runtime/fixtures/synthetic_invoice.png;type=image/png' \
  --form 'index_for_rag=true' \
  --output "${WORK_DIR}/ingest.json" \
  "${GATEWAY_URL}/v1/ingest"
DOCUMENT_ID="$($PYTHON_BIN - "${WORK_DIR}/ingest.json" <<'PY'
import json
import re
import sys

with open(sys.argv[1], encoding="utf-8") as handle:
    payload = json.load(handle)
extraction = payload["extraction"]
assert extraction["vendor_name"] and extraction["vendor_name"] != "UNKNOWN", payload
assert extraction["invoice_id"] and extraction["invoice_id"] != "UNKNOWN", payload
assert extraction["line_items"], payload
assert payload["validation"]["passed"] is True, payload
assert payload["page_count"] >= 1, payload
assert isinstance(payload["processing_time_ms"], (int, float)), payload
rag_index = payload["rag_index"]
assert re.fullmatch(r"[0-9a-f]{64}", rag_index["document_id"]), payload
assert rag_index["chunk_count"] >= 1, payload
sys.stdout.write(rag_index["document_id"])
PY
)"

echo "[3/7] hybrid RAG retrieval with dense and sparse provenance"
cat >"${WORK_DIR}/rag-request.json" <<'JSON'
{
  "user_id": "integration-user",
  "feature_scope": "rag",
  "payload_query": "Find the Acme Corp Ltd Software License invoice details.",
  "session_context": {
    "session_id": "integration-rag",
    "rag_top_k": 5
  }
}
JSON
curl --silent --show-error --fail \
  --header 'Content-Type: application/json' \
  --header 'X-Team-ID: coremesh-integration' \
  --data-binary "@${WORK_DIR}/rag-request.json" \
  --output "${WORK_DIR}/rag.json" \
  "${GATEWAY_URL}/v1/execute"
"$PYTHON_BIN" - "${WORK_DIR}/rag.json" "$DOCUMENT_ID" <<'PY'
import json
import sys

with open(sys.argv[1], encoding="utf-8") as handle:
    payload = json.load(handle)
document_id = sys.argv[2]
assert payload["status"] == "completed", payload
assert payload["feature_scope"] == "rag", payload
assert payload["arbitration"]["status"] == "passed", payload
observation = next(item for item in payload["observations"] if item["specialist"] == "rag_search")
assert observation["status"] == "success", payload
results = observation["output"]["results"]
matching = [item for item in results if item.get("metadata", {}).get("document_id") == document_id]
assert matching, payload
result = matching[0]
assert result["source"] == "synthetic_invoice.png", result
assert isinstance(result["dense_rank"], int) and result["dense_rank"] >= 1, result
assert isinstance(result["sparse_rank"], int) and result["sparse_rank"] >= 1, result
for name in ("chunk_id", "reference_marker", "score", "rrf_score", "rerank_score"):
    assert result.get(name) is not None, result
PY

echo "[4/7] guardrailed SQL execution against PostgreSQL"
cat >"${WORK_DIR}/sql-request.json" <<'JSON'
{
  "user_id": "integration-user",
  "feature_scope": "text_to_sql",
  "payload_query": "Count rows in the database.",
  "session_context": {
    "session_id": "integration-sql"
  }
}
JSON
curl --silent --show-error --fail \
  --header 'Content-Type: application/json' \
  --header 'X-Team-ID: coremesh-integration' \
  --data-binary "@${WORK_DIR}/sql-request.json" \
  --output "${WORK_DIR}/sql.json" \
  "${GATEWAY_URL}/v1/execute"
"$PYTHON_BIN" - "${WORK_DIR}/sql.json" <<'PY'
import json
import re
import sys

with open(sys.argv[1], encoding="utf-8") as handle:
    payload = json.load(handle)
assert payload["status"] == "completed", payload
assert payload["arbitration"]["status"] == "passed", payload
observation = next(item for item in payload["observations"] if item["specialist"] == "sql_generation")
assert observation["status"] == "success", payload
output = observation["output"]
assert re.match(r"^SELECT COUNT\(\*\) AS row_count FROM ", output["sql"], re.I), output
assert output["sql"].upper().endswith("LIMIT 1000"), output
assert output["columns"] == ["row_count"], output
assert output["row_count"] == 1, output
assert output["rows"][0]["row_count"] >= 0, output
assert output["limit_applied"] is True, output
assert output["schema_tables"], output
PY

echo "[5/7] deterministic skipped-document arbitration failure and forensic trace"
cat >"${WORK_DIR}/failure-request.json" <<'JSON'
{
  "user_id": "integration-user",
  "feature_scope": "agent_orchestrator",
  "payload_query": "Extract invoice data.",
  "session_context": {
    "session_id": "integration-arbitration"
  }
}
JSON
curl --silent --show-error --fail \
  --header 'Content-Type: application/json' \
  --header 'X-Team-ID: coremesh-integration' \
  --data-binary "@${WORK_DIR}/failure-request.json" \
  --output "${WORK_DIR}/failure.json" \
  "${GATEWAY_URL}/v1/execute"
TRACE_ID="$($PYTHON_BIN - "${WORK_DIR}/failure.json" <<'PY'
import json
import re
import sys

with open(sys.argv[1], encoding="utf-8") as handle:
    payload = json.load(handle)
assert payload["status"] == "blocked_by_arbitration", payload
assert len(payload["plan"]) == 1, payload
assert payload["plan"][0]["specialist"] == "document_extraction", payload
assert payload["plan"][0]["status"] == "skipped", payload
assert len(payload["observations"]) == 1, payload
observation = payload["observations"][0]
assert observation["status"] == "skipped" and observation["error"] is None, payload
assert "No document_text" in observation["output"]["reason"], payload
verdict = payload["arbitration"]
assert verdict["status"] == "blocked", payload
assert verdict["delivery_allowed"] is False, payload
assert verdict["adjudication_required"] is True, payload
scores = {item["evaluation_dimension"]: item["assigned_score"] for item in verdict["critic_assessments"]}
assert scores == {"factual": 5, "logic": 5, "completeness": 2}, payload
assert "completeness_score_below_4" in verdict["triggered_by"], payload
assert verdict["adjudication"]["overall_quality_score"] == 2, payload
assert payload["final_response"] == "I could not safely deliver that response because the arbitration layer flagged it for review.", payload
assert re.fullmatch(r"[0-9a-f]{32}", payload["trace_id"]), payload
sys.stdout.write(payload["trace_id"])
PY
)"

for _ in $(seq 1 30); do
  if curl --silent --show-error --fail \
    --output "${WORK_DIR}/trace.json" \
    "${GATEWAY_URL}/v1/traces/${TRACE_ID}"; then
    break
  fi
  sleep 1
done
test -s "${WORK_DIR}/trace.json"
"$PYTHON_BIN" - "${WORK_DIR}/trace.json" <<'PY'
import json
import sys

with open(sys.argv[1], encoding="utf-8") as handle:
    payload = json.load(handle)
assert payload["trigger"] == "arbitration_failure", payload
assert "completeness_score_below_4" in payload["trigger_reasons"], payload
assert payload["diagnosis"]["category"] == "arbitration_failure", payload
PY

ELIGIBLE_ROW="$(compose exec -T postgres psql \
  --username "$POSTGRES_USER" \
  --dbname "$POSTGRES_DB" \
  --tuples-only --no-align --field-separator '|' \
  --command "SELECT arbitration_status, min_arbitration_score, (negative_feedback OR min_arbitration_score < 4) FROM production_interaction_logs WHERE trace_id = '${TRACE_ID}'")"
ELIGIBLE_ROW="$(printf '%s' "$ELIGIBLE_ROW" | tr -d '\r\n ')"
[[ "$ELIGIBLE_ROW" == "blocked|2|t" ]]

echo "[6/7] PostgreSQL-backed log-miner schema and eligible-row health check"
compose exec -T log-miner python -m src.log_miner.extractor check \
  >"${WORK_DIR}/miner-check.json"
"$PYTHON_BIN" - "${WORK_DIR}/miner-check.json" <<'PY'
import json
import sys

with open(sys.argv[1], encoding="utf-8") as handle:
    payload = json.load(handle)
assert payload["status"] == "ok", payload
assert payload["schema"] == "ready", payload
assert payload["eligible_count"] >= 1, payload
PY

echo "[7/7] semantic cache miss, exact repeat hit, and observability counters"
CACHE_NONCE="${PROJECT_NAME}-${RANDOM}"
curl --silent --show-error --fail \
  --output "${WORK_DIR}/observability-before.json" \
  "${GATEWAY_URL}/v1/observability"
cat >"${WORK_DIR}/cache-request.json" <<JSON
{
  "model": "gpt-4o-mini",
  "messages": [
    {"role": "user", "content": "CoreMesh cache probe ${CACHE_NONCE}"}
  ],
  "temperature": 0
}
JSON
curl --silent --show-error --fail \
  --dump-header "${WORK_DIR}/cache-first.headers" \
  --output "${WORK_DIR}/cache-first.json" \
  --header 'Content-Type: application/json' \
  --header 'X-Team-ID: coremesh-integration-cache' \
  --data-binary "@${WORK_DIR}/cache-request.json" \
  "${GATEWAY_URL}/v1/chat/completions"
curl --silent --show-error --fail \
  --dump-header "${WORK_DIR}/cache-second.headers" \
  --output "${WORK_DIR}/cache-second.json" \
  --header 'Content-Type: application/json' \
  --header 'X-Team-ID: coremesh-integration-cache' \
  --data-binary "@${WORK_DIR}/cache-request.json" \
  "${GATEWAY_URL}/v1/chat/completions"
"$PYTHON_BIN" - "${WORK_DIR}/cache-first.headers" "${WORK_DIR}/cache-second.headers" <<'PY'
import sys

def headers(path):
    result = {}
    with open(path, encoding="iso-8859-1") as handle:
        for line in handle:
            if ":" in line:
                key, value = line.split(":", 1)
                result[key.strip().lower()] = value.strip()
    return result

first, second = headers(sys.argv[1]), headers(sys.argv[2])
assert first.get("x-coremesh-cache") == "miss", first
assert second.get("x-coremesh-cache") == "hit", second
PY
cmp -s "${WORK_DIR}/cache-first.json" "${WORK_DIR}/cache-second.json"
curl --silent --show-error --fail \
  --output "${WORK_DIR}/observability.json" \
  "${GATEWAY_URL}/v1/observability"
"$PYTHON_BIN" - \
  "${WORK_DIR}/observability-before.json" \
  "${WORK_DIR}/observability.json" <<'PY'
import json
import sys

with open(sys.argv[1], encoding="utf-8") as handle:
    before = json.load(handle)
with open(sys.argv[2], encoding="utf-8") as handle:
    after = json.load(handle)
before_cache = before["semantic_cache"]
after_cache = after["semantic_cache"]
assert after_cache["enabled"] is True, after
assert after_cache["hits"] == before_cache["hits"] + 1, (before, after)
assert after_cache["misses"] == before_cache["misses"] + 1, (before, after)
assert after_cache["hit_rate"] is not None, after
PY

echo "All seven CoreMesh integration stages passed."
