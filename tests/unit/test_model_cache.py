"""One embedding-model cache per user, not one per project."""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

from drspec.core import embedder
from tests.fakes import not_writable

FILES = ("model.onnx", "tokenizer.json", "config.json", "tokenizer_config.json", "special_tokens_map.json")
FOLDER = "models--qdrant--all-MiniLM-L6-v2-onnx"


def _make_model(cache: Path, complete: bool = True) -> Path:
    """A folder laid out the way the hub library leaves a downloaded model: blobs and a snapshot of links."""
    model = cache / FOLDER
    (model / "blobs").mkdir(parents=True)
    snapshot = model / "snapshots" / "abc123"
    snapshot.mkdir(parents=True)
    for name in FILES:
        blob = model / "blobs" / ("blob-" + name)
        if complete or name != "model.onnx":
            blob.write_bytes(b"weights:" + name.encode())
        (snapshot / name).symlink_to(os.path.join("..", "..", "blobs", blob.name))
    return model


@pytest.fixture(autouse=True)
def _fresh_state(monkeypatch, tmp_path):
    # The code looks for the old project folder `_drspec/models` under the CURRENT directory: it is the test's own empty folder,
    # never the repository (whose `_drspec/models` holds the developer's real weights, which `_adopt_project_model` would move).
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(embedder, "_fallback_reason", None)
    monkeypatch.setattr(embedder, "_old_project_folder", None)
    monkeypatch.setattr(embedder, "_model", None)


class TestWhere:
    def test_the_default_is_the_systems_cache_folder(self, monkeypatch, tmp_path):
        monkeypatch.delenv(embedder.MODEL_DIR_ENV_VAR)
        monkeypatch.setattr(embedder.sys, "platform", "linux")
        monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "xdg"))
        assert embedder.shared_cache_dir() == tmp_path / "xdg" / "drspec" / "models"
        monkeypatch.delenv("XDG_CACHE_HOME")
        monkeypatch.setattr(embedder.Path, "home", classmethod(lambda cls: tmp_path / "home"))
        assert embedder.shared_cache_dir() == tmp_path / "home" / ".cache" / "drspec" / "models"

    def test_macos_and_windows_have_their_own_folders(self, monkeypatch, tmp_path):
        monkeypatch.delenv(embedder.MODEL_DIR_ENV_VAR)
        monkeypatch.setattr(embedder.Path, "home", classmethod(lambda cls: tmp_path / "home"))
        monkeypatch.setattr(embedder.sys, "platform", "darwin")
        assert embedder.shared_cache_dir() == tmp_path / "home" / "Library" / "Caches" / "drspec" / "models"
        monkeypatch.setattr(embedder.sys, "platform", "win32")
        monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "local"))
        assert embedder.shared_cache_dir() == tmp_path / "local" / "drspec" / "Cache" / "models"

    def test_the_environment_variable_overrides(self, monkeypatch, tmp_path):
        monkeypatch.setenv(embedder.MODEL_DIR_ENV_VAR, str(tmp_path / "team"))
        assert embedder.shared_cache_dir() == tmp_path / "team"
        assert embedder._default_cache_dir() == str(tmp_path / "team")

    def test_the_path_does_not_depend_on_the_current_directory(self, monkeypatch, tmp_path):
        monkeypatch.setenv(embedder.MODEL_DIR_ENV_VAR, str(tmp_path / "shared"))
        first = embedder._default_cache_dir()
        elsewhere = tmp_path / "elsewhere"
        elsewhere.mkdir()
        monkeypatch.chdir(elsewhere)
        assert embedder._default_cache_dir() == first
        assert not (elsewhere / "_drspec").exists()  # nothing is created in the project


