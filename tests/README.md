# Tests

Every test has a tier. The tier says what the test may touch and how much CPU time it may use.

| Tier | May use | May not use | CPU budget per test |
|---|---|---|---|
| `unit` | pure code, in-memory data, text files in `tmp_path` | a process, a socket, a sleep, a DuckDB file, the CLI runner | 50 ms |
| `component` | in-process code: a DuckDB file in `tmp_path`, the CLI through typer's `CliRunner`, parsers on small sources | a process, a socket, a sleep | 250 ms |
| `integration` | anything: a worker, a parser pool, a subprocess, a real project | (may sleep and spawn) | 5 s |

The `slow`, `socket` and `model` markers imply `integration`.

Lane targets: the unit lane (`-m unit`) under 30 s, the component lane (`-m "unit or component"`) under 90 s, and at most 40 integration tests.

## Commands

- Unit tests only: `python3 -m pytest -m unit`
- Unit and component tests: `python3 -m pytest -m "unit or component"`
- Integration tests only: `python3 -m pytest -m integration`
- The quick lane (the default run): `python3 -m pytest -q -p no:cacheprovider -rf tests`

The quick lane runs everything that is not `slow`, `socket` or `model`. It is the command the reviewer runs.

## Database checkpoints in tests

`test_scan_pipeline.py` skips DuckDB's checkpoint when a project database closes (`PRAGMA disable_checkpoint_on_shutdown`). The checkpoint writes the whole database to disk, which costs about 0.4 s a close on a shared disk. A test's database is removed with its folder, so the checkpoint buys nothing there; the write-ahead log still holds every change and the next open reads it. Other test files do not do this.

## Nothing outside the repository and `tmp_path`; the repository is read-only

A test READS only the repository (committed files), its own `tmp_path` and the folders of the Python that runs it, and WRITES only in
this session's temporary folder. The repository is read-only for tests: no `_drspec`, no `.gitignore`, no `old.db` in it, no rename, no
`chdir` that leaves a file. No sibling clone of a project, no home folder, no `/tmp` except through `tmp_path`, no absolute path of a
developer's machine, no socket file or database outside, no network. A test then gives the same result on any machine and leaves nothing
behind. Two things hold it, for every test, in every tier:

- **The session is isolated** (`tests/conftest.py`, `_isolated_environment`, one `MonkeyPatch` undone at the end): `HOME`, `USERPROFILE`, `TMPDIR`, `TEMP`,
  `TMP`, `XDG_CACHE_HOME`, `XDG_CONFIG_HOME` and `XDG_DATA_HOME` are folders under pytest's own temporary folder, `tempfile.tempdir` is reset
  to it, `PATH` is an empty folder of that area plus the folder of the Python (so `shutil.which` does not walk the machine's `PATH`),
  `DRSPEC_MODEL_DIR` is an empty folder of it (no weights: a test that needs an embedder fakes it), `DRSPEC_DISABLE_AUTO_WORKER=1`, and
  `PYTHONUSERBASE` keeps the user's Python packages for a Python a test starts. The free memory the code asks the kernel for is a fixed 8 GB
  (4 GB in the trace snapshot) unless a test says another number.
- **A guard checks every test** (`tests/tools/outside_guard.py`, registered by `conftest.py`): the calls that open, list, look at, create, change,
  copy, move or remove a path (`open`, `os.*`, `shutil.*`, `pathlib.Path`), `duckdb.connect` and `sqlite3.connect` on a file, `socket.bind` and
  `connect` on a path and the `cwd` of a subprocess are wrapped (the list is at the head of the module). A READ outside the repository, the
  Python's folders and this session's `basetemp`, or any WRITE outside this session's `basetemp` (the repository included), is refused
  (`OutsideAccess`, a `PermissionError`) and the test FAILS, naming the first path and the call. The folders above the area may be looked
  at (`stat`). Only THIS session's `basetemp` is allowed, not every `pytest-of-<user>` folder. A relative name given with `dir_fd` (what
  `shutil.rmtree` does) is resolved against the open folder, not the current directory. `DRSPEC_GUARD_STACK=1` prints who asked.
