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
#   * a worker slot is resupervised: if a worker process exits (a crash, an OOM
#     kill, a bad deploy), it is restarted in place. Without this the web kept
#     serving while every worker stayed dead — no job runs, every schedule goes
#     late and retried jobs only queue, with nothing in the UI explaining why.
# A WORKER_ID set for the container is deliberately ignored — one shared id
# would make every slot fight over a single heartbeat row.
set -e

: "${WORKERS:=4}"
: "${WORKER_RESTART_DELAY:=5}"
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

# One supervised slot. A clean exit (status 0 — the worker handled SIGTERM)
# ends the slot; anything else is a crash and the worker is restarted. On TERM
# the slot forwards the signal and waits for the worker to finish shutting down
# (requeue its job) before exiting, so `docker stop` never cuts it short.
run_slot() {
  slot="$1"
  child=""
  stopping=0
  trap 'stopping=1; if [ -n "$child" ]; then kill -TERM "$child" 2>/dev/null || true; wait "$child" 2>/dev/null || true; fi' TERM INT
  while [ "$stopping" -eq 0 ]; do
    WORKER_ID="worker-$slot" python -m app.workers.worker &
    child=$!
    status=0
    wait "$child" || status=$?
    child=""
    if [ "$stopping" -eq 1 ] || [ "$status" -eq 0 ]; then
      return 0
    fi
    echo "entrypoint: worker-$slot exited with status $status — restarting in ${WORKER_RESTART_DELAY}s" >&2
    sleep "$WORKER_RESTART_DELAY" &
    child=$!
    wait "$child" || true
    child=""
  done
}

i=1
while [ "$i" -le "$WORKERS" ]; do
  run_slot "$i" &
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
