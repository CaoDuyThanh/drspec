"""Session-wide test fixtures.

`drspec scan` auto-launches the background staleness worker, and every
routed command reaches the database through that worker. Without the
flag below, each scan- or command-exercising test would spawn a real,
independent OS process with nothing to reap it. The flag makes
`worker_ipc.get_worker_connection` return a plain, single-use
in-memory DuckDB connection instead. Tests that exercise the worker/IPC layer
itself (genuinely separate processes, this project's standard for any
DuckDB concurrency claim) unset it for their own subprocess's environment.
"""

from __future__ import annotations

import os
import shutil
import site
import sys
import tempfile
import time

import pytest


def _stand_in_for_matplotlib() -> None:
    """The product imports matplotlib when the CLI starts (`drspec.visualization`): 0.5 to 2 s in every test process. No test draws a figure
    any more (the matplotlib tests were removed), and the import only calls `matplotlib.use("Agg")`, so an empty module takes its place."""
    import types

    if "matplotlib" in sys.modules:
        return
    matplotlib = types.ModuleType("matplotlib")
    matplotlib.__path__ = []  # a package, so that `import matplotlib.pyplot` finds the module below
    matplotlib.use = lambda *args, **kwargs: None
    pyplot = types.ModuleType("matplotlib.pyplot")
    matplotlib.pyplot = pyplot
    sys.modules["matplotlib"] = matplotlib
    sys.modules["matplotlib.pyplot"] = pyplot


_stand_in_for_matplotlib()

from tests.tools import outside_guard  # noqa: E402

# The end-of-run tier report, printed only with DRSPEC_TIER_REPORT=1 (reports only; see tests/README.md).
# Matplotlib must be stubbed before test plugins import application modules.
from tests.tools.tier_summary import (  # noqa: E402, F401
    pytest_runtest_call,
    pytest_runtest_protocol,
    pytest_sessionfinish,
    pytest_sessionstart,
    pytest_terminal_summary,
)


def pytest_configure(config):
    """Every test is checked: one that touches a path outside the repository and its own temporary folder fails (`tests/tools/outside_guard.py`)."""
    outside_guard.register(config)


def pytest_unconfigure(config):
    outside_guard.unregister(config)