- **Nothing is hidden**: two things are answered, not refused, and every run prints how many (`outside guard: what it answered or left
  unrecorded`): a project marker looked for above the area (`.git`, `pyproject.toml`, `_drspec`, `pytest.ini`, ... made by `init`'s walk up from
  `tmp_path`) is answered "absent" whatever the machine has there, and what a third-party library reads while it is being imported
  (`requests`, `onnxruntime`) is left unrecorded and counted by library. The text of an `ATTACH` inside SQL is out of the guard's sight (it
  sees the path given to `connect`).
- A test that needs a real thing outside says so with `@pytest.mark.outside(reason=...)`; the run lists those tests. **Today: 0.**

Real-code checks that read a clone of another project are not tests: they are scripts run by hand when a parser changes. **They are the
only place the real raylib and Dear ImGui are read**: the clone is the argument (it can be anywhere; the scripts need a checkout of each, no
other setup), and no test looks for one: a reader of the tests must not search for the corpora there.
(`python3 scripts/corpus_check_c.py /path/to/raylib`, `python3 scripts/corpus_check_cpp.py /path/to/imgui`; the recorded compiler answers and the
recorders are under `scripts/`). What they found is pinned in the tests by small committed excerpts under `tests/fixtures/`:

| Found by the corpus sweep | Pinned by |
|---|---|
| a function read as one of 5,135 lines that held 116 others (`ma_context_init__coreaudio`) | `test_cpp_conditionals.py` (the swallow rule `_is_a_function_of_its_own`: one in which another starts, a name not where it starts, one that starts inside a macro) |
| a function's text that started in the `#define` above it, or in the use of a macro that defines another | `test_cpp_conditionals.py::test_the_text_of_a_function_does_not_start_in_the_define_before_it` and `..._in_the_use_of_a_macro_that_defines_another` |
| functions lost in the `#else` branch of a conditional, and a declaration an `#else` branch adds | `test_cpp_conditionals.py` (the functions of every branch, a C file reads the declaration an else branch adds, a branch with no definition is not parsed) |
| `if (...) {` lines inside unreadable code, and code shown in comments, counted as lost functions | `test_c_support.py::TestVendoredLibraries` (a control statement in an unreadable stretch; code shown in a comment) |
| losses inside `#ifdef __cplusplus` counted for a C file (`m3d.h`) | `test_c_support.py::TestWhatTheCompilerSeesAndWhatItDoesNot` (the lines only a C++ compiler reads; a loss in a `__cplusplus` block) |
| a function a macro defines (`MA_DR_FLAC_DEFINE_FULL_READ_AND_CLOSE`) not reported by name | `test_c_support.py::TestWhatTheCompilerSeesAndWhatItDoesNot` (a function a macro defines is reported by the use that makes it) |
| raw strings and macros above a function hiding it (`RGFW.h`, `raygui.h`) | `test_cpp_conditionals.py` (a shader in a raw string is not a function; a delimiter is honoured; a function after it is found) |
| constructs only C has (K&R, function-pointer returns, C++ keywords as names, statement expressions) | `test_c_support.py::TestTheConformanceFile` (25 constructs of `fixtures/c/constructs.c`) and `TestTheCOnlyConstructs` |
| the C++ cases the ImGui sweep found (a macro between string literals, braces split by an `#if`, a calling-convention macro, ...) | `test_cpp_recovery.py` (the parametrized recovery cases) |

## Fakes

Use a fake before anything real. The shared ones:

