"""The guard that no test touches anything outside the repository and its own temporary folder (`tests/tools/outside_guard.py`).

Two kinds of test. The classification of a path (`is_outside`) is pure: a string in, an answer out, nothing is opened. And one
small session of fake tests is run in a process of its own (`pytester`), each fake test doing one thing: reading a file outside,
writing outside, listing outside, binding a socket outside, opening a DuckDB file outside (each is refused and fails), and the
ones that are not flagged (a file in `tmp_path`, a file of the repository, an in-memory database, a test marked `outside`).
"""

from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

import pytest

from tests.tools import outside_guard

pytest_plugins = ["pytester"]

REPOSITORY = Path(outside_guard.REPOSITORY)


class TestWhichPathsAreOutside:
    def test_the_repository_is_inside(self):
        assert not outside_guard.is_outside(REPOSITORY / "pyproject.toml")
        assert not outside_guard.is_outside(str(REPOSITORY))

    def test_the_temporary_folder_of_the_session_is_inside(self, tmp_path):
        assert not outside_guard.is_outside(tmp_path / "a" / "b.txt")
        assert not outside_guard.is_outside(Path(tempfile.gettempdir()) / "x")  # (the session moved it inside its own area)

    def test_a_relative_path_is_judged_from_the_working_folder(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        assert not outside_guard.is_outside("a.txt")
        monkeypatch.chdir(REPOSITORY)
        assert not outside_guard.is_outside("src/drspec")

    @pytest.mark.parametrize(
        "path",
        ["/etc/passwd", "/home/someone/.codex/prompts", "/media/thanh/data/Projects/raylib", "/tmp/custom.db", "/root", "/var/tmp/x"],
    )
    def test_a_path_elsewhere_is_outside(self, path, monkeypatch):
        monkeypatch.chdir(REPOSITORY)
        assert outside_guard.is_outside(path)

    def test_a_sibling_of_the_repository_is_outside(self):
        assert outside_guard.is_outside(REPOSITORY.parent / "raylib")
        assert outside_guard.is_outside(str(REPOSITORY) + "-other/file")  # (a name that only starts with the repository's)

    def test_the_folders_above_a_root_may_be_looked_at_but_not_listed_or_opened(self):
        assert not outside_guard.is_outside(REPOSITORY.parent, looking=True)
        assert not outside_guard.is_outside("/", looking=True)
        assert outside_guard.is_outside(REPOSITORY.parent)  # listing the folder that holds the clones is not allowed

    def test_the_python_that_runs_the_tests_is_inside(self):
        assert not outside_guard.is_outside(os.path.join(sys.prefix, "lib"))
        assert not outside_guard.is_outside(os.path.dirname(os.__file__))

    def test_the_devices_and_the_process_itself_are_inside(self):
        assert not outside_guard.is_outside("/dev/null")
        assert not outside_guard.is_outside("/proc/self/stat")
        assert not outside_guard.is_outside(f"/proc/{os.getpid()}/status")
        assert outside_guard.is_outside("/proc/1/stat")

    def test_the_repository_is_read_only(self):
        assert not outside_guard.is_outside(REPOSITORY / "pyproject.toml")  # reading it is fine
        assert outside_guard.is_outside(REPOSITORY / "pyproject.toml", writing=True)
        assert outside_guard.is_outside(REPOSITORY / "_drspec" / "contracts.db", writing=True)
        assert outside_guard.is_outside(REPOSITORY / ".gitignore", writing=True)

    def test_only_this_sessions_temporary_folder_may_be_written(self, tmp_path):
        assert not outside_guard.is_outside(tmp_path / "a.txt", writing=True)
        assert not outside_guard.is_outside("/dev/null", writing=True)
        other_session = tmp_path.parent.parent / "pytest-999999" / "test_x0" / "a.txt"  # (another session of the same user)
        assert outside_guard.is_outside(other_session)
        assert outside_guard.is_outside(other_session, writing=True)

    def test_the_pythons_own_folders_may_be_read_not_written(self):
        assert outside_guard.is_outside(os.path.join(sys.prefix, "lib", "x"), writing=True)
        assert outside_guard.is_outside(os.path.dirname(os.__file__) + "/x.py", writing=True)

    def test_a_file_descriptor_names_no_path(self):
        assert not outside_guard.is_outside(3)

    def test_a_project_marker_above_the_area_is_known_by_name(self):
        assert outside_guard._is_marker_probe(str(REPOSITORY.parent / ".git")) == ".git"
        assert outside_guard._is_marker_probe(str(REPOSITORY.parent / "_drspec" / "contracts.db")) == "_drspec"
        assert not outside_guard._is_marker_probe(str(REPOSITORY.parent / "raylib"))  # a clone is not a marker: it is flagged
        assert not outside_guard._is_marker_probe(str(REPOSITORY / ".git"))  # inside the repository it is just a path


class TestTheSessionIsolatesTheEnvironment:
    def test_home_and_the_xdg_folders_are_inside_the_sessions_temporary_area(self, tmp_path):
        area = tmp_path.parent  # pytest's folder of this session (`tmp_path` is one of its children)
        for name in ("HOME", "USERPROFILE", "XDG_CACHE_HOME", "XDG_CONFIG_HOME", "XDG_DATA_HOME"):
            assert Path(os.environ[name]).is_relative_to(area) if hasattr(Path, "is_relative_to") else str(area) in os.environ[name], name
        assert Path.home() == Path(os.environ["HOME"])

    def test_the_temporary_folder_is_inside_it_too(self, tmp_path):
        area = str(tmp_path.parent)
        for name in ("TMPDIR", "TEMP", "TMP"):
            assert os.environ[name].startswith(area), name
        assert tempfile.gettempdir().startswith(area)

    def test_the_model_folder_is_an_empty_one_of_the_session_not_the_repositorys(self, tmp_path):
        folder = Path(os.environ["DRSPEC_MODEL_DIR"])
        assert folder.parent.parent == tmp_path.parent and folder.name == "models"  # (<basetemp>/isolated/models)
        assert not outside_guard.is_outside(folder, writing=True) and folder != REPOSITORY / "_drspec" / "models"
        assert not any(folder.iterdir())  # (no weights: a test that needs an embedder fakes it)

    def test_the_interpreter_does_not_write_bytecode_into_the_repository(self, tmp_path, monkeypatch):
        """A module imported for the first time inside a test is not compiled to a `__pycache__` (the repository is read-only for tests). The
        guard has no exception for bytecode: a test that compiles a file into the repository is refused (`repo_compiles_a_file` below)."""
        import importlib

        assert sys.dont_write_bytecode and os.environ["PYTHONDONTWRITEBYTECODE"] == "1"
        (tmp_path / "fresh_module_for_the_bytecode_test.py").write_text("VALUE = 1\n")
        monkeypatch.syspath_prepend(str(tmp_path))
        assert importlib.import_module("fresh_module_for_the_bytecode_test").VALUE == 1
        assert not (tmp_path / "__pycache__").exists()


REPO = str(REPOSITORY)
NEVER = REPO + "/_guard_probe_never"  # (its folder does not exist: even if the guard let a call through, nothing could be made here)
NOWHERE = "/nonexistent_drspec_guard_probe"

#: What the fake tests do, by name: each is a thing a test must NOT do (the guard has to refuse it and fail the test).
FORBIDDEN = {
    # outside the repository: read, write, list, look, make, bind, open a database
    "reads_a_file_outside": 'Path("/etc/hostname").read_text()',
    "opens_a_file_outside_to_write": f'open("{NOWHERE}/f.txt", "w").close()',
    "lists_a_folder_outside": 'os.listdir("/media")',
    "looks_for_a_sibling_clone": f'Path("{REPO}-clone").exists()',
    "makes_a_folder_outside": f'os.makedirs("{NOWHERE}/sub")',
    "binds_a_socket_outside": f's = socket.socket(socket.AF_UNIX); s.bind("{NOWHERE}/x.sock")',
    "opens_a_duckdb_file_outside": f'duckdb.connect("{NOWHERE}/x.db")',
    "opens_a_sqlite_file_outside": f'sqlite3.connect("{NOWHERE}/x.db")',
    "os_access_outside": 'os.access("/etc/hostname", os.R_OK)',
    "os_readlink_outside": 'os.readlink("/etc/hostname")',
    "path_lstat_outside": 'Path("/etc/hostname").lstat()',
    "os_path_exists_outside": 'os.path.exists("/etc/hostname")',
    "os_path_isfile_outside": 'os.path.isfile("/etc/hostname")',
    "os_path_isdir_outside": 'os.path.isdir("/var")',
    "os_path_islink_outside": 'os.path.islink("/etc/hostname")',
    "tempfile_mkdtemp_outside": f'tempfile.mkdtemp(dir="{NOWHERE}")',
    "the_same_read_in_the_test_itself_is_refused": 'Path("/etc/hostname").read_text()',
    # into the repository: the repository is read-only
    "repo_open_to_write": f'open("{NEVER}/f.txt", "w")',
    "repo_open_to_append": f'open("{NEVER}/f.txt", "a")',
    "repo_os_open_to_create": f'os.open("{NEVER}/f.txt", os.O_WRONLY | os.O_CREAT)',
    "repo_path_open_to_write": f'Path("{NEVER}/f.txt").open("w")',
    "repo_os_mkdir": f'os.mkdir("{NEVER}/sub")',
    "repo_os_remove": f'os.remove("{NEVER}/f.txt")',
    "repo_os_rename": f'os.rename("{NEVER}/a", "{NEVER}/b")',
    "repo_os_symlink": f'os.symlink("x", "{NEVER}/link")',
    "repo_os_chmod": f'os.chmod("{NEVER}/f.txt", 0o600)',
    "repo_os_utime": f'os.utime("{NEVER}/f.txt")',
    "repo_os_truncate": f'os.truncate("{NEVER}/f.txt", 0)',
    "repo_path_write_text": f'Path("{NEVER}/f.txt").write_text("x")',
    "repo_path_mkdir": f'Path("{NEVER}/sub").mkdir()',
    "repo_path_touch": f'Path("{NEVER}/f.txt").touch()',
    "repo_path_symlink_to": f'Path("{NEVER}/link").symlink_to("x")',
    "repo_shutil_copy_into": f'shutil.copy(__file__, "{NEVER}/f.txt")',
    "repo_shutil_rmtree": f'shutil.rmtree("{NEVER}/sub")',
    "repo_unlink_by_name_from_an_open_folder": f'fd = os.open("{REPO}", os.O_RDONLY); os.unlink("_guard_probe_never", dir_fd=fd)',
    "repo_compiles_a_file": f'import py_compile; py_compile.compile(__file__, cfile="{NEVER}/x.pyc")',
    "repo_duckdb_file": f'duckdb.connect("{NEVER}/x.db")',
    "repo_sqlite_file": f'sqlite3.connect("{NEVER}/x.db")',
}

#: What the fake tests do that is fine, and must pass.
ALLOWED = {
    "reads_a_file_of_the_repository": f'assert Path("{REPO}/pyproject.toml").read_text()',
    "opens_a_file_of_the_repository_to_read": f'open("{REPO}/pyproject.toml", "rb").close()',
    "lists_the_repository": f'assert os.listdir("{REPO}")',
    "stats_the_repository": f'assert Path("{REPO}").stat() and os.path.isdir("{REPO}")',
    "writes_in_tmp_path": 'Path(tmp_path, "a.txt").write_text("x"); assert Path(tmp_path, "a.txt").read_text() == "x"',
    "makes_things_in_tmp_path": 'os.makedirs(Path(tmp_path, "a", "b")); shutil.copy(__file__, Path(tmp_path, "a", "c.py")); tempfile.mkdtemp(dir=tmp_path)',
    "an_in_memory_database": 'duckdb.connect(":memory:").close(); sqlite3.connect(":memory:").close()',
    # (`shutil.rmtree` removes the entries of a folder BY NAME from an open folder: the name is relative to that folder, not to the current directory)
    "removes_a_folder_in_tmp_path_while_the_current_directory_is_the_repository": 'folder = Path(tmp_path, "d"); folder.mkdir(); Path(folder, "f").write_text("x"); previous = os.getcwd(); os.chdir("' + REPO + '"); shutil.rmtree(folder); os.chdir(previous); assert not folder.exists()',
    "a_database_file_in_tmp_path": 'duckdb.connect(str(Path(tmp_path, "x.db"))).close()',
    "looks_at_the_folders_above": f'assert Path("{REPO}").parent.exists()',
    "a_marker_above_is_absent": f'assert not Path("{REPO}").parent.joinpath(".git").exists()',
    "a_library_may_look_at_the_machine_when_it_is_imported": "import probe_library; assert probe_library.READ",
    "a_marked_test_may_read_outside": 'Path("/etc/hostname").read_text()',
}


def _fake_session() -> str:
    lines = [
        "import os, shutil, socket, sqlite3, tempfile",
        "from pathlib import Path",
        "import duckdb, pytest",
        "",
    ]
    for name, code in FORBIDDEN.items():
        lines += [f"def test_{name}():", f"    {code}", ""]
    for name, code in ALLOWED.items():
        mark = '@pytest.mark.outside(reason="the real file is the objective of this fake test")\n' if name == "a_marked_test_may_read_outside" else ""
        lines += [f"{mark}def test_{name}(tmp_path):", f"    {code}", ""]
    return "\n".join(lines)


@pytest.mark.integration  # (a pytest session in a process of its own: the fake tests must not be seen by this session's guard)
class TestTheGuardOnFakeTests:
    def test_each_forbidden_access_fails_its_test_and_each_allowed_one_passes(self, pytester, monkeypatch):
        # (`pytester` gives the process another HOME, which hides the user's site-packages: the path of this session is handed on)
        monkeypatch.setenv("PYTHONPATH", os.pathsep.join([str(REPOSITORY), *[entry for entry in sys.path if entry]]))
        pytester.makeconftest(
            "from tests.tools import outside_guard\n\n\ndef pytest_configure(config):\n    outside_guard.register(config)\n"
        )
        pytester.makepyfile(test_fake=_fake_session(), probe_library='READ = open("/etc/hostname").read()\n')
        result = pytester.runpytest_subprocess("-p", "no:cacheprovider", "-rA", "--tb=short")
        outcomes = {}
        for line in result.stdout.lines:
            for verdict in ("PASSED", "FAILED"):
                if line.startswith(verdict + " ") and "::" in line:
                    outcomes[line.split("::", 1)[1].split(" ")[0]] = verdict
        assert {name: outcomes.get(f"test_{name}") for name in FORBIDDEN} == {name: "FAILED" for name in FORBIDDEN}
        assert {name: outcomes.get(f"test_{name}") for name in ALLOWED} == {name: "PASSED" for name in ALLOWED}
        text = result.stdout.str()
        assert f"touched a path outside the repository and tmp_path: {len(FORBIDDEN)} test(s)" in text
        for line in (
            "Path.read_text /etc/hostname",
            "socket.bind /nonexistent_drspec_guard_probe/x.sock",
            "duckdb.connect /nonexistent_drspec_guard_probe/x.db",
            "os.listdir /media",
            "os.unlink " + NEVER,
            "open " + NEVER + "/f.txt",
            "os.mkdir " + NEVER + "/sub",
            "tempfile",  # (the call that made the folder is `os.mkdir`)
        ):
            if line != "tempfile":
                assert line in text, line
        # what is answered or left out is COUNTED and printed
        assert "answered 'absent': 1 probes of project markers above the area (.git 1)" in text
        assert "unrecorded accesses inside third-party imports: 1, libraries: probe_library 1" in text
        assert "tests exempted by @pytest.mark.outside: 1" in text
        assert "test_a_marked_test_may_read_outside  reason: the real file is the objective of this fake test" in text
        # nothing was written outside the test's own area, and the session worked in this test's own folder
        assert not os.path.exists(NEVER)  # (the folder of the repository that the fake tests tried to make; `NOWHERE` is not looked at: that would be outside)
        assert str(pytester.path) in text