@pytest.fixture(scope="session", autouse=True)
def _isolated_environment(tmp_path_factory):
    """The environment of the whole session, before any test code: the home folder, the temporary folder, the XDG folders and the PATH are
    inside pytest's own temporary folder, so that everything the code derives from them (`~/.codex/prompts`, `~/.cache/drspec`, the trace
    log folder, short-path sockets, the folder `tempfile` makes, `shutil.which`) lands inside the area the tests may touch, and nothing
    is read from or left in the real ones. Everything is set with a `MonkeyPatch` and undone at the end of the session (no direct assignment to the environment)."""
    base = tmp_path_factory.getbasetemp()
    area = base / "isolated"
    home, temporary, tools = area / "home", area / "tmp", area / "bin"
    for folder in (home, temporary, tools, area / "models", home / ".cache", home / ".config", home / ".local" / "share"):
        folder.mkdir(parents=True, exist_ok=True)
    outside_guard.allow(base)
    patch = pytest.MonkeyPatch()
    # A Python started by a test (`python -m drspec ...`) finds the packages installed for the user in `~/.local`; with HOME moved it would not,
    # so the user base of the real Python is handed on explicitly (it is Python's own installation, which the guard allows).
    patch.setenv("PYTHONUSERBASE", os.environ.get("PYTHONUSERBASE") or site.USER_BASE)
    for name in ("HOME", "USERPROFILE"):
        patch.setenv(name, str(home))
    for name in ("TMPDIR", "TEMP", "TMP"):
        patch.setenv(name, str(temporary))
    patch.setenv("XDG_CACHE_HOME", str(home / ".cache"))
    patch.setenv("XDG_CONFIG_HOME", str(home / ".config"))
    patch.setenv("XDG_DATA_HOME", str(home / ".local" / "share"))
    # A program searched for on PATH is looked for in the folders of PATH, which are the developer's machine's: the PATH of the session is an
    # empty folder of the test area and the folder of the Python that runs the tests.
    patch.setenv("PATH", os.pathsep.join([str(tools), os.path.dirname(sys.executable)]))
    patch.setattr(tempfile, "tempdir", str(temporary))  # pattern-ok: tempfile the session's temporary folder is this one (the tests make no temp files of their own)
    # No worker process starts by itself. The embedding model folder is an EMPTY folder of the test area: the repository is read-only for tests,
    # and the weights are not committed, so no test may rely on them (a test that needs an embedder fakes it; `embed_text`, `load_model`).
    patch.setenv("DRSPEC_DISABLE_AUTO_WORKER", "1")
    # The identity of the code (`core/build_id.py`) is the package version and the names, sizes and MODIFICATION TIMES of its source files. A test that
    # starts a real worker compares the worker's identity with the one of this process; if any source file is touched between the two (an editor, a
    # checkout, another session's run), the client finds the worker outdated, stops it and the test's held connection breaks with "Broken pipe" /
    # "Lost contact". That was the failure seen once and then in two runs of dev-4: it depended on the tree being edited during the run. The identity is
    # a fixed string for the session and for the processes a test starts (a test of the comparison sets its own: `test_worker_build.py`).
    from drspec.core import build_id as build_id_module

    patch.setenv(build_id_module.BUILD_OVERRIDE_ENV, "test-session")
    build_id_module.reset_for_tests()
    # The code looks for an OLDER model folder under the current directory (`<cwd>/_drspec/models`) and adopts what it finds into the shared one.
    # When a test runs with the repository as its directory that is the developer's real weights (hard-linked into the test area on the first
    # call, so the result depended on the machine and on which test came first): the repository's own folder is never looked in.
    from drspec.core import embedder

    real_project_cache_dir = embedder._project_cache_dir
    nothing_here = area / "no_older_models"

    def project_cache_dir():
        folder = real_project_cache_dir()
        return nothing_here if outside_guard.is_in_repository(folder) else folder

    patch.setattr(embedder, "_project_cache_dir", project_cache_dir)
    # DuckDB imports pandas (and with it numpy) at the first parameterized query of the process: about 1.0 s of CPU that no test of ours uses (the
    # product marks them absent the same way, but only when they are not installed). Marked absent for the session, as `drspec.db.connection` does.
    for optional in ("pandas", "pyarrow", "polars"):
        if optional not in sys.modules:
            patch.setitem(sys.modules, optional, None)
    # The interpreter writes `.pyc` files next to the modules it imports (`tests/unit/__pycache__`, `src/drspec/__pycache__`): in a test that is a
    # write into the read-only repository, made by the interpreter and not by the test. It is switched OFF for the session (and for the
    # Python processes a test starts) rather than excused in the guard, which stays strict: a test that compiles a file into the repository
    # is still refused. Cost: a module first imported inside a test is compiled again by every run; the ones collected are not affected.
    patch.setattr(sys, "dont_write_bytecode", True)
    patch.setenv("PYTHONDONTWRITEBYTECODE", "1")
    patch.setenv("DRSPEC_MODEL_DIR", str(area / "models"))
    yield area
    patch.undo()
    build_id_module.reset_for_tests()


@pytest.fixture(autouse=True)
def _the_machine_has_a_fixed_amount_of_free_memory(monkeypatch):
    """The code asks the kernel (the kernel's memory table) how much memory is free, to decide whether to parse in parallel or to pause the embedding.
    A test must not depend on the machine it runs on, nor read the kernel's file: the free memory is 8 GB (4 GB for the trace's snapshot, so
    that the two are told apart) unless the test says another number with its own `monkeypatch`. (`staleness_worker` takes the name from
    `embedding_runner` when it is imported, so it is patched too only if it is already there: importing it here would make every test pay it.)"""
    from drspec.core import embedding_runner, tracing

    monkeypatch.setattr(embedding_runner, "available_memory_mb", lambda: 8192.0)
    monkeypatch.setattr(tracing, "_available_mb", lambda: 4096.0)
    worker = sys.modules.get("drspec.staleness_worker")
    if worker is not None:
        monkeypatch.setattr(worker, "available_memory_mb", lambda: 8192.0)


