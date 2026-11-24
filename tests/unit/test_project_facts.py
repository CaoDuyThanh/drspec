"""Tests for DB-backed project-wide facts: pure DB aggregation and import
resolution against already-scanned artifacts, never text parsing of its own.
"""


import duckdb
import pytest

from drspec.core.project_facts import (
    bind_declared_types,
    build_classes_by_name,
    build_project_function_index,
    build_properties_by_name,
    resolve_module_imports,
    resolve_name_imports,
)
from drspec.db.connection import init_schema
from drspec.db.queries import insert_artifact
from drspec.parsers.models import ParseResult, RawModuleImport, RawNameImport


@pytest.fixture
def db_conn():
    """An in-memory schema per test: the functions under test answer SQL over `artifacts`, and nothing needs a file."""
    conn = duckdb.connect(":memory:")
    init_schema(conn)
    yield conn
    conn.close()


def _insert(conn, function_id, file_path, function_name, parent=None, is_property=False):
    insert_artifact(
        conn,
        function_id=function_id,
        file_path=file_path,
        function_name=function_name,
        signature=f"def {function_name}():",
        body="pass",
        code_hash=function_id,
        language="python",
        start_line=1,
        end_line=2,
        parent=parent,
        is_property=is_property,
    )


class TestBuildClassesByName:
    def test_groups_methods_by_parent(self, db_conn):
        _insert(db_conn, "a.py::Order.process", "a.py", "process", parent="Order")
        _insert(db_conn, "a.py::standalone", "a.py", "standalone")

        classes = build_classes_by_name(db_conn)

        assert classes == {"a.py::Order": {"process": ["a.py::Order.process"]}}

    def test_empty_when_no_classes(self, db_conn):
        _insert(db_conn, "a.py::standalone", "a.py", "standalone")

        assert build_classes_by_name(db_conn) == {}


class TestBuildPropertiesByName:
    """The `references` counterpart of `build_classes_by_name`."""

    def test_includes_only_property_methods(self, db_conn):
        _insert(db_conn, "a.py::Calculator.value", "a.py", "value", parent="Calculator", is_property=True)
        _insert(db_conn, "a.py::Calculator.compute", "a.py", "compute", parent="Calculator", is_property=False)

        properties = build_properties_by_name(db_conn)

        assert properties == {"a.py::Calculator": {"value": "a.py::Calculator.value"}}

    def test_excludes_top_level_functions_even_if_flagged(self, db_conn):
        """`is_property` only ever gets set True on a method by the
        parser — defensive coverage that a top-level function (no
        `parent`) is still excluded regardless."""
        _insert(db_conn, "a.py::standalone", "a.py", "standalone", parent=None, is_property=True)

        assert build_properties_by_name(db_conn) == {}

    def test_empty_when_no_properties(self, db_conn):
        _insert(db_conn, "a.py::Calculator.compute", "a.py", "compute", parent="Calculator")

        assert build_properties_by_name(db_conn) == {}


class TestBuildProjectFunctionIndex:
    def test_indexes_every_artifact_by_name(self, db_conn):
        _insert(db_conn, "a.py::foo", "a.py", "foo")
        _insert(db_conn, "b.py::foo", "b.py", "foo")

        index = build_project_function_index(db_conn)

        assert set(index["foo"]) == {"a.py::foo", "b.py::foo"}