class TestWhatCountsAsCached:
    def test_a_complete_snapshot_is_cached(self, monkeypatch, tmp_path):
        _make_model(tmp_path / "shared")
        monkeypatch.setenv(embedder.MODEL_DIR_ENV_VAR, str(tmp_path / "shared"))
        assert embedder.model_is_cached() is True

    def test_a_download_killed_midway_does_not_look_cached(self, monkeypatch, tmp_path):
        _make_model(tmp_path / "shared", complete=False)  # the model file never arrived: its link points nowhere
        monkeypatch.setenv(embedder.MODEL_DIR_ENV_VAR, str(tmp_path / "shared"))
        assert embedder.model_is_cached() is False

    def test_an_empty_folder_is_not_cached(self, monkeypatch, tmp_path):
        monkeypatch.chdir(tmp_path)
        monkeypatch.setenv(embedder.MODEL_DIR_ENV_VAR, str(tmp_path / "shared"))
        assert embedder.model_is_cached() is False


class TestAdoptingTheOldProjectFolder:
    def test_the_model_is_moved_across_once_without_a_download(self, monkeypatch, tmp_path):
        project = tmp_path / "project"
        _make_model(project / "_drspec" / "models")
        monkeypatch.chdir(project)
        monkeypatch.setenv(embedder.MODEL_DIR_ENV_VAR, str(tmp_path / "shared"))
        assert embedder.model_is_cached() is True
        assert (tmp_path / "shared" / FOLDER / "snapshots" / "abc123" / "model.onnx").read_bytes() == b"weights:model.onnx"
        info = embedder.model_cache_info()
        assert info["folder"] == str(tmp_path / "shared") and info["cached"] is True
        assert str(project / "_drspec" / "models") in info["note"] and "can be deleted" in info["note"]
        assert (project / "_drspec" / "models" / FOLDER).exists()  # nothing is deleted automatically

    def test_blobs_are_hard_linked_where_the_disk_allows(self, monkeypatch, tmp_path):
        project = tmp_path / "project"
        old = _make_model(project / "_drspec" / "models")
        monkeypatch.chdir(project)
        monkeypatch.setenv(embedder.MODEL_DIR_ENV_VAR, str(tmp_path / "shared"))
        embedder._default_cache_dir()
        new_blob = tmp_path / "shared" / FOLDER / "blobs" / "blob-model.onnx"
        assert os.stat(new_blob).st_ino == os.stat(old / "blobs" / "blob-model.onnx").st_ino

    def test_an_existing_shared_model_is_left_alone(self, monkeypatch, tmp_path):
        project = tmp_path / "project"
        _make_model(project / "_drspec" / "models")
        shared = tmp_path / "shared"
        mine = _make_model(shared)
        (mine / "blobs" / "blob-model.onnx").write_bytes(b"mine")
        monkeypatch.chdir(project)
        monkeypatch.setenv(embedder.MODEL_DIR_ENV_VAR, str(shared))
        embedder._default_cache_dir()
        assert (mine / "blobs" / "blob-model.onnx").read_bytes() == b"mine"

    def test_a_project_without_an_old_folder_has_nothing_to_say(self, monkeypatch, tmp_path):
        _make_model(tmp_path / "shared")
        monkeypatch.chdir(tmp_path)
        monkeypatch.setenv(embedder.MODEL_DIR_ENV_VAR, str(tmp_path / "shared"))
        assert "note" not in embedder.model_cache_info()


class TestFallback:
    def test_an_unusable_shared_folder_falls_back_to_the_project_and_says_so(self, monkeypatch, tmp_path):
        blocker = tmp_path / "blocker"
        blocker.write_text("a file, not a folder")
        monkeypatch.chdir(tmp_path)
        monkeypatch.setenv(embedder.MODEL_DIR_ENV_VAR, str(blocker / "models"))
        assert embedder._default_cache_dir() == str(tmp_path / "_drspec" / "models")
        info = embedder.model_cache_info()
        assert info["folder"] == str(tmp_path / "_drspec" / "models") and "cannot be used" in info["fallback"]

    def test_a_read_only_shared_folder_falls_back(self, monkeypatch, tmp_path):
        shared = tmp_path / "ro"
        shared.mkdir()
        not_writable(monkeypatch, shared)  # a folder mode does not stop root: the answer of `os.access` is faked
        monkeypatch.chdir(tmp_path)
        monkeypatch.setenv(embedder.MODEL_DIR_ENV_VAR, str(shared))
        assert embedder._default_cache_dir() == str(tmp_path / "_drspec" / "models")
        assert "cannot be written to" in embedder.model_cache_info()["fallback"]


