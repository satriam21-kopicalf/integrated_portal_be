#!/usr/bin/env bash
# Deploy integrated_portal_be to the VPS with Docker Compose.
#
# Usage:  ./scripts/deploy.sh            (from the repo root)
# Env:    VPS_HOST (default root@187.52.114.14), VPS_PATH (default /opt/integrated-portal-be)
#
# The server-side .env is never overwritten. On the first deploy it is created
# from the DB_* credentials already used by /opt/esb-integration/.env.
set -euo pipefail

VPS_HOST="${VPS_HOST:-root@187.52.114.14}"
VPS_PATH="${VPS_PATH:-/opt/integrated-portal-be}"

cd "$(dirname "$0")/.."

echo "==> Uploading source to ${VPS_HOST}:${VPS_PATH}"
ssh "$VPS_HOST" "mkdir -p '$VPS_PATH'"
tar czf - --exclude=__pycache__ app requirements.txt Dockerfile docker-compose.yml .dockerignore .env.example \
  | ssh "$VPS_HOST" "rm -rf '$VPS_PATH/app' && tar xzf - -C '$VPS_PATH'"

echo "==> Building and starting container"
ssh "$VPS_HOST" VPS_PATH="$VPS_PATH" 'bash -s' <<'REMOTE'
set -euo pipefail
cd "$VPS_PATH"
if [ ! -f .env ]; then
  echo "    creating .env from /opt/esb-integration/.env"
  {
    grep -E '^DB_(HOST|PORT|NAME|USER|PASSWORD)=' /opt/esb-integration/.env
    echo "DB_SSLMODE=require"
    echo "PORT=8002"
    echo "CORS_ORIGINS=*"
    echo "TIMEZONE=Asia/Jakarta"
  } > .env
  chmod 600 .env
fi
docker compose up -d --build
docker image prune -f >/dev/null

echo "==> Waiting for health check"
for i in $(seq 1 30); do
  if curl -fsS http://127.0.0.1:8002/health; then
    echo
    docker exec integrated-portal-be python -m app.migrate
    exit 0
  fi
  sleep 2
done
echo "Health check failed" >&2
docker compose logs --tail 50
exit 1
REMOTE

echo "==> Deployed: http://${VPS_HOST#*@}:8002  (docs: /docs)"
