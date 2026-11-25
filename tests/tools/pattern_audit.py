"""The pattern register's detector: every known way a test depends on something it should not, found by reading the test files.

`python3 -m tests.tools.pattern_audit` prints, per pattern, the files and lines that match (a hit is a place to LOOK at, not always a
fault). `tests/unit/test_pattern_register.py` fails when a file has MORE hits of a pattern than `pattern_baseline.json` allows, so a
pattern that was cleaned cannot come back. A line that is right on purpose carries `# pattern-ok: <pattern> <why>` and is not counted.

The register itself (name, how it is detected, the fix) is in `tests/README.md`. To add a pattern: add it to PATTERNS here, add its row
to the README, run `python3 -m tests.tools.pattern_audit --write-baseline`, and clean or justify the hits of EVERY file, accepted or not.
"""

from __future__ import annotations

import ast
import functools
import json
import re
import sys
from pathlib import Path
from typing import Dict, List

TESTS = Path(__file__).resolve().parents[1]
BASELINE = Path(__file__).with_name("pattern_baseline.json")

# Files of the guard and the recorder talk about these things on purpose: they are the tools that look for them.
NOT_AUDITED = {"tests/unit/test_pattern_register.py", "tests/tools/pattern_audit.py", "tests/tools/outside_guard.py", "tests/tools/test_outside_guard.py", "tests/tools/tier_report.py", "tests/tools/tier_summary.py"}

_LINE_PATTERNS = {
    "tempfile": r"\btempfile\.",
    "init-for-setup": r"""invoke\(\s*app\s*,\s*\[\s*["']init["']|\binit_command\(|["']init["']\s*[,\]]""",
    "outside-path": r"""["']/(?:nonexistent|no/such|tmp|home|media|usr|etc|var|opt|root|proc|dev)\b|\bPath\.home\(\)|expanduser|["']~/""",
    "skip-on-machine": r"\bskipif\(|\bpytest\.skip\(|\bimportorskip\(",
    "os-chdir": r"\bos\.chdir\(",
    "real-sleep": r"\btime\.sleep\(",
    "real-subprocess": r"\bsubprocess\.(?:run|Popen|check_output|check_call|call)\(|\bos\.system\(|\bos\.fork\(",
    "slow-socket-model-marker": r"pytest\.mark\.(?:slow|socket|model)\b",
    "environ-assigned": r"\bos\.environ\[[^\]]+\]\s*=(?!=)|\bos\.environ\.(?:update|setdefault|pop)\(",
    "duckdb-file": r"""\bduckdb\.connect\((?!\s*["']:memory:["'])|\bget_connection\(""",  # any argument: `get_connection(root / db)` and `(str(db_path))` were missed
    "git": r"""\[\s*["']git["']""",
    "test-module-import-in-test": r"^\s+(?:from tests\.unit(?:\.\w+)* import|import tests\.unit)",
    "real-time-assert": r"""\bwait4\(|\bgetrusage\(|\btracemalloc\b|\bprocess_time\(|\bperf_counter\(|/proc/|\bassert\b.*(?:elapsed|duration|execution_time|_seconds|seconds\b|took\b|\bmonotonic\(\)|time\.time\(\))|\bassert\b[^#\n]*<\s*\d+(?:\.\d+)?\s*(?:s\b|sec)""",
}

_ASSERTING = ("raises", "assert_", "pytest.fail", "xfail", "approx", "warns")


def _no_assert(tree: ast.AST, lines: List[str]) -> List[int]:
    out = []
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name.startswith("test_"):
            if not any(isinstance(n, ast.Assert) for n in ast.walk(node)):
                text = "\n".join(lines[node.lineno - 1:node.end_lineno])
                if not any(word in text for word in _ASSERTING):
                    out.append(node.lineno)
    return out


_COPY_COMMENT = re.compile(r"#.*\b(?:local cop(?:y|ies)|copy of (?:the )?(?:product|source|function))", re.IGNORECASE)
_COPY_MIN_STATEMENTS = 8  # a helper shorter than this is a helper, not a copy of product code


@functools.lru_cache(maxsize=1)
def _product_function_names() -> frozenset:
    """The names (without leading underscores) of the module-level functions of `src/drspec` (methods are not: `run`, `embed`, `execute` are
    names of a hundred classes)."""
    names = set()
    for path in (TESTS.parent / "src" / "drspec").rglob("*.py"):
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"))
        except (SyntaxError, UnicodeDecodeError):
            continue
        names.update(n.name.lstrip("_") for n in tree.body if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)))
    return frozenset(names)