- `no_git` (fixture, `tests/conftest.py`): the answer of `project_files.inside_git` is "no", so no test starts `git`. Put `pytestmark = pytest.mark.usefixtures("no_git")` at the top of a file that lists a project.
- `fake_time` (fixture) and `tests.fakes.FakeTime`: a clock that moves only when something sleeps, with `.slept` listing the sleeps. `monkeypatch.setattr(module, "time", fake_time)` makes a retry loop or a timeout take no time.
- `tests.fakes.fake_socket_pair()`: two connected in-memory sockets (`recv`, `sendall`, `close`), so a client and a server can talk without an operating-system socket or a file. `test_worker_ipc.py` serves a `WorkerIPCServer` to a `WorkerConnection` this way.
- `fixed_build` (fixture): drspec's build id is a fixed string, so an MCP `tools/call` does not hash the package (6 to 19 ms a call). Use it in a file that calls many tools.
- An in-memory DuckDB: `duckdb.connect(":memory:")` plus `init_schema(conn)`, about 15 ms; use it before a database file.
- `tests/unit/test_staleness_worker.py` shows the two bigger fakes: `remediation` (the re-scan replaced by a recorder that settles its batch) and `_World` (everything `run_worker` reaches).

## Finding a test's tier

Run a file with the recorder and read the table it prints:

    python3 -m pytest -p tests.tools.tier_report tests/unit/test_version.py

The table shows, per test, the tier implied by what the call did, its CPU time, and the forbidden calls it made (`process`, `socket`, `sleep`, `duckdb`, `cli`). The recorder changes nothing and fails nothing. Load it only for measurement: it wraps `subprocess.Popen`, `os.fork`, `time.sleep` and the socket constructor for the whole session.

## Disk work, counted (report only)

CPU time hides the flushes of a DuckDB file, and wall time depends on what else the machine does. With `DRSPEC_TIER_REPORT=1` the end-of-run report also prints the disk work as COUNTS of requests (`tests/tools/disk_counts.py`): `commits` (a write statement outside `BEGIN`..`COMMIT`, and each `COMMIT`), `checkpoints`, `closes` of a file database, `fsyncs`, `connects` to a file (`:memory:` costs nothing). The counts are the same on a busy machine and a quiet one, so they are the figure to cut; the report also prints the load average at the start and the end of the run and the 10 tests with the most disk work. It fails nothing.

The session marks `pandas`, `pyarrow` and `polars` absent (`tests/conftest.py`), as `drspec.db.connection` does when they are not installed: DuckDB otherwise imports pandas and numpy at the first parameterized query of every process, about 1 s of CPU that no test uses.

## Known load-sensitive tests

A test that starts a real worker and runs `drspec` as a SEPARATE process meets the product's fixed 15 s wait for a worker to answer (`src/drspec/worker_ipc.py`, `CONNECT_TIMEOUT_SECONDS`; no setting). On a machine that is saturated (4 busy processes, load 6-7 on 12 cores) these failed about 7% of the time (3 of 40 runs) with `DATABASE_BUSY`; on a quiet machine they pass. They stay in the `slow` lane (a decision about leaving the default and CI lanes is the person's, not the tests'):

- `test_scan_cancel.py::TestWithARealWorker` (Ctrl-C on a real scan)
- `test_parse_pool.py::TestARealScanOnSeveralCores` (a scan with one and with several parser processes)
- the `slow` real-worker tests of `test_drain.py`, `test_worker_build.py`, `test_init_indexing.py`, `test_parser_version.py`, `test_scan_with_worker.py`, `test_followup_regressions.py`, `test_identity_migration.py` (not yet reviewed for this)

The "Lost contact / Broken pipe" failure of `test_worker_ipc::TestBackgroundMaintenanceEndToEnd` was NOT load: the identity of the code includes the modification times of the source files, and a source file touched during a run made the client replace the worker. The session pins the identity now (`DRSPEC_BUILD_ID` in `conftest.py`).

## Where a tier is set

In this order:

1. A marker on the test (`@pytest.mark.unit`, `component` or `integration`).
2. A `pytestmark` at the top of the file.
3. `tests/tiers.json`, keyed by the test's node id.

