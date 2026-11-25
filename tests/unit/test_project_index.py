"""The module, package and class lookups of `project_facts` answered from one read of the artifacts give what the query
gives, and ask the database once for a whole publish."""

from __future__ import annotations


import duckdb
import pytest

from drspec.core import project_facts as pf
from drspec.core.graph_sync import sync_scan_results
from drspec.core.scanner import Scanner
from drspec.db import init_schema
from tests.helpers import table_counts

TREE = {
    "pkg/__init__.py": "",
    "pkg/alpha.py": "def run():\n    return 1\n\n\nclass Box:\n    def open(self):\n        return 2\n",
    "pkg/beta.py": "def run():\n    return 3\n\n\ndef only_beta():\n    return 4\n",
    "pkg/sub/gamma.py": "def deep():\n    return 5\n\n\nclass Box:\n    def close(self):\n        return 6\n",
    "other/pkg/alpha.py": "def run():\n    return 7\n\n\ndef other_only():\n    return 8\n",
    "utils/helper.py": "def assist():\n    return 9\n",
    "utils/nested/utils/helper.py": "def assist_nested():\n    return 10\n",  # the directory name twice in one path
    "utils/nested/inner.py": "def inner_fn():\n    return 11\n",
    "my_pkg/my_mod.py": "def under_score():\n    return 12\n\n\nclass Under:\n    def go(self):\n        return 13\n",
    "my/pkg/mod.py": "def slash_pair():\n    return 14\n",  # what `my_pkg/my_mod` would match if `_` were a character and not a wildcard
    "dotted.name/thing.py": "def in_dotted():\n    return 15\n",
    "solo.py": "def solo_fn():\n    return 16\n",
}

MODULES = [
    "pkg", "pkg.alpha", "pkg.beta", "pkg.sub", "pkg.sub.gamma", "alpha", "beta", "gamma", "run", "other.pkg", "other.pkg.alpha",
    "utils", "utils.helper", "helper", "utils.nested", "nested", "nested.utils", "utils.nested.utils", "inner", "utils.nested.inner",
    "my_pkg", "my_pkg.my_mod", "my_mod", "my.pkg", "my.pkg.mod", "mod", "dotted", "dotted.name", "thing", "dotted.name.thing", "solo",
    "nothing", "pkg.nothing", ".alpha", "..pkg", "./beta", "pkg/alpha", "a..b",
]
CLASSES = ["Box", "Under", "Missing", "Box.Inner"]


@pytest.fixture(scope="module")
def connection(tmp_path_factory):
    """The scanned tree in a database in memory, built ONCE for the module: every test only reads it (the rows are counted again at the end)."""
    root = tmp_path_factory.mktemp("tree")
    for name, text in TREE.items():
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)
    scanner = Scanner()
    functions, imports = [], {}
    with pytest.MonkeyPatch.context() as patch:
        patch.chdir(root)
        for name in sorted(TREE):
            result = scanner.scan_file(name, relative_to=root)
            functions += result.functions
            imports.update(result.file_imports)
    conn = duckdb.connect(":memory:")
    init_schema(conn)
    sync_scan_results(conn, functions, set(TREE), imports)
    before = table_counts(conn)
    yield conn
    assert table_counts(conn) == before, "a test wrote into the database the module shares"
    conn.close()


class TestTheSameAnswersAsTheQueries:
    def test_module_files(self, connection):
        cache: dict = {}
        for module in MODULES:
            assert pf._guess_module_file_path(connection, module, cache) == pf._guess_module_file_path(connection, module), module

    def test_the_functions_of_a_package(self, connection):
        cache: dict = {}
        for module in MODULES:
            package = pf._module_path_to_package_dir(module)
            if package is None:
                continue
            assert pf._functions_by_name_in_package(connection, package, cache) == pf._functions_by_name_in_package(connection, package), package

    def test_the_file_of_a_class_in_a_package(self, connection):
        cache: dict = {}
        for module in MODULES:
            for name in CLASSES:
                assert pf._guess_package_reexport_class_file(connection, module, name, cache) == pf._guess_package_reexport_class_file(connection, module, name), (module, name)

    def test_the_cases_the_tree_was_built_for_are_answered_as_expected(self, connection):
        cache: dict = {}
        # a directory name that stands twice in a path is not the directory of that file: the query leaves it out
        assert pf._functions_by_name_in_package(connection, "utils", cache) == pf._functions_by_name_in_package(connection, "utils")
        assert "assist_nested" not in pf._functions_by_name_in_package(connection, "utils", cache)
        assert "assist" in pf._functions_by_name_in_package(connection, "utils", cache)
        # the same name in two files of a package is dropped, in one it is kept
        assert "run" not in pf._functions_by_name_in_package(connection, "pkg", cache)
        assert "only_beta" in pf._functions_by_name_in_package(connection, "pkg", cache)
        # a stem two files share is ambiguous
        assert pf._guess_module_file_path(connection, "alpha", cache) is None
        assert pf._guess_module_file_path(connection, "gamma", cache) == "pkg/sub/gamma.py"
        assert pf._guess_module_file_path(connection, "dotted", cache) == pf._guess_module_file_path(connection, "dotted")

    def test_names_with_a_like_wildcard_are_asked_of_the_database(self, connection):
        # `my_pkg`: in the query `_` matches any character, so it matches `my/pkg` too; the index does not copy that, it
        # does not answer for such a name at all
        cache: dict = {}
        assert pf._WILDCARDS.search("my_pkg") and pf._WILDCARDS.search("a//b") and not pf._WILDCARDS.search("pkg/sub")
        assert pf._functions_by_name_in_package(connection, "my_pkg", cache) == pf._functions_by_name_in_package(connection, "my_pkg")
        assert ("project index",) not in cache  # nothing was read for it


class TestOneReadForAWholePublish:
    class _Counting:
        def __init__(self, inner):
            self.inner = inner
            self.statements = []

        def execute(self, sql, *args, **kwargs):
            self.statements.append(" ".join(sql.split()))
            return self.inner.execute(sql, *args, **kwargs)

        def __getattr__(self, name):
            return getattr(self.inner, name)

    def test_the_artifacts_are_read_a_few_times_and_not_once_per_name(self, connection):
        counting = self._Counting(connection)
        cache: dict = {}
        for _ in range(5):
            for module in MODULES:
                pf._guess_module_file_path(counting, module, cache)
                package = pf._module_path_to_package_dir(module)
                if package is not None:
                    pf._functions_by_name_in_package(counting, package, cache)
                    for name in CLASSES:
                        pf._guess_package_reexport_class_file(counting, module, name, cache)
        reads = [sql for sql in counting.statements if "FROM artifacts" in sql]
        wildcard_modules = [m for m in MODULES if pf._WILDCARDS.search((pf._module_path_to_package_dir(m) or "")) or any(pf._WILDCARDS.search(c) for c in m.replace("/", ".").split(".") if c)]
        # the index is three reads; the rest are the names the query still answers (each as often as it is asked)
        assert len(reads) <= 3 + 5 * (len(wildcard_modules) * (2 + len(CLASSES)))
        fast = [m for m in MODULES if m not in wildcard_modules]
        assert fast, "the tree should have names the index answers"
        before = len(counting.statements)
        for module in fast:
            pf._guess_module_file_path(counting, module, cache)
        assert len(counting.statements) == before  # answered from memory