def _local_copies(tree: ast.AST, lines: List[str]) -> List[tuple]:
    """A module-level function of a test file, not a test and not a fixture, of more than `_COPY_MIN_STATEMENTS` statements, that has the name of a
    module-level function of `src/drspec` (leading underscores ignored): a copy of product code that the tests then test instead of the product.
    Also any comment that says it is a local copy. Returns `(first line, last line)` of each: a `# pattern-ok:` on any of those lines justifies it."""
    product = _product_function_names()
    out = []
    for node in getattr(tree, "body", []):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) or node.name.startswith("test_"):
            continue
        if any("fixture" in ast.dump(d) for d in node.decorator_list) or node.name.lstrip("_") not in product:
            continue
        statements = sum(1 for n in ast.walk(node) if isinstance(n, ast.stmt)) - 1
        if statements > _COPY_MIN_STATEMENTS:
            out.append((node.lineno, node.end_lineno))
    for number, line in enumerate(lines, 1):
        if _COPY_COMMENT.search(line):
            out.append((number, number))
    return out


PATTERNS: Dict[str, str] = {
    "tempfile": "`tempfile.*`: writes to the system temp folder, outside the repository and pytest's control. Fix: `tmp_path`, or nothing.",
    "init-for-setup": "a real `drspec init` (CLI or `init_command`) used to set up a project. Fix: no project, a template copied per test, or in-memory DuckDB + `init_schema`.",
    "outside-path": "a path outside the repository and tmp_path (`/nonexistent`, `/tmp`, the home folder). Fix: a path under `tmp_path`.",
    "skip-on-machine": "a `skip`/`skipif` that depends on the machine. Fix: a committed fixture or a fake, so the test always runs.",
    "os-chdir": "`os.chdir` (global state). Fix: `monkeypatch.chdir`, or pass the path.",
    "real-sleep": "`time.sleep` in a test. Fix: the fake clock (`FakeTime`) or `wait_until` for a real process.",
    "real-subprocess": "a real subprocess or `os.fork`. Fix: `InProcessPython`, a fake runner; or integration marker with the reason.",
    "slow-socket-model-marker": "a `slow`/`socket`/`model` marker: the test needs a real worker, socket or model weights. Fix: a fake, or integration with the reason.",
    "environ-assigned": "`os.environ[...] =` / update / setdefault / pop (leaks into the next test). Fix: `monkeypatch.setenv/delenv`.",
    "duckdb-file": "a DuckDB FILE opened where only SQL is checked. Fix: `duckdb.connect(':memory:')` + `init_schema`, or `kept_duckdb`.",
    "git": "`git` started. Fix: `tests.fakes.git_listing` / `FakeGit`. Banned in every tier.",
    "real-time-assert": "a test that asserts on real time, CPU or memory (`wait4`, `getrusage`, `/proc`, `process_time`, `perf_counter`, `tracemalloc`, 'x < N s'). Time budgets only REPORT. Fix: prove what it wants (a loop sleeps, a read is streamed, a batch is cut) with a count, `FakeTime` or a fake size.",
    "test-module-import-in-test": "an import of another TEST module inside a test: the first import writes bytecode into the (read-only) repository, so the test fails when run alone. Fix: move the shared helper to tests/fakes.py or tests/helpers.py and import it at the top.",
    "no-assert": "a test with no assert/raises (cannot fail except by crashing). Fix: assert what it is for, or delete it with the reason.",
    "local-copy-of-product-code": "a test file that DEFINES a function (more than 8 statements) with the name of a function of `src/drspec`, or says 'local copy'/'copy of the product': the tests then test the copy, and the product can break with every test green. Fix: import the product's function and test that.",
}


