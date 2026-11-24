import os
import time

from drspec.db.staging import STAGING_PREFIX, sweep_orphaned_staging


def _folder(parent, name, age_seconds=0):
    path = parent / name
    path.mkdir()
    (path / "contracts.db").write_bytes(b"x")
    stamp = time.time() - age_seconds
    os.utime(path, (stamp, stamp))
    return path


def _dead_pid():
    """A process id nobody has: the highest one the system could give out that `os.kill(pid, 0)` (what the sweep asks) says is gone.
    Nothing is started."""
    for pid in range(2**22 - 1, 1, -1):
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return pid
        except PermissionError:
            continue


class TestSweepOrphanedStaging:
    def test_removes_a_folder_whose_owner_has_exited(self, tmp_path):
        orphan = _folder(tmp_path, f"{STAGING_PREFIX}{_dead_pid()}-abc")

        assert sweep_orphaned_staging(tmp_path) == 1
        assert not orphan.exists()

    def test_keeps_a_folder_whose_owner_is_running(self, tmp_path):
        live = _folder(tmp_path, f"{STAGING_PREFIX}{os.getpid()}-abc")

        assert sweep_orphaned_staging(tmp_path) == 0
        assert live.exists()

    def test_an_unowned_folder_goes_only_once_it_is_old(self, tmp_path):
        recent = _folder(tmp_path, f"{STAGING_PREFIX}recent", age_seconds=60)
        old = _folder(tmp_path, f"{STAGING_PREFIX}old", age_seconds=7200)

        assert sweep_orphaned_staging(tmp_path) == 1
        assert recent.exists() and not old.exists()

    def test_leaves_other_entries_alone(self, tmp_path):
        other = _folder(tmp_path, "models", age_seconds=7200)
        (tmp_path / f"{STAGING_PREFIX}file").write_text("not a folder")

        assert sweep_orphaned_staging(tmp_path) == 0
        assert other.exists()
