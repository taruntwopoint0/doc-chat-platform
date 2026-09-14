"""Start the local development Postgres (pgserver) and print its URL.

    python scripts/dev_db.py            # start, print DATABASE_URL
    python scripts/dev_db.py --write    # also point .env at it

The cluster lives in ./.pgdata inside the project, so your documents survive
restarts. pgserver ships its own Postgres with pgvector, which is what makes
this work on a machine with no Docker and no Postgres installed.

A server killed without a clean shutdown leaves a stale postmaster.pid that
pgserver refuses to start over. This script clears it -- but only after
confirming no Postgres process is actually running on that cluster, so it can
never pull the lock out from under a live server.
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DATA_DIR = ROOT / ".pgdata"
ENV_FILE = ROOT / ".env"


def _pid_is_running(pid: int) -> bool:
    try:
        import psutil
    except ImportError:  # pragma: no cover - pgserver depends on psutil
        return True  # cannot tell, so assume live and leave the lock alone
    return psutil.pid_exists(pid)


def _clear_stale_lock() -> None:
    lock = DATA_DIR / "postmaster.pid"
    if not lock.exists():
        return
    first_line = lock.read_text(encoding="utf-8", errors="replace").splitlines()[:1]
    try:
        pid = int(first_line[0]) if first_line else -1
    except ValueError:
        pid = -1
    if pid > 0 and _pid_is_running(pid):
        print(f"Postgres already running (pid {pid}); leaving the lock alone.")
        return
    lock.unlink()
    print("Cleared a stale postmaster.pid left by an unclean shutdown.")


def _wait_for_ready(timeout_seconds: int = 120) -> str:
    """Poll the server log until recovery finishes, then build the URL from the
    port Postgres wrote into postmaster.pid."""
    import time

    log = DATA_DIR / "log"
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        # Only the recent tail matters: older runs' lines are in the same file.
        text = log.read_text(encoding="utf-8", errors="replace") if log.exists() else ""
        tail = text[-800:]
        if "ready to accept connections" in tail:
            break
        if "FATAL" in tail or "PANIC" in tail:
            raise SystemExit(f"Postgres failed to start; see {log}")
        time.sleep(2)
    else:
        raise SystemExit(
            f"Postgres did not become ready within {timeout_seconds}s; see {log}"
        )

    # postmaster.pid line 4 is the port the server is listening on.
    lines = (DATA_DIR / "postmaster.pid").read_text(encoding="utf-8").splitlines()
    port = lines[3].strip()
    return f"postgresql://postgres:@127.0.0.1:{port}/postgres"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--write", action="store_true", help="update DATABASE_URL in .env"
    )
    args = parser.parse_args()

    try:
        import pgserver
    except ImportError:
        print("pgserver is not installed: pip install pgserver", file=sys.stderr)
        return 1

    DATA_DIR.mkdir(parents=True, exist_ok=True)
    _clear_stale_lock()
    try:
        uri = pgserver.get_server(DATA_DIR, cleanup_mode=None).get_uri()
    except Exception as exc:
        # After an unclean shutdown Postgres replays its WAL before accepting
        # connections. On Windows that replay also collides with pgserver's own
        # log file (kept inside the data dir), costing a 30-second retry --
        # longer than pgserver's hard 10-second start timeout. The server is
        # still coming up, so wait for it instead of failing.
        print(f"pgserver gave up waiting ({type(exc).__name__}); "
              "Postgres is likely still in crash recovery. Waiting...")
        uri = _wait_for_ready()
    print(f"DATABASE_URL={uri}")

    if args.write and ENV_FILE.exists():
        text = ENV_FILE.read_text(encoding="utf-8")
        updated, count = re.subn(
            r"^DATABASE_URL=.*$", f"DATABASE_URL={uri}", text, flags=re.M
        )
        if count:
            ENV_FILE.write_text(updated, encoding="utf-8")
            print("Updated .env")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