A test with none of these has no tier. The tier rule that fails such a test is not active yet; until it is, a test with no tier is reported as such.

## Writing a new test

- A test that needs no database, no process and no file is `unit`. Put the logic in a function that takes its inputs as arguments, and test the function directly.
- A test that needs a DuckDB file or the CLI is `component`. Use `tmp_path` for the file.
- Only a test that starts a worker or a process, or sleeps, is `integration`. Keep those few.

## Pattern register

Every way a test can depend on something it should not is a pattern. A test file is only "done" for the patterns known when it was
done, so a NEW pattern is run over ALL test files, accepted ones too. The detector is `tests/tools/pattern_audit.py`
(`python3 -m tests.tools.pattern_audit --files` lists the files and counts; `--sites <pattern>` lists every hit as `file:line: text`, so that the owner of a file reads its own, e.g. `--sites duckdb-file`); `tests/unit/test_pattern_register.py` fails when a file has
more hits of a pattern than `tests/tools/pattern_baseline.json` allows, so a cleaned pattern cannot come back. A line that is right on
purpose says so: `# pattern-ok: <pattern> <why>`.

To add a pattern: add it to `PATTERNS` and the regex table in the detector, add a row here, run
`python3 -m tests.tools.pattern_audit --write-baseline`, then clean or justify the hits of every file.

| pattern | detected by | fix |
|---|---|---|
| tempfile | `tempfile.` | `tmp_path` / `tmp_path_factory`, or no folder at all |
| init-for-setup | `invoke(app, ["init"...`, `init_command(` | no project; a template project copied per test; in-memory DuckDB + `init_schema` |
| outside-path | an absolute path literal (`/nonexistent`, `/tmp`, `~`, `Path.home()`) | a path under `tmp_path` |
| skip-on-machine | `skipif`, `pytest.skip`, `importorskip` | a committed fixture or a fake, so the test always runs (`unreadable`, `not_writable` for "root can read anything") |
| os-chdir | `os.chdir(` | `monkeypatch.chdir` (`pytest.MonkeyPatch.context()` in a module fixture) |
| real-sleep | `time.sleep(` | `FakeTime`, or `wait_until` for a real process |
| real-subprocess | `subprocess.run/Popen`, `os.fork`, `os.system` | `InProcessPython`, a fake runner; or `integration` with the reason |
| slow-socket-model-marker | `pytest.mark.slow/socket/model` | a fake; or `integration` with the reason |
| environ-assigned | `os.environ[...] =`, `.update`, `.setdefault`, `.pop` | `monkeypatch.setenv` / `delenv` |
| duckdb-file | `duckdb.connect(<not :memory:>)`, `get_connection(<path>)`; counted PER TEST: not counted in a test that uses `kept_duckdb` / `kept_project` (or a fixture of its file that does), nor in a helper of a file that has such a fixture | `duckdb.connect(':memory:')` + `init_schema`, `kept_duckdb`, or the `kept_project` fixture (a folder + an in-memory project database, no `init`) |
| git | `["git"` | `git_listing`, `FakeGit` (git is banned in every tier) |
| test-module-import-in-test | an indented `from tests.unit... import` | the helper goes to `tests/fakes.py` / `tests/helpers.py`, imported at the top of the file |
| no-assert | a `test_` function without assert/raises | assert what it is for, or delete it with the reason |
| local-copy-of-product-code | a non-test function of more than 8 statements with the name of a function in `src/drspec`; a comment saying 'local copy' / 'copy of the product' | import the product's function and test that; if a test fails, the copy had drifted: report the difference |
| real-time-assert | `wait4`, `getrusage`, `/proc/`, `process_time`, `perf_counter`, `tracemalloc`, an assert on elapsed time | a count, `FakeTime` or a fake size. Time budgets REPORT slow tests; a test never fails because of time |
