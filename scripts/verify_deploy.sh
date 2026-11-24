#!/usr/bin/env bash
# Verify a clean production deployment, end to end, in a disposable Docker
# project (separate containers, network and volumes; your real stack and data
# are not touched):
#
#   1. build the production image and start the stack from empty volumes
#   2. wait for every service to report healthy and the API to be ready
#   3. check the production settings (no source mount, no --reload, docs off,
#      databases not published, no .env inside the image)
#   4. run the end-to-end workflow test (real worker, Whisper, Pyannote, Ollama)
#   5. tear everything down, including the images and volumes it created
#
# Requires: Docker, a populated .env (JWT_SECRET, HF_TOKEN), Ollama running on
# the host. Usage: scripts/verify_deploy.sh   (or: make verify-deploy)
set -uo pipefail
cd "$(dirname "$0")/.."

PROJECT=mia_verify
PORT="${VERIFY_API_PORT:-18000}"
COMPOSE=(docker compose -p "$PROJECT" -f docker-compose.yml -f docker-compose.prod.yml)
export API_HOST_PORT="$PORT"
status=0

step() { printf '\n==> %s\n' "$*"; }
fail() { printf 'FAIL: %s\n' "$*"; status=1; }
cleanup() {
  step "Tearing down (containers, volumes, images)"
  "${COMPOSE[@]}" down -v --rmi local >/dev/null 2>&1 || true
}
trap cleanup EXIT

step "Building and starting a fresh stack"
"${COMPOSE[@]}" up -d --build >/dev/null 2>&1 || { fail "stack failed to start"; exit 1; }

step "Waiting for health (first start downloads models; up to 5 min)"
for _ in $(seq 1 100); do
  api=$(docker inspect -f '{{.State.Health.Status}}' "${PROJECT}-api-1" 2>/dev/null)
  wrk=$(docker inspect -f '{{.State.Health.Status}}' "${PROJECT}-celery-worker-1" 2>/dev/null)
  [ "$api" = healthy ] && [ "$wrk" = healthy ] && break
  sleep 3
done
echo "api=$api worker=$wrk"
[ "$api" = healthy ] && [ "$wrk" = healthy ] || { fail "services not healthy"; "${COMPOSE[@]}" logs --tail 30; exit 1; }

step "Production settings"
ready=$(curl -s -m 5 "localhost:$PORT/health/ready")
echo "ready: $ready"
echo "$ready" | grep -q '"database":"ok"' && echo "$ready" | grep -q '"redis":"ok"' || fail "dependencies not ok"
[ "$(curl -s -o /dev/null -w '%{http_code}' "localhost:$PORT/docs")" = 404 ] || fail "API docs exposed"
[ "$(curl -s -o /dev/null -w '%{http_code}' "localhost:$PORT/api/v1/meetings")" = 401 ] || fail "API not protected"
docker inspect -f '{{.Config.Cmd}}' "${PROJECT}-api-1" | grep -q -- '--reload' && fail "--reload enabled"
docker inspect -f '{{range .Mounts}}{{.Type}} {{end}}' "${PROJECT}-api-1" | grep -q bind && fail "source bind-mounted"
docker ps --format '{{.Names}} {{.Ports}}' | grep -E "^${PROJECT}-(postgres|redis)" | grep -q -- '->' && fail "database published on host"
docker exec "${PROJECT}-api-1" test -e /app/.env && fail ".env baked into image"
[ "$status" = 0 ] && echo "ok"

step "End-to-end workflow"
"${COMPOSE[@]}" run --rm --no-deps -T \
  -v "$PWD/tests:/app/tests:ro" -v "$PWD/pytest.ini:/app/pytest.ini:ro" \
  -e E2E_BASE_URL=http://api:8000 --entrypoint python api \
  -m pytest tests/e2e -v -p no:cacheprovider --no-cov \
  || fail "end-to-end tests failed"

step "Result"
[ "$status" = 0 ] && echo "PASS: clean deployment verified" || echo "FAILED"
exit "$status"
