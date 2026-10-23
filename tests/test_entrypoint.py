"""The container entrypoint is what makes a Dokploy redeploy clean:

* every worker slot gets a stable WORKER_ID, so the new process overwrites its
  own `worker_heartbeats` row instead of adding an outdated duplicate;
* SIGTERM reaches the workers, so they can requeue their in-flight jobs instead
  of being SIGKILLed with `running` jobs and a live lease left behind.
"""

from __future__ import annotations

import os
import signal
import stat
import subprocess
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
ENTRYPOINT = ROOT / "docker-entrypoint.sh"


def _stub(bindir: Path, name: str, log: Path) -> None:
    """Stand-in for `python` / `uvicorn` that records TERM delivery.

    `ready` is logged only after the TERM trap is installed, so a test never
    signals a stub that is still starting (the real worker installs its signal
    handlers at the same point in startup).
    """
    path = bindir / name
    path.write_text(
        "#!/bin/sh\n"
        f'echo "{name} up worker_id=${{WORKER_ID:-none}}" >> "{log}"\n'
        f'trap \'echo "{name} term worker_id=${{WORKER_ID:-none}}" >> "{log}"; exit 0\' TERM INT\n'
        f'echo "{name} ready worker_id=${{WORKER_ID:-none}}" >> "{log}"\n'
        "while :; do sleep 0.05; done\n"
    )
    path.chmod(path.stat().st_mode | stat.S_IEXEC)


def _wait_for(log: Path, needle: str, timeout: float = 10.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if needle in log.read_text():
            return
        time.sleep(0.05)
    raise AssertionError(f"entrypoint never logged {needle!r}:\n{log.read_text()}")


def test_entrypoint_labels_slots_and_forwards_term(tmp_path):
    bindir = tmp_path / "bin"
    bindir.mkdir()
    log = tmp_path / "log"
    log.touch()
    for name in ("python", "uvicorn"):
        _stub(bindir, name, log)

    env = {**os.environ, "PATH": f"{bindir}:{os.environ['PATH']}", "WORKERS": "2"}
    proc = subprocess.Popen(
        ["sh", str(ENTRYPOINT)],
        cwd=ROOT,
        env=env,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
    )
    try:
        _wait_for(log, "worker_id=worker-2")
        _wait_for(log, "uvicorn ready")
        _wait_for(log, "python ready worker_id=worker-1")
        _wait_for(log, "python ready worker_id=worker-2")
        proc.send_signal(signal.SIGTERM)
        assert proc.wait(timeout=10) == 0  # a requested stop is a clean exit
    finally:
        if proc.poll() is None:
            proc.kill()

    # Signal delivery is asynchronous: a stub handles TERM on its next `sleep`
    # wake-up, which can be after the entrypoint itself has exited.
    _wait_for(log, "python term worker_id=worker-1")
    _wait_for(log, "python term worker_id=worker-2")
    text = log.read_text()
    assert "worker_id=worker-1" in text
    assert text.count("term worker_id=worker-") == 2  # both workers, not just PID 1
    assert "uvicorn term" in text


def test_entrypoint_restarts_a_crashed_worker(tmp_path):
    """A worker that exits must come back in place.

    The web process anchors the container, so without a supervisor a crashed
    worker stays dead while the app keeps serving: no job runs, every schedule
    goes late and retries only queue.
    """
    bindir = tmp_path / "bin"
    bindir.mkdir()
    log = tmp_path / "log"
    log.touch()
    marker = tmp_path / "crashed-once"
    # First start crashes immediately; later starts stay up until TERMed.
    python = bindir / "python"
    python.write_text(
        "#!/bin/sh\n"
        f'echo "worker up worker_id=${{WORKER_ID:-none}}" >> "{log}"\n'
        f'if [ ! -e "{marker}" ]; then touch "{marker}"; echo "worker crash" >> "{log}"; exit 7; fi\n'
        f'trap \'echo "worker term worker_id=${{WORKER_ID:-none}}" >> "{log}"; exit 0\' TERM INT\n'
        f'echo "worker ready worker_id=${{WORKER_ID:-none}}" >> "{log}"\n'
        "while :; do sleep 0.05; done\n"
    )
    python.chmod(python.stat().st_mode | stat.S_IEXEC)
    _stub(bindir, "uvicorn", log)

    env = {
        **os.environ,
        "PATH": f"{bindir}:{os.environ['PATH']}",
        "WORKERS": "1",
        "WORKER_RESTART_DELAY": "0.1",
    }
    proc = subprocess.Popen(
        ["sh", str(ENTRYPOINT)],
        cwd=ROOT,
        env=env,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
    )
    try:
        _wait_for(log, "worker crash")
        _wait_for(log, "worker ready worker_id=worker-1")
        proc.send_signal(signal.SIGTERM)
        assert proc.wait(timeout=10) == 0
    finally:
        if proc.poll() is None:
            proc.kill()
    _wait_for(log, "worker term worker_id=worker-1")


def test_entrypoint_waits_for_the_workers_graceful_shutdown(tmp_path):
    """`docker stop` must not cut a worker off mid-requeue.

    The slot supervises its worker, so the entrypoint only exits after the slot
    has waited for the worker to finish handling SIGTERM.
    """
    bindir = tmp_path / "bin"
    bindir.mkdir()
    log = tmp_path / "log"
    log.touch()
    for name in ("python", "uvicorn"):
        delay = "0.4" if name == "python" else "0"
        path = bindir / name
        path.write_text(
            "#!/bin/sh\n"
            f'echo "{name} up" >> "{log}"\n'
            f'trap \'echo "{name} term" >> "{log}"; sleep {delay}; exit 0\' TERM INT\n'
            "while :; do sleep 0.02; done\n"
        )
        path.chmod(path.stat().st_mode | stat.S_IEXEC)

    env = {**os.environ, "PATH": f"{bindir}:{os.environ['PATH']}", "WORKERS": "1"}
    proc = subprocess.Popen(
        ["sh", str(ENTRYPOINT)],
        cwd=ROOT,
        env=env,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        _wait_for(log, "python up")
        started = time.monotonic()
        proc.send_signal(signal.SIGTERM)
        assert proc.wait(timeout=10) == 0
        elapsed = time.monotonic() - started
    finally:
        if proc.poll() is None:
            proc.kill()
    assert elapsed >= 0.3, f"entrypoint did not wait for the worker shutdown ({elapsed:.2f}s)"


def test_worker_startup_retries_while_pocketbase_is_unreachable(monkeypatch):
    """A PB hiccup at boot must not leave the worker pool dead for good."""
    import app.workers.worker as W

    attempts: list[int] = []

    class Boom(Exception):
        pass

    def flaky() -> str:
        attempts.append(1)
        if len(attempts) < 3:
            raise Boom("PocketBase is still starting")
        return "admin-client"

    monkeypatch.setattr(W, "get_admin_pb", flaky)
    monkeypatch.setattr(W.time, "sleep", lambda _delay: None)

    assert W.admin_pb_with_retry() == "admin-client"
    assert len(attempts) == 3
