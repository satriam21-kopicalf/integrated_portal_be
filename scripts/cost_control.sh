#!/usr/bin/env bash
# Rebuild the Cost Control aggregates inside the running container (serialised with flock).
#   scripts/cost_control.sh --recent-days 10
#   scripts/cost_control.sh --from 2025-11-01
set -euo pipefail
exec flock -w 7200 /tmp/integrated-portal-cost-control.lock \
  docker exec integrated-portal-be python -m app.cost_control "$@"
