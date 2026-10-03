#!/usr/bin/env bash
# Rebuild Overview aggregates inside the running container (serialised with flock).
#   scripts/aggregates.sh --recent 2
#   scripts/aggregates.sh --from 2025-08-01 --to 2026-10-02
set -euo pipefail
exec flock -w 7200 /tmp/integrated-portal-aggregates.lock \
  docker exec integrated-portal-be python -m app.aggregates "$@"