@pytest.fixture(autouse=True)
def _tracing_off(tmp_path_factory, monkeypatch):
    """Tracing is process-wide state; no test may leave it on for the next. The log folder is
    a private one per test, so no test writes into the real temporary folder."""
    from drspec.core import tracing

    monkeypatch.setenv(tracing.DIR_ENV_VAR, str(tmp_path_factory.mktemp("trace-logs")))
    tracing.reset_for_tests()
    yield
    tracing.reset_for_tests()


@pytest.fixture
def wait_until():
    """Poll `predicate` until it is truthy or `timeout` elapses, for waiting
    on another process (a fixed sleep is flaky on a loaded machine)."""

    def _wait_until(predicate, timeout: float = 120.0, interval: float = 0.05) -> bool:  # (it returns as soon as the predicate holds; a real worker took 3-21 s to start on a loaded machine)
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if predicate():
                return True
            time.sleep(interval)  # pattern-ok: real-sleep this IS the one allowed poll, for a real process another test started
        return bool(predicate())

    return _wait_until


@pytest.fixture(scope="session", autouse=True)
def _no_git_for_shared_fixtures():
    """The block of `no_git` for the fixtures that outlive a test: a scan shared by a class or a module runs before any
    function-scoped fixture, so without this its `inside_git` really ran `git -C <folder> rev-parse`."""
    from drspec.core import project_files

    with pytest.MonkeyPatch.context() as patch_for_the_session:
        patch_for_the_session.setattr(project_files, "_git_lines", lambda root, *arguments: None)
        yield


@pytest.fixture(autouse=True)
def no_git(monkeypatch):
    """No test starts `git`, whatever it does: for every test, `git` is "not there" for the listing of a project
    (`project_files._git_lines` answers None, as when git fails), so `inside_git` is False and the scanner's own rules alone
    apply, as for a folder outside any repository. A test that wants git's answer says what it is (`tests.fakes.git_listing`
    for the file listing, `tests.fakes.FakeGit` for the commits); it patches `_git_lines` again after this fixture ran."""
    from drspec.core import project_files

    monkeypatch.setattr(project_files, "_git_lines", lambda root, *arguments: None)


@pytest.fixture
def in_process_python(monkeypatch):
    """The verification scripts of `execute_verification` run in the test's own process (`tests/fakes.py::InProcessPython`), not in a
    Python subprocess: the generated verifier really runs, nothing is started."""
    from tests.fakes import InProcessPython

    return InProcessPython().install(monkeypatch)


@pytest.fixture
def scripts_in_process_unless_integration(request):
    """For a file that runs verification scripts: they run in this process (`in_process_python`), except in a test marked
    `integration`, which keeps the real Python subprocess because its objective is the real child (a timeout that kills it)."""
    if request.node.get_closest_marker("integration") is None:
        return request.getfixturevalue("in_process_python")
    return None


@pytest.fixture
def kept_duckdb(monkeypatch):
    """A DuckDB connection to a path gives one in-memory database per path, kept between connections (`tests/fakes.py::KeptDuckDB`): for a test
    whose objective is what a series of commands does to the data, not that it reaches a file. No file is written but an empty marker."""
    from tests.fakes import KeptDuckDB

    return KeptDuckDB().install(monkeypatch)


@pytest.fixture
def cached_cli_command(monkeypatch):
    """`CliRunner.invoke(app, ...)` builds the whole click command tree of the app at every call (about 15 ms of the 25 ms an invocation
    takes). With this fixture each app is built once for the test and reused: for a test that makes many CLI calls. The command itself
    is the same object the runner would have built, so what the commands do is unchanged."""
    import typer.testing

    built = {}
    real = typer.testing._get_command

    def cached(app):
        if id(app) not in built:
            built[id(app)] = real(app)
        return built[id(app)]

    monkeypatch.setattr(typer.testing, "_get_command", cached)


@pytest.fixture
def fake_embedder(monkeypatch):
    """`embed_text` gives a vector made from the words of the text (`tests/fakes.py::FakeEmbeddingModel`): no model weights, no network,
    no model folder written. Use it in any test whose objective is what happens to the vectors, not what the real model thinks."""
    from tests.fakes import FakeEmbeddingModel

    return FakeEmbeddingModel().install(monkeypatch)


