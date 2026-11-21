#!/usr/bin/env bash
# Keep the crew repo's dev branch level with upstream Archon, so issues filed today are worked against
# today's code. The crew repo is a standalone copy (not a fork, so nothing in it can link to upstream):
# fetch upstream dev, push it to the crew repo's dev, fast-forward the local clone. Runs hourly.
CLONE=${CREW_FORK_DIR:-/c/Users/ASUS/proart-crew/archon-local-crew}
while true; do
  git -C "$CLONE" fetch -q upstream dev \
    && git -C "$CLONE" push -q origin refs/remotes/upstream/dev:refs/heads/dev \
    && git -C "$CLONE" fetch -q origin dev && git -C "$CLONE" merge -q --ff-only origin/dev \
    && echo "$(date +%H:%M) synced crew dev to $(git -C "$CLONE" rev-parse --short HEAD)" \
    || echo "$(date +%H:%M) SYNC_FAILED"
  sleep 3600
done
