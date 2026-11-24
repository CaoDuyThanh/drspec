"""Checks of the REAL embedding model, run by a person when the model, its library or the wrapper changes (not by the test suite).

These four checks used to be tests/unit/test_embedder.py. They test a third-party model and need its weights: the first call downloads about
90 MB from the internet into the model folder (`DRSPEC_MODEL_DIR`, else the per-user cache), so they depend on the network and the machine and
cannot be part of a self-contained test run. What is OURS (the wrapper's dimension, its single loading, its folder) is tested in
tests/unit/test_embedder.py on a fake `fastembed`.

    python3 scripts/check_real_embedder.py
"""

from __future__ import annotations

import os
import subprocess
import sys
import tempfile


def main() -> int:
    from drspec.core.embedder import EMBEDDING_DIM, embed_text

    failures = []

    def check(name, ok, detail=""):
        print(("PASS " if ok else "FAIL ") + name + (f": {detail}" if detail and not ok else ""))
        if not ok:
            failures.append(name)

    vector = embed_text("def foo(): return 1")
    check("returns 384 floats", len(vector) == EMBEDDING_DIM == 384 and all(isinstance(v, float) for v in vector))

    text = "def charge_card(amount): return gateway.charge(amount)"
    check("is deterministic", embed_text(text) == embed_text(text))

    long_body = "def foo():\n" + "    x = 1\n" * 2000  # thousands of tokens: the tokenizer truncates at 256, it does not error
    check("long input still returns a valid vector", len(embed_text(long_body)) == EMBEDDING_DIM)

    script = (
        "from drspec.core.embedder import embed_text, EMBEDDING_DIM\n"
        "v = embed_text('offline check')\n"
        "assert len(v) == EMBEDDING_DIM\n"
        "print('OK')\n"
    )
    with tempfile.TemporaryDirectory() as folder:  # a scratch folder as working folder: the model cache does not land in the repository
        done = subprocess.run(
            [sys.executable, "-c", script], cwd=folder, env={**os.environ, "HF_HUB_OFFLINE": "1"}, capture_output=True, text=True, timeout=120
        )
    check("a second process needs no network once the model is cached", done.returncode == 0, done.stderr[-300:])

    print("all passed" if not failures else f"{len(failures)} failed")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