@pytest.fixture
def kept_project(tmp_path, monkeypatch, kept_duckdb, cached_cli_command):
    """A folder that is the working folder, with an empty project database made as `init` makes its schema (`init_schema`): one in-memory
    DuckDB kept between the commands (`kept_duckdb`), the CLI command tree built once (`cached_cli_command`). For a test whose objective is
    what a command does to the index, not that `init` or a file works. No `init`, no database file: the project folder is `tmp_path`."""
    import duckdb

    from drspec.db import init_schema

    monkeypatch.chdir(tmp_path)
    (tmp_path / "_drspec").mkdir()
    connection = duckdb.connect(str(tmp_path / "_drspec" / "contracts.db"))  # pattern-ok: duckdb-file this fixture builds the template file other tests copy
    init_schema(connection)
    connection.close()
    return tmp_path


@pytest.fixture
def fake_time():
    """A clock that moves only when something sleeps (`tests/fakes.py::FakeTime`): put it in place of a module's `time` with
    `monkeypatch.setattr(module, "time", fake_time)`."""
    from tests.fakes import FakeTime

    return FakeTime()


@pytest.fixture
def serve():
    """`serve(server).client()` is a connection to a `WorkerIPCServer` without starting it or opening a socket
    (`tests/fakes.py::Served`); everything is closed at the end of the test."""
    from tests.fakes import Served

    made = []

    def _serve(server):
        made.append(Served(server))
        return made[-1]

    yield _serve
    for served in made:
        served.finish()


@pytest.fixture(scope="session")
def initialised_template(tmp_path_factory, _isolated_environment):
    """An empty project that `init --no-ide --non-interactive` has initialised, made ONCE for the session. A module that needs an
    initialised project copies this folder (`shutil.copytree`) instead of running `init` itself. The copy is the project a fresh `init`
    makes: `init` writes no absolute path of its folder into the database or the `_drspec` folder, and it does not index the files
    that are already there (a module writes its own files into the copy). Its tests that check `init` itself do not use this."""
    from typer.testing import CliRunner

    from drspec.cli.app import app

    root = tmp_path_factory.mktemp("initialised_template") / "project"
    root.mkdir()
    with pytest.MonkeyPatch.context() as patch:
        patch.chdir(root)
        result = CliRunner().invoke(app, ["init", "--no-ide", "--non-interactive"])  # pattern-ok: init-for-setup the one init of the session; the modules copy its folder
        assert result.exit_code == 0, result.output
    return root


@pytest.fixture
def fixed_launcher(tmp_path, monkeypatch):
    """`shutil.which` as the code under test calls it: `drspec` is found at a fixed place in this test's folder, so the result does not
    depend on whether drspec is installed where the PATH points; every other name goes to the real function. A test that needs "not
    found" or another place sets its own answer with `monkeypatch.setattr(shutil, "which", ...)`. A file whose tests all need it says
    `pytestmark = pytest.mark.usefixtures("fixed_launcher")`."""
    launcher = tmp_path / "bin" / "drspec"
    real_which = shutil.which

    def which(name, *args, **kwargs):
        return str(launcher) if name == "drspec" else real_which(name, *args, **kwargs)

    monkeypatch.setattr(shutil, "which", which)
    return launcher


@pytest.fixture
def fixed_build(monkeypatch):
    """The build id of drspec is a fixed string. Every MCP `tools/call` asks whether drspec was upgraded since the server
    started, which hashes the whole package (6 to 19 ms a call); with a fixed id it answers at once, and the "upgraded" notice
    stays off (`test_mcp_registration.py` tests it with stubs of its own). A file whose tests call many tools says
    `pytestmark = pytest.mark.usefixtures("fixed_build")`."""
    from drspec import mcp_server
    from drspec.core import build_id

    monkeypatch.setenv(build_id.BUILD_OVERRIDE_ENV, "test")
    build_id.reset_for_tests()
    monkeypatch.setattr(mcp_server, "_started_build", None)
    monkeypatch.setattr(mcp_server, "_upgrade_notice_given", False)
    yield
    build_id.reset_for_tests()
