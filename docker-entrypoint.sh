#!/bin/sh
# One container, N worker processes + the web process.
#
# Each worker claims jobs atomically from PocketBase (unique `job_leases`
# constraint), so N processes cooperate safely — no config, no leader.
# Scale throughput with WORKERS (each process gets its own event loop, which
# matters because the PocketBase SDK is synchronous and blocks the loop).
set -e

: "${WORKERS:=4}"
echo "entrypoint: starting ${WORKERS} worker process(es) + web"

i=0
while [ "$i" -lt "$WORKERS" ]; do
  python -m app.workers.worker &
  i=$((i + 1))
done

# ponytail: web is PID 1, workers are not signal-forwarded — a restart drops
# in-flight jobs, which the lease expiry (LEASE_SECONDS) recovers.
exec uvicorn app.main:app --host 0.0.0.0 --port 8000 --proxy-headers --forwarded-allow-ips '*'
