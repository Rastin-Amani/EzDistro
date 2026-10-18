#!/bin/sh
# One container, N worker processes + the web process.
#
# Each worker claims jobs atomically from PocketBase (unique `job_leases`
# constraint), so N processes cooperate safely — no config, no leader.
# Scale throughput with WORKERS (each process gets its own event loop, which
# matters because the PocketBase SDK is synchronous and blocks the loop).
#
# Two things keep a redeploy honest — the new version must never run next to
# workers of the previous one:
#   * every slot has a STABLE worker id, so a fresh process overwrites its own
#     `worker_heartbeats` row instead of adding a second, outdated one to the
#     Workers page (crash-killed processes used to pile up there forever);
#   * SIGTERM is forwarded to the children. `docker stop` only TERMs PID 1
#     (this shell): without the forward the workers are SIGKILLed a few seconds
#     later, so they never requeue their in-flight jobs and those sit `running`
#     until LEASE_SECONDS expires.
# A WORKER_ID set for the container is deliberately ignored — one shared id
# would make every slot fight over a single heartbeat row.
set -e

: "${WORKERS:=4}"
echo "entrypoint: starting ${WORKERS} worker process(es) + web"

pids=""
web_pid=""

stop() {
  trap - TERM INT
  # Unquoted on purpose: word-split into one pid per argument.
  # shellcheck disable=SC2086
  kill -TERM $pids $web_pid 2>/dev/null || true
  wait
  exit 0
}
trap stop TERM INT

i=1
while [ "$i" -le "$WORKERS" ]; do
  WORKER_ID="worker-$i" python -m app.workers.worker &
  pids="$pids $!"
  i=$((i + 1))
done

uvicorn app.main:app --host 0.0.0.0 --port 8000 --proxy-headers --forwarded-allow-ips '*' &
web_pid="$!"
pids="$pids $web_pid"

# Web is the container's lifecycle anchor: if it exits, take the pool down too,
# and keep its exit status so a crashed web is not reported as a clean stop.
web_status=0
wait "$web_pid" || web_status=$?
# shellcheck disable=SC2086
kill -TERM $pids 2>/dev/null || true
wait || true
exit "$web_status"
