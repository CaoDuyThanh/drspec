"""Entry point for python -m drspec and the standalone executable."""

from __future__ import annotations

import sys

if __name__ == "__main__":
    import multiprocessing

    multiprocessing.freeze_support()  # a frozen executable is also what the parser processes start as
    if getattr(sys, "frozen", False) and len(sys.argv) == 3 and sys.argv[1] == "--drspec-internal-verify":
        # A frozen executable has no separate Python interpreter. Reuse its
        # bundled runtime for the isolated verifier before importing the CLI.
        import runpy

        runpy.run_path(sys.argv[2], run_name="__main__")
    else:
        from drspec.cli.app import app

        app()
