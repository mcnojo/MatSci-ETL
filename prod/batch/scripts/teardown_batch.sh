#!/usr/bin/env bash
# Tear down the batch fleet.
#
# Phase 3: placeholder — currently a no-op since no fleet exists yet.
# Phase 5: scales cpu+gpu batch ASGs back to zero. Optionally cleans up the
#          manifest if a flag is passed.
#
# Usage:
#   ./prod/batch/scripts/teardown_batch.sh

set -euo pipefail

echo "Phase 3: no batch fleet provisioned yet; nothing to tear down."
echo "Phase 5 will scale prod-batch-cpu and prod-batch-gpu ASGs to zero here."