def _kept_lines(text: str) -> set:
    """The lines of one file where a DuckDB `connect(<path>)` is the kept in-memory database, counted PER TEST: a fixture that asks for `kept_duckdb` /
    `kept_project` (directly, or through another fixture of the file) makes the tests that use it kept; the lines of such a test are kept; so are the
    lines of a helper that is not a test (it is called by tests) when the file has any kept fixture. A test of the same file that does not use one is
    NOT kept: its hits count."""
    try:
        tree = ast.parse(text)
    except SyntaxError:
        return set()
    functions = [n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef)]

    def params(fn):
        return {a.arg for a in fn.args.args + fn.args.kwonlyargs}

    def is_fixture(fn):
        return any("fixture" in ast.dump(d) for d in fn.decorator_list)

    kept = {"kept_duckdb", "kept_project"}
    changed = True
    while changed:  # a fixture of this file that uses a kept one is kept too
        changed = False
        for fn in functions:
            if is_fixture(fn) and fn.name not in kept and params(fn) & kept:
                kept.add(fn.name)
                changed = True
    for node in ast.walk(tree):  # `pytestmark = pytest.mark.usefixtures("kept_duckdb")` keeps the whole file
        if isinstance(node, ast.Assign) and any(getattr(t, "id", "") == "pytestmark" for t in node.targets) and "kept_" in ast.dump(node.value):
            return set(range(1, len(text.split("\n")) + 1))
    lines: set = set()
    any_kept_fixture = any(is_fixture(fn) and fn.name in kept for fn in functions)
    for fn in functions:
        if fn.name.startswith("test_"):
            if params(fn) & kept:
                lines.update(range(fn.lineno, fn.end_lineno + 1))
        elif not is_fixture(fn) and any_kept_fixture:
            lines.update(range(fn.lineno, fn.end_lineno + 1))  # a helper
        elif is_fixture(fn) and fn.name in kept:
            lines.update(range(fn.lineno, fn.end_lineno + 1))
    return lines


def detect(path: Path) -> Dict[str, List[int]]:
    """Pattern -> line numbers of the hits in one file (a line with `# pattern-ok: <pattern>` is not a hit)."""
    text = path.read_text(encoding="utf-8")
    lines = text.split("\n")
    hits: Dict[str, List[int]] = {}
    kept_lines = _kept_lines(text)  # lines where `connect(path)` is memory: the tests (and helpers) of a file that get `kept_duckdb`/`kept_project`
    for name, regex in _LINE_PATTERNS.items():
        compiled = re.compile(regex)
        for number, line in enumerate(lines, 1):
            stripped = line.strip()
            if stripped.startswith("#") or f"pattern-ok: {name}" in line:
                continue
            if compiled.search(line):
                if name == "duckdb-file" and number in kept_lines:
                    continue
                hits.setdefault(name, []).append(number)
    try:
        tree = ast.parse(text)
    except SyntaxError:
        return hits
    found = [n for n in _no_assert(tree, lines) if "pattern-ok: no-assert" not in lines[n - 1]]
    if found:
        hits["no-assert"] = found
    copies = [first for first, last in _local_copies(tree, lines) if not any("pattern-ok: local-copy-of-product-code" in lines[i - 1] for i in range(first, last + 1))]
    if copies:
        hits["local-copy-of-product-code"] = sorted(set(copies))
    return hits


def audit() -> Dict[str, Dict[str, List[int]]]:
    """Relative path -> pattern -> lines, for every test file."""
    result: Dict[str, Dict[str, List[int]]] = {}
    for path in sorted(TESTS.rglob("*.py")):
        relative = path.relative_to(TESTS.parent).as_posix()
        if relative in NOT_AUDITED or "__pycache__" in relative or "/fixtures/" in relative:
            continue
        hits = detect(path)
        if hits:
            result[relative] = hits
    return result


def counts(result: Dict[str, Dict[str, List[int]]]) -> Dict[str, Dict[str, int]]:
    return {file: {pattern: len(lines) for pattern, lines in hits.items()} for file, hits in result.items()}


def main(argv: List[str]) -> int:
    result = audit()
    if "--sites" in argv:  # `--sites duckdb-file`: every hit of the pattern, `file:line: text` (the owner of a file reads its own)
        wanted = argv[argv.index("--sites") + 1]
        for file, hits in result.items():
            lines = (TESTS.parent / file).read_text(encoding="utf-8").split("\n")
            for number in hits.get(wanted, []):
                print(f"{file}:{number}: {lines[number - 1].strip()[:110]}")
        return 0
    if "--write-baseline" in argv:
        BASELINE.write_text(json.dumps(counts(result), indent=1, sort_keys=True) + "\n")
        print(f"baseline written: {len(result)} files")
        return 0
    per_pattern: Dict[str, List[str]] = {}
    for file, hits in result.items():
        for pattern, lines in hits.items():
            per_pattern.setdefault(pattern, []).append(f"{file} ({len(lines)})")
    for pattern in PATTERNS:
        files = per_pattern.get(pattern, [])
        print(f"{pattern}: {sum(int(f.rsplit('(', 1)[1][:-1]) for f in files)} hits in {len(files)} files")
        if "--files" in argv:
            for f in files:
                print("   ", f)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