class TestOneDownloadAtATime:
    def test_while_one_holds_the_lock_another_cannot_take_it(self, tmp_path):
        """`flock` locks the open file, not the process: a second open of the lock file in this process is refused while the first holds it,
        as it is for another process (no second process is started to show it), and is granted once the first lets go."""
        import fcntl

        cache = tmp_path / "cache"

        def try_to_take():
            other = open(cache / ".download.lock", "w")
            try:
                fcntl.flock(other, fcntl.LOCK_EX | fcntl.LOCK_NB)
            finally:
                other.close()

        with embedder._download_lock(cache):
            with pytest.raises(BlockingIOError):
                try_to_take()
        try_to_take()

    def test_a_cached_model_is_loaded_without_taking_the_lock(self, monkeypatch, tmp_path):
        _make_model(tmp_path / "shared")
        monkeypatch.setenv(embedder.MODEL_DIR_ENV_VAR, str(tmp_path / "shared"))
        loaded = []
        locked = []
        monkeypatch.setattr(embedder, "_download_lock", lambda cache: (locked.append(cache), __import__("contextlib").nullcontext())[1])
        _fake_fastembed(monkeypatch, loaded)
        embedder._get_model()
        assert loaded == [str(tmp_path / "shared")] and locked == []

    def test_a_missing_model_is_downloaded_under_the_lock(self, monkeypatch, tmp_path):
        monkeypatch.chdir(tmp_path)
        monkeypatch.setenv(embedder.MODEL_DIR_ENV_VAR, str(tmp_path / "shared"))
        events = []
        import contextlib

        @contextlib.contextmanager
        def lock(cache):
            events.append("lock")
            yield
            events.append("unlock")

        monkeypatch.setattr(embedder, "_download_lock", lock)
        _fake_fastembed(monkeypatch, events)
        embedder._get_model()
        assert events == ["lock", str(tmp_path / "shared"), "unlock"]


def _fake_fastembed(monkeypatch, record):
    import types

    class FakeEmbedding:
        def __init__(self, model_name, cache_dir, threads=None):
            record.append(cache_dir)

    fake = types.ModuleType("fastembed")
    fake.TextEmbedding = FakeEmbedding
    monkeypatch.setitem(sys.modules, "fastembed", fake)


class TestWhatTheUserSees:
    def test_worker_status_says_where_the_model_is_and_what_can_be_deleted(self, monkeypatch, tmp_path):
        from drspec.staleness_worker import get_worker_status

        project = tmp_path / "project"
        _make_model(project / "_drspec" / "models")
        monkeypatch.chdir(project)
        monkeypatch.setenv(embedder.MODEL_DIR_ENV_VAR, str(tmp_path / "shared"))
        status = get_worker_status(project / "_drspec" / "contracts.db")
        assert status["model_cache"]["folder"] == str(tmp_path / "shared") and "can be deleted" in status["model_cache"]["note"]

    def test_the_embedding_state_follows_whether_the_shared_model_is_there(self, monkeypatch, tmp_path):
        monkeypatch.chdir(tmp_path)
        monkeypatch.setenv(embedder.MODEL_DIR_ENV_VAR, str(tmp_path / "shared"))
        assert embedder.model_is_cached() is False  # the runner would say "downloading"
        _make_model(tmp_path / "shared")
        assert embedder.model_is_cached() is True  # and now "loading"