class TestResolveNameImports:
    def test_resolves_to_function_id(self, db_conn):
        _insert(db_conn, "utils.py::helper", "utils.py", "helper")

        resolved = resolve_name_imports(db_conn, [RawNameImport(module_path="utils", name="helper")])

        assert resolved == {"helper": ["utils.py::helper"]}

    def test_respects_alias(self, db_conn):
        _insert(db_conn, "utils.py::helper", "utils.py", "helper")

        resolved = resolve_name_imports(
            db_conn, [RawNameImport(module_path="utils", name="helper", alias="h")]
        )

        assert resolved == {"h": ["utils.py::helper"]}

    def test_unmatched_module_resolves_to_nothing(self, db_conn):
        resolved = resolve_name_imports(db_conn, [RawNameImport(module_path="nonexistent", name="helper")])

        assert resolved == {}

    def test_ambiguous_module_stem_resolves_to_nothing(self, db_conn):
        """Two files share the same stem — never guess which one."""
        _insert(db_conn, "pkg_a/utils.py::helper", "pkg_a/utils.py", "helper")
        _insert(db_conn, "pkg_b/utils.py::helper", "pkg_b/utils.py", "helper")

        resolved = resolve_name_imports(db_conn, [RawNameImport(module_path="utils", name="helper")])

        assert resolved == {}

    def test_does_not_collide_with_a_same_named_method(self, db_conn):
        """Regression (found via dogfooding a real JS fixture): a free
        function and a same-named method in the same file (e.g. a free
        `add()` and a `Calculator.add` method) must not collide — the
        import binds to the free function, never silently to whichever
        one the DB happened to return last."""
        _insert(db_conn, "math_utils.js::add", "math_utils.js", "add")
        _insert(db_conn, "math_utils.js::Calculator.add", "math_utils.js", "add", parent="Calculator")

        resolved = resolve_name_imports(db_conn, [RawNameImport(module_path="math_utils", name="add")])

        assert resolved == {"add": ["math_utils.js::add"]}

    def test_name_not_found_in_matched_file_resolves_to_nothing(self, db_conn):
        _insert(db_conn, "utils.py::helper", "utils.py", "helper")

        resolved = resolve_name_imports(db_conn, [RawNameImport(module_path="utils", name="missing")])

        assert resolved == {}


class TestResolveModuleImports:
    def test_resolves_module_functions(self, db_conn):
        _insert(db_conn, "utils.py::helper", "utils.py", "helper")
        _insert(db_conn, "utils.py::other", "utils.py", "other")

        resolved = resolve_module_imports(db_conn, [RawModuleImport(module_path="utils", alias="utils")])

        assert resolved == {"utils": {"helper": ["utils.py::helper"], "other": ["utils.py::other"]}}

    def test_excludes_methods_from_the_module_namespace(self, db_conn):
        """Only top-level functions are reachable via `module.name(...)`
        — a method can't collide with a same-named free function here."""
        _insert(db_conn, "math_utils.js::add", "math_utils.js", "add")
        _insert(db_conn, "math_utils.js::Calculator.add", "math_utils.js", "add", parent="Calculator")

        resolved = resolve_module_imports(db_conn, [RawModuleImport(module_path="math_utils", alias="m")])

        assert resolved == {"m": {"add": ["math_utils.js::add"]}}

    def test_no_alias_is_skipped(self, db_conn):
        _insert(db_conn, "utils.py::helper", "utils.py", "helper")

        resolved = resolve_module_imports(db_conn, [RawModuleImport(module_path="utils", alias=None)])

        assert resolved == {}

    def test_unmatched_module_resolves_to_nothing(self, db_conn):
        resolved = resolve_module_imports(db_conn, [RawModuleImport(module_path="nonexistent", alias="x")])

        assert resolved == {}


