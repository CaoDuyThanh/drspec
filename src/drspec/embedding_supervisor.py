"""The worker's side of background embedding.

Each worker pass folds new usage into scores and, when something is owed an
embedding, starts the throttled embedding process (at most one at a time).
A process that cannot get the model, or that makes no progress, is retried
after a growing wait instead of on every pass.
"""

from __future__ import annotations

import os
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Callable, Optional

from drspec.core import tracing
from drspec.core.discovery import project_root_for
from drspec.core.embedding_backlog import find_backlog
from drspec.core.embedding_runner import EXIT_MODEL_UNAVAILABLE, EXIT_NOT_PERSISTED, write_state
from drspec.db.queries import count_usage_pending, fold_usage_pending

# What the last embedding process printed, for finding out why one failed.
LOG_NAME = "embedding.log"
FIRST_RETRY_SECONDS = 60.0
MAX_RETRY_SECONDS = 900.0


# Thread pools the model library's own `threads` option does not reach: the
# OpenMP, BLAS and tokenizer pools each start one thread per core by default.
THREAD_ENVIRONMENT = (
    "OMP_NUM_THREADS",
    "OPENBLAS_NUM_THREADS",
    "MKL_NUM_THREADS",
    "NUMEXPR_NUM_THREADS",
    "VECLIB_MAXIMUM_THREADS",
    "RAYON_NUM_THREADS",
)


def embedding_environment(threads: int, base: Optional[dict] = None) -> dict:
    """The environment for the embedding process, with every thread pool it
    could start held to `threads`."""
    environment = dict(os.environ if base is None else base)
    for name in THREAD_ENVIRONMENT:
        environment[name] = str(threads)
    environment["TOKENIZERS_PARALLELISM"] = "false"
    return environment


def spawn_embedding_process(db_path: Path, threads: int = 1) -> subprocess.Popen:
    """Start the embedding process with the project root as its working
    directory, which is where the model cache lives."""
    root = project_root_for(db_path)
    with open(Path(db_path).parent / LOG_NAME, "w") as log:
        return subprocess.Popen(
            [sys.executable, "-m", "drspec", "--db", str(db_path), "worker", "embed"],
            stdout=log,
            stderr=subprocess.STDOUT,
            stdin=subprocess.DEVNULL,
            cwd=str(root) if root is not None and root.exists() else None,
            env=embedding_environment(threads),
        )


class EmbeddingSupervisor:
    def __init__(self, spawn: Callable[..., Any] = spawn_embedding_process) -> None:
        self._spawn = spawn
        self.process: Optional[Any] = None
        self.failures = 0
        self.retry_at = 0.0
        self._owed_at_spawn = 0
        self._started_at = 0.0

    def _back_off(self, now: float, reason: str) -> None:
        self.failures += 1
        wait = min(MAX_RETRY_SECONDS, FIRST_RETRY_SECONDS * 2 ** (self.failures - 1))
        self.retry_at = now + wait
        tracing.emit("supervisor", kind="backoff", reason=tracing.code_of(reason), wait_s=wait, failures=self.failures)

    def tick(self, conn: Any, db_path: Path, settings: dict[str, Any], now: Optional[float] = None) -> None:
        """Fold usage, reap a finished process, and start one if work is owed."""
        now = time.time() if now is None else now
        if count_usage_pending(conn):
            fold_usage_pending(conn, now, settings["embedding.half_life_days"])

        finished = False
        if self.process is not None:
            code = self.process.poll()
            if code is None:
                return
            self.process = None
            finished = True
            tracing.emit("supervisor", (time.monotonic() - self._started_at) * 1000.0, kind="exit", code=code)
            if code != 0:
                # These two exits have already written their own state.
                if code not in (EXIT_MODEL_UNAVAILABLE, EXIT_NOT_PERSISTED):
                    write_state(conn, "error", f"embedding process exited with code {code} (see {LOG_NAME})")
                reason = {EXIT_MODEL_UNAVAILABLE: "model unavailable", EXIT_NOT_PERSISTED: "vector did not persist"}.get(code, "exit code")
                self._back_off(now, reason)
            else:
                self.failures = 0

        if not settings["embedding.enabled"] or now < self.retry_at:
            return
        owed = len(find_backlog(conn, settings["embedding.top_n"], settings["embedding.half_life_days"], now))
        if not owed:
            return
        if finished and self.failures == 0 and owed >= self._owed_at_spawn:
            # It ran to completion and still left as much behind: no progress.
            self._back_off(now, f"no progress: {owed} still owed")
            return
        self._owed_at_spawn = owed
        self.process = self._spawn(db_path, threads=settings["embedding.threads"])
        self._started_at = time.monotonic()
        tracing.emit("supervisor", kind="start", owed=owed, child_pid=getattr(self.process, "pid", None))

    def stop(self) -> None:
        process, self.process = self.process, None
        if process is None:
            return
        try:
            process.terminate()
            process.wait(timeout=5)
        except Exception:  # noqa: BLE001 - shutting down; best effort
            try:
                process.kill()
            except Exception:  # noqa: BLE001
                pass
