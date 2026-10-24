#!/usr/bin/env bash
# Bring up the local cluster, send real requests through nginx, tear down. Used by CI; free, no cloud.
set -euo pipefail
cd "$(dirname "$0")/.."
TOKEN=$(python scripts/dev_token.py u-ava)
docker compose up -d --build --wait --wait-timeout 180 || true
trap 'docker compose ps -a; for s in api1 worker-bulk autoscaler; do docker compose logs --no-color --tail 40 $s; done; docker compose down -v' EXIT
for i in $(seq 1 60); do
  if curl -fsS http://localhost:8080/healthz >/dev/null 2>&1; then break; fi
  sleep 2
done
curl -fsS http://localhost:8080/readyz
code=$(curl -s -o /dev/null -w '%{http_code}' -X POST http://localhost:8080/v1/chat -H 'content-type: application/json' -d '{"message":"hi"}')
[ "$code" = "401" ] || { echo "expected 401 without token, got $code"; exit 1; }
out=$(curl -fsS -X POST http://localhost:8080/v1/chat -H "authorization: Bearer $TOKEN" \
  -H 'content-type: application/json' -d '{"message":"where is my order KG-100001"}')
echo "$out"
echo "$out" | grep -q 'KG-100001' || { echo "unexpected chat reply"; exit 1; }
job=$(curl -fsS -X POST http://localhost:8080/v1/refunds -H "authorization: Bearer $TOKEN" \
  -H 'content-type: application/json' -H 'idempotency-key: smoke-refund-0001' -d '{"order_id":"KG-100002","amount":5}')
echo "$job"
echo "$job" | grep -q job_id || { echo "no job id"; exit 1; }
echo "compose smoke passed"