class TestPackageReexportFallback:
    """Package-level imports like `from drspec.db import get_artifact`, where
    `drspec.db` names a directory (via `__init__.py` re-export) rather than a
    file `_guess_module_file_path` can match directly — this project's own
    dominant import convention, which silently resolved to zero callers
    before this fallback existed."""

    def test_resolves_a_name_import_through_the_package_directory(self, db_conn):
        _insert(db_conn, "pkg/queries.py::get_artifact", "pkg/queries.py", "get_artifact")
        _insert(db_conn, "pkg/connection.py::get_connection", "pkg/connection.py", "get_connection")

        resolved = resolve_name_imports(db_conn, [RawNameImport(module_path="pkg", name="get_artifact")])

        assert resolved == {"get_artifact": ["pkg/queries.py::get_artifact"]}

    def test_respects_alias_through_the_package_directory(self, db_conn):
        _insert(db_conn, "pkg/queries.py::get_artifact", "pkg/queries.py", "get_artifact")

        resolved = resolve_name_imports(
            db_conn, [RawNameImport(module_path="pkg", name="get_artifact", alias="ga")]
        )

        assert resolved == {"ga": ["pkg/queries.py::get_artifact"]}

    def test_direct_file_match_still_takes_priority_over_the_package_fallback(self, db_conn):
        """`pkg.queries` names a real file directly — must resolve via
        the ordinary file-stem path, never fall through to the package
        scan at all (the fallback only runs when the direct match finds
        nothing)."""
        _insert(db_conn, "pkg/queries.py::get_artifact", "pkg/queries.py", "get_artifact")

        resolved = resolve_name_imports(db_conn, [RawNameImport(module_path="pkg.queries", name="get_artifact")])

        assert resolved == {"get_artifact": ["pkg/queries.py::get_artifact"]}

    def test_name_colliding_across_sibling_files_resolves_to_nothing(self, db_conn):
        """Never guess: two direct children of the same package both
        define `helper` — ambiguous, same discipline as a stem collision
        across two unrelated files."""
        _insert(db_conn, "pkg/a.py::helper", "pkg/a.py", "helper")
        _insert(db_conn, "pkg/b.py::helper", "pkg/b.py", "helper")

        resolved = resolve_name_imports(db_conn, [RawNameImport(module_path="pkg", name="helper")])

        assert resolved == {}

    def test_does_not_reach_into_a_nested_subpackage(self, db_conn):
        """Only one level deep: a function in a sub-package two directories
        down must not be picked up as if it were a direct child."""
        _insert(db_conn, "pkg/sub/deep.py::helper", "pkg/sub/deep.py", "helper")

        resolved = resolve_name_imports(db_conn, [RawNameImport(module_path="pkg", name="helper")])

        assert resolved == {}

    def test_unmatched_package_resolves_to_nothing(self, db_conn):
        resolved = resolve_name_imports(db_conn, [RawNameImport(module_path="nonexistent_pkg", name="helper")])

        assert resolved == {}

    def test_resolves_when_the_package_is_nested_under_an_src_style_prefix(self, db_conn):
        """The exact real-world shape this bug was reported against:
        `module_path` is the Python package path (`drspec.db`, no `src`
        at all — that's a layout convention, not part of the package
        name), but real scanned file paths are rooted under `src/`
        (`src/drspec/db/queries.py`). A root-anchored-only match would
        silently miss this — regression for exactly that gap."""
        _insert(db_conn, "src/drspec/db/queries.py::get_artifact", "src/drspec/db/queries.py", "get_artifact")

        resolved = resolve_name_imports(db_conn, [RawNameImport(module_path="drspec.db", name="get_artifact")])

        assert resolved == {"get_artifact": ["src/drspec/db/queries.py::get_artifact"]}

    def test_module_import_resolves_through_the_package_directory(self, db_conn):
        """`import drspec.db as db` style — same fallback, but for the
        whole-namespace alias form (`resolve_module_imports`)."""
        _insert(db_conn, "pkg/queries.py::get_artifact", "pkg/queries.py", "get_artifact")
        _insert(db_conn, "pkg/connection.py::get_connection", "pkg/connection.py", "get_connection")

        resolved = resolve_module_imports(db_conn, [RawModuleImport(module_path="pkg", alias="db")])

        assert resolved == {
            "db": {"get_artifact": ["pkg/queries.py::get_artifact"], "get_connection": ["pkg/connection.py::get_connection"]}
        }

    def test_module_import_drops_colliding_names_but_keeps_the_rest(self, db_conn):
        _insert(db_conn, "pkg/a.py::helper", "pkg/a.py", "helper")
        _insert(db_conn, "pkg/b.py::helper", "pkg/b.py", "helper")
        _insert(db_conn, "pkg/a.py::unique_one", "pkg/a.py", "unique_one")

        resolved = resolve_module_imports(db_conn, [RawModuleImport(module_path="pkg", alias="db")])

        assert resolved == {"db": {"unique_one": ["pkg/a.py::unique_one"]}}

    def test_bind_declared_types_resolves_a_class_through_the_package_directory(self, db_conn):
        """`bind_declared_types` has the same blind spot for a type
        annotation imported via a package (e.g. `from pkg import
        Order`) — same fallback, keyed on `parent` instead of a free
        function name."""
        _insert(db_conn, "pkg/models.py::Order.process", "pkg/models.py", "process", parent="Order")
        classes = build_classes_by_name(db_conn)
        imports = ParseResult(name_imports=[RawNameImport(module_path="pkg", name="Order")])

        resolved = bind_declared_types(db_conn, "caller.py", imports, {"order": "Order"}, classes)

        assert resolved == {"order": "pkg/models.py::Order"}
