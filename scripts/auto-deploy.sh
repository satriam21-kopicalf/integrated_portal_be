#!/usr/bin/env bash
# Pull-based auto deploy for integrated_portal_be (runs on the VPS from cron).
#
# Every run: fetch origin/main; when it moved since the last successful deploy,
# run the tests in a throwaway container, build the image, restart the service
# and wait for /health. If the new container is unhealthy, the previous image
# is restored. No secrets are needed in GitHub; the server .env is never touched.
#
# Install (once, on the VPS):
#   git clone https://github.com/satriam21-kopicalf/integrated_portal_be.git /opt/integrated-portal-be/repo
#   cp /opt/integrated-portal-be/repo/scripts/auto-deploy.cron /etc/cron.d/integrated-portal-be-deploy
set -euo pipefail

APP_DIR="${APP_DIR:-/opt/integrated-portal-be}"
REPO_DIR="${REPO_DIR:-$APP_DIR/repo}"
BRANCH="${BRANCH:-main}"
STATE_FILE="$APP_DIR/.deployed-commit"
IMAGE="integrated-portal-be"
log() { echo "$(date -u +%FT%TZ) $*"; }

cd "$REPO_DIR"
git fetch -q origin "$BRANCH"
target=$(git rev-parse "origin/$BRANCH")
current=$(cat "$STATE_FILE" 2>/dev/null || true)
[ "$target" = "$current" ] && exit 0

log "deploying ${current:0:7} -> ${target:0:7}: $(git log -1 --format=%s "$target")"
git reset -q --hard "$target"

log "running tests"
if ! docker run --rm -v "$REPO_DIR":/src:ro -w /src python:3.12-slim \
    sh -c "cp -r /src /tmp/app && cd /tmp/app && pip install -q -r requirements-dev.txt >/dev/null && pytest -q" ; then
  log "tests failed - deploy skipped (stays on ${current:0:7})"
  exit 1
fi

# keep the running image for rollback
docker image inspect "$IMAGE:latest" >/dev/null 2>&1 && docker tag "$IMAGE:latest" "$IMAGE:previous"

cp -r app requirements.txt Dockerfile docker-compose.yml .dockerignore .env.example "$APP_DIR"/
cd "$APP_DIR"
docker compose up -d --build

for _ in $(seq 1 30); do
  if curl -fsS http://127.0.0.1:8002/health >/dev/null 2>&1; then
    echo "$target" > "$STATE_FILE"
    docker image prune -f >/dev/null
    log "deployed ${target:0:7} - healthy"
    exit 0
  fi
  sleep 2
done

log "new version unhealthy - rolling back"
docker compose logs --tail 30 || true
if docker image inspect "$IMAGE:previous" >/dev/null 2>&1; then
  docker tag "$IMAGE:previous" "$IMAGE:latest"
  docker compose up -d --no-build
fi
exit 1
