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
    """Stand-in for `python` / `uvicorn` that records TERM delivery."""
    path = bindir / name
    path.write_text(
        "#!/bin/sh\n"
        f'echo "{name} up worker_id=${{WORKER_ID:-none}}" >> "{log}"\n'
        f'trap \'echo "{name} term worker_id=${{WORKER_ID:-none}}" >> "{log}"; exit 0\' TERM INT\n'
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
        _wait_for(log, "uvicorn up")
        proc.send_signal(signal.SIGTERM)
        assert proc.wait(timeout=10) == 0  # a requested stop is a clean exit
    finally:
        if proc.poll() is None:
            proc.kill()

    text = log.read_text()
    assert "worker_id=worker-1" in text
    assert text.count("term worker_id=worker-") == 2  # both workers, not just PID 1
    assert "uvicorn term" in text
