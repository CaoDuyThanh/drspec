"""Local text embeddings for the code graph.

Runs entirely locally via `fastembed` (ONNX Runtime) — no PyTorch, no LLM,
no network call once the model weights are cached. The weights are kept once
per user, not once per project (see `shared_cache_dir`).
"""

from __future__ import annotations

import contextlib
import os
import shutil
import sys
import tempfile
from pathlib import Path
from typing import Iterator, Optional

try:
    import fcntl
except ImportError:  # not on Windows
    fcntl = None  # type: ignore[assignment]

MODEL_NAME = "sentence-transformers/all-MiniLM-L6-v2"
EMBEDDING_DIM = 384
MODEL_VERSION = MODEL_NAME + ":fastembed-0.4.2:384:v1"

_model = None  # lazy singleton; loaded once per process on first embed_text() call


MODEL_DIR_ENV_VAR = "DRSPEC_MODEL_DIR"

#: Files a model snapshot needs before it can be loaded without the network.
_SNAPSHOT_FILES = ("model.onnx", "tokenizer.json", "config.json", "tokenizer_config.json", "special_tokens_map.json")

#: Set when the shared folder could not be used and the project's own was taken instead.
_fallback_reason: Optional[str] = None
#: A project-local model folder left by an older version, once its model was moved to the shared one.
_old_project_folder: Optional[Path] = None


def shared_cache_dir() -> Path:
    """The per-user folder the model is kept in, the same for every project: `DRSPEC_MODEL_DIR`, else the
    system's cache folder (`$XDG_CACHE_HOME` or `~/.cache` on Linux, `~/Library/Caches` on macOS,
    `%LOCALAPPDATA%` on Windows)."""
    override = os.environ.get(MODEL_DIR_ENV_VAR)
    if override:
        return Path(override).expanduser()
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Caches" / "drspec" / "models"
    if sys.platform.startswith("win"):
        base = os.environ.get("LOCALAPPDATA") or str(Path.home() / "AppData" / "Local")
        return Path(base) / "drspec" / "Cache" / "models"
    base = os.environ.get("XDG_CACHE_HOME") or str(Path.home() / ".cache")
    return Path(base) / "drspec" / "models"


def _project_cache_dir() -> Path:
    """Where older versions kept the model, and where it is kept when the shared folder cannot be used."""
    return Path.cwd() / "_drspec" / "models"


def _snapshot_complete(snapshot: Path) -> bool:
    return all((snapshot / name).exists() for name in _SNAPSHOT_FILES)  # follows links: a missing blob is not there


def _has_model(cache: Path) -> bool:
    return cache.is_dir() and any(_snapshot_complete(path) for path in cache.glob("models--*/snapshots/*"))


def _usable(folder: Path) -> Optional[str]:
    """`None` when `folder` can hold the model: it can be created, belongs to this user and can be written to.
    Otherwise why not."""
    try:
        folder.mkdir(parents=True, exist_ok=True)
        info = os.stat(str(folder))
        if hasattr(os, "geteuid") and info.st_uid != os.geteuid():
            return "it belongs to another user"
        if not os.access(str(folder), os.W_OK | os.X_OK):
            return "it cannot be written to"
    except OSError as e:
        return e.strerror or e.__class__.__name__
    return None


def _link_or_copy(source: str, target: str) -> None:
    try:
        os.link(source, target)  # no second copy on the same disk
    except OSError:
        shutil.copy2(source, target)


def _adopt_project_model(shared: Path) -> None:
    """Move an older project-local model into the shared folder once, so nothing is downloaded again. The
    old folder is left alone: the user is told it can be deleted."""
    global _old_project_folder
    old = _project_cache_dir()
    if not _has_model(old):
        return
    _old_project_folder = old
    if _has_model(shared):
        return
    staging = Path(tempfile.mkdtemp(prefix=".adopting-", dir=str(shared)))
    try:
        for model in old.glob("models--*"):
            copy = staging / model.name
            shutil.copytree(str(model), str(copy), symlinks=True, copy_function=_link_or_copy)
            final = shared / model.name
            if not final.exists():
                os.replace(str(copy), str(final))  # appears whole or not at all
    finally:
        shutil.rmtree(str(staging), ignore_errors=True)


def _default_cache_dir() -> str:
    """The folder the model is read from and downloaded to: the shared per-user folder, or, when that cannot be
    used, the project's own (`model_cache_info` says which)."""
    global _fallback_reason
    shared = shared_cache_dir()
    problem = _usable(shared)
    if problem is not None:
        _fallback_reason = f"{shared} cannot be used ({problem})"
        return str(_project_cache_dir())
    _fallback_reason = None
    try:
        _adopt_project_model(shared)
    except OSError:
        pass  # worst case the model is downloaded again
    return str(shared)


def model_cache_info() -> dict:
    """Where the model is kept, for `worker status`: the folder, whether the project's own had to be used,
    and an older project folder that can be deleted."""
    folder = _default_cache_dir()
    info: dict = {"folder": folder, "cached": _has_model(Path(folder))}
    if _fallback_reason:
        info["fallback"] = _fallback_reason
    if _old_project_folder is not None and str(_old_project_folder) != folder:
        info["note"] = f"The model now lives in {folder}; the copy in {_old_project_folder} (about 87 MB) can be deleted."
    return info


def model_is_cached() -> bool:
    """Whether the model weights are already in the cache folder, so loading needs no download."""
    return _has_model(Path(_default_cache_dir()))


@contextlib.contextmanager
def _download_lock(cache: Path) -> Iterator[None]:
    """One download at a time per cache folder, across processes: the others wait here and then find the
    model in place."""
    cache.mkdir(parents=True, exist_ok=True)
    handle = open(cache / ".download.lock", "w")
    try:
        if fcntl is not None:
            fcntl.flock(handle, fcntl.LOCK_EX)
        yield
    finally:
        handle.close()  # releases the lock


def _get_model(threads: Optional[int] = None):
    global _model
    if _model is None:
        from huggingface_hub.utils import disable_progress_bars

        # The first model download's tqdm progress bar can otherwise land in stdout, breaking clean JSON output.
        disable_progress_bars()

        from fastembed import TextEmbedding

        cache = Path(_default_cache_dir())
        if _has_model(cache):
            _model = TextEmbedding(model_name=MODEL_NAME, cache_dir=str(cache), threads=threads)
        else:
            with _download_lock(cache):
                # Whoever held the lock may have finished the download; loading then needs no network.
                _model = TextEmbedding(model_name=MODEL_NAME, cache_dir=str(cache), threads=threads)
    return _model


def load_model(threads: int) -> None:
    """Load the model with a fixed thread count; the one-time download
    happens here if the weights are not cached yet."""
    _get_model(threads)


def embed_text(text: str) -> list[float]:
    """Embed `text` into a 384-dim vector using the local all-MiniLM-L6-v2 model.

    Input longer than 256 tokens is silently truncated by the tokenizer rather than
    chunked or rejected — this is only a best-effort similarity aid, not an exact representation.

    Args:
        text: Source text to embed (a function body, or call-site text).

    Returns:
        A list of 384 floats.
    """
    model = _get_model()
    vector = next(model.embed([text]))
    return vector.tolist()
