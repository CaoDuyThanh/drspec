"""Tests for database query functions."""

import duckdb
import pytest

from drspec.db.connection import init_schema
from drspec.db.queries import (
    VALID_ARTIFACT_STATUSES,
    Artifact,
    count_artifacts,
    count_search_artifacts,
    count_unused_artifacts,
    delete_scanned_file,
    dequeue_staleness,
    enqueue_staleness,
    get_artifact,
    get_callees,
    get_callers,
    get_contract,
    get_edge_stats,
    get_reasoning_traces,
    insert_artifact,
    insert_contract,
    insert_reasoning_trace,
    list_artifacts,
    list_scanned_files,
    list_search_file_paths,
    list_staleness_queue,
    list_unused_artifacts,
    only_test_caller_function_ids,
    search_artifacts,
    search_artifacts_semantic,
    stale_file_paths_among,
    update_artifact_status,
    upsert_dependency_edge,
    upsert_reference_edge,
    upsert_scanned_file,
)
from tests.helpers import insert_dependency


@pytest.fixture(scope="module")
def _schema_database():
    """One in-memory database with the schema for the whole module. Each test rolls back what it wrote."""
    conn = duckdb.connect(":memory:")
    init_schema(conn)
    yield conn
    # every test ran in a transaction that was rolled back: the database must be as empty as it was made, or a test leaked into the others
    tables = [row[0] for row in conn.execute("SHOW TABLES").fetchall()]
    leaked = {t: n for t in tables if (n := conn.execute(f'SELECT COUNT(*) FROM "{t}"').fetchone()[0])}
    conn.close()
    assert not leaked, f"a test of this module left rows behind: {leaked}"


#: Tests that open their own explicit transaction. DuckDB cannot start one inside another, so they get a database of
#: their own, built for them alone.
_OWN_TRANSACTION = {
    "test_update_with_fk_reference_survives_an_outer_explicit_transaction",
    "test_the_function_and_every_edge_that_names_it_go",  # `cascade_delete_artifact` commits
    "test_a_function_that_is_not_there_is_reported_and_nothing_else_changes",
}


@pytest.fixture
def db_conn(_schema_database, request):
    """The module's database inside a transaction that is rolled back after the test, so no test sees another's rows
    and the schema is built once, not per test."""
    if request.node.name in _OWN_TRANSACTION:
        conn = duckdb.connect(":memory:")
        init_schema(conn)
        yield conn
        conn.close()
        return
    _schema_database.execute("BEGIN")
    try:
        yield _schema_database
    finally:
        _schema_database.execute("ROLLBACK")


class TestArtifactModel:
    """Tests for the Artifact dataclass."""

    def test_artifact_from_row(self):
        from datetime import datetime

        row = (
            "test.py::foo",  # function_id
            "test.py",  # file_path
            "foo",  # function_name
            "def foo():",  # signature
            "pass",  # body
            "abc123",  # code_hash
            "python",  # language
            10,  # start_line
            15,  # end_line
            None,  # parent
            "PENDING",  # status
            False,  # is_property
            datetime(2024, 1, 1),  # created_at
            datetime(2024, 1, 2),  # updated_at
        )

        artifact = Artifact.from_row(row)

        assert artifact.function_id == "test.py::foo"
        assert artifact.file_path == "test.py"
        assert artifact.function_name == "foo"
        assert artifact.signature == "def foo():"
        assert artifact.body == "pass"
        assert artifact.code_hash == "abc123"
        assert artifact.language == "python"
        assert artifact.start_line == 10
        assert artifact.end_line == 15
        assert artifact.parent is None
        assert artifact.status == "PENDING"
        assert artifact.is_property is False


class TestArtifactQueries:
    """Tests for artifact query functions."""

    def test_insert_and_get_artifact(self, db_conn):
        changed = insert_artifact(
            db_conn,
            function_id="src/utils.py::calculate",
            file_path="src/utils.py",
            function_name="calculate",
            signature="def calculate(x: int, y: int) -> int:",
            body="return x + y",
            code_hash="abc123",
            language="python",
            start_line=10,
            end_line=12,
        )

        assert changed is True

        artifact = get_artifact(db_conn, "src/utils.py::calculate")

        assert artifact is not None
        assert isinstance(artifact, Artifact)
        assert artifact.function_id == "src/utils.py::calculate"
        assert artifact.file_path == "src/utils.py"
        assert artifact.function_name == "calculate"
        assert artifact.signature == "def calculate(x: int, y: int) -> int:"
        assert artifact.body == "return x + y"
        assert artifact.code_hash == "abc123"
        assert artifact.language == "python"
        assert artifact.start_line == 10
        assert artifact.end_line == 12
        assert artifact.parent is None
        assert artifact.status == "PENDING"

    def test_insert_artifact_with_parent(self, db_conn):
        insert_artifact(
            db_conn,
            function_id="test.py::MyClass.method",
            file_path="test.py",
            function_name="method",
            signature="def method(self):",
            body="pass",
            code_hash="abc",
            language="python",
            start_line=5,
            end_line=6,
            parent="MyClass",
        )

        artifact = get_artifact(db_conn, "test.py::MyClass.method")
        assert artifact.parent == "MyClass"

    def test_get_artifact_not_found(self, db_conn):
        artifact = get_artifact(db_conn, "nonexistent::foo")
        assert artifact is None

    def test_insert_artifact_no_change_on_same_hash(self, db_conn):
        insert_artifact(
            db_conn,
            function_id="test.py::foo",
            file_path="test.py",
            function_name="foo",
            signature="def foo():",
            body="pass",
            code_hash="hash1",
            language="python",
            start_line=1,
            end_line=2,
        )

        changed = insert_artifact(
            db_conn,
            function_id="test.py::foo",
            file_path="test.py",
            function_name="foo",
            signature="def foo():",  # signature can change without affecting the hash-keyed dedup
            body="pass",
            code_hash="hash1",
            language="python",
            start_line=1,
            end_line=2,
        )

        assert changed is False

    def test_insert_artifact_changed_on_different_hash(self, db_conn):
        insert_artifact(
            db_conn,
            function_id="test.py::foo",
            file_path="test.py",
            function_name="foo",
            signature="def foo():",
            body="pass",
            code_hash="hash1",
            language="python",
            start_line=1,
            end_line=2,
        )

        changed = insert_artifact(
            db_conn,
            function_id="test.py::foo",
            file_path="test.py",
            function_name="foo",
            signature="def foo(x):",
            body="return x",
            code_hash="hash2",
            language="python",
            start_line=1,
            end_line=2,
        )

        assert changed is True

        artifact = get_artifact(db_conn, "test.py::foo")
        assert artifact.signature == "def foo(x):"
        assert artifact.code_hash == "hash2"

    def test_hash_change_marks_verified_as_stale(self, db_conn):
        insert_artifact(
            db_conn,
            function_id="test.py::foo",
            file_path="test.py",
            function_name="foo",
            signature="def foo():",
            body="pass",
            code_hash="hash1",
            language="python",
            start_line=1,
            end_line=2,
        )
        update_artifact_status(db_conn, "test.py::foo", "VERIFIED")

        insert_artifact(
            db_conn,
            function_id="test.py::foo",
            file_path="test.py",
            function_name="foo",
            signature="def foo(x):",
            body="return x",
            code_hash="hash2",
            language="python",
            start_line=1,
            end_line=2,
        )

        artifact = get_artifact(db_conn, "test.py::foo")
        assert artifact.status == "STALE"

    def test_hash_change_marks_needs_review_as_stale(self, db_conn):
        insert_artifact(
            db_conn,
            function_id="test.py::foo",
            file_path="test.py",
            function_name="foo",
            signature="def foo():",
            body="pass",
            code_hash="hash1",
            language="python",
            start_line=1,
            end_line=2,
        )
        update_artifact_status(db_conn, "test.py::foo", "NEEDS_REVIEW")

        insert_artifact(
            db_conn,
            function_id="test.py::foo",
            file_path="test.py",
            function_name="foo",
            signature="def foo(x):",
            body="return x",
            code_hash="hash2",
            language="python",
            start_line=1,
            end_line=2,
        )

        artifact = get_artifact(db_conn, "test.py::foo")
        assert artifact.status == "STALE"

    def test_hash_change_keeps_broken_status(self, db_conn):
        insert_artifact(
            db_conn,
            function_id="test.py::foo",
            file_path="test.py",
            function_name="foo",
            signature="def foo():",
            body="pass",
            code_hash="hash1",
            language="python",
            start_line=1,
            end_line=2,
        )
        update_artifact_status(db_conn, "test.py::foo", "BROKEN")

        insert_artifact(
            db_conn,
            function_id="test.py::foo",
            file_path="test.py",
            function_name="foo",
            signature="def foo(x):",
            body="return x",
            code_hash="hash2",
            language="python",
            start_line=1,
            end_line=2,
        )

        artifact = get_artifact(db_conn, "test.py::foo")
        assert artifact.status == "BROKEN"

    def test_hash_change_keeps_pending_status(self, db_conn):
        insert_artifact(
            db_conn,
            function_id="test.py::foo",
            file_path="test.py",
            function_name="foo",
            signature="def foo():",
            body="pass",
            code_hash="hash1",
            language="python",
            start_line=1,
            end_line=2,
        )

        insert_artifact(
            db_conn,
            function_id="test.py::foo",
            file_path="test.py",
            function_name="foo",
            signature="def foo(x):",
            body="return x",
            code_hash="hash2",
            language="python",
            start_line=1,
            end_line=2,
        )

        artifact = get_artifact(db_conn, "test.py::foo")
        assert artifact.status == "PENDING"

    def test_update_artifact_status(self, db_conn):
        insert_artifact(
            db_conn,
            function_id="test.py::foo",
            file_path="test.py",
            function_name="foo",
            signature="def foo():",
            body="pass",
            code_hash="abc",
            language="python",
            start_line=1,
            end_line=2,
        )

        result = update_artifact_status(db_conn, "test.py::foo", "VERIFIED")
        assert result is True

        artifact = get_artifact(db_conn, "test.py::foo")
        assert artifact.status == "VERIFIED"

    def test_update_artifact_status_not_found(self, db_conn):
        result = update_artifact_status(db_conn, "nonexistent::foo", "VERIFIED")
        assert result is False

    def test_update_artifact_status_invalid(self, db_conn):
        insert_artifact(
            db_conn,
            function_id="test.py::foo",
            file_path="test.py",
            function_name="foo",
            signature="def foo():",
            body="pass",
            code_hash="abc",
            language="python",
            start_line=1,
            end_line=2,
        )

        with pytest.raises(ValueError, match="Invalid status"):
            update_artifact_status(db_conn, "test.py::foo", "INVALID")

    def test_updating_embedding_survives_a_contract_reference(self, db_conn):
        """DuckDB raises a spurious FK ConstraintException when UPDATE-ing an
        array-typed column on a row any other table still references (a
        `contracts` row is enough); `insert_artifact` must detach/restore
        around this rather than propagate the error or lose the contract row."""
        insert_artifact(
            db_conn,
            function_id="test.py::foo",
            file_path="test.py",
            function_name="foo",
            signature="def foo():",
            body="pass",
            code_hash="hash1",
            language="python",
            start_line=1,
            end_line=2,
        )
        insert_contract(db_conn, function_id="test.py::foo", contract_json='{"intent": "test"}', confidence_score=0.5)

        changed = insert_artifact(
            db_conn,
            function_id="test.py::foo",
            file_path="test.py",
            function_name="foo",
            signature="def foo():",
            body="return 1",
            code_hash="hash2",
            language="python",
            start_line=1,
            end_line=2,
            embedding=[1.0] * 384,
        )

        assert changed is True
        artifact = get_artifact(db_conn, "test.py::foo")
        assert artifact.code_hash == "hash2"

        assert get_contract(db_conn, "test.py::foo") is not None

    def test_updating_embedding_survives_a_dependency_reference(self, db_conn):
        """Same regression as above, but via an incoming `dependencies` edge
        instead of a `contracts` row — the edge must round-trip intact."""
        insert_artifact(
            db_conn,
            function_id="test.py::foo",
            file_path="test.py",
            function_name="foo",
            signature="def foo():",
            body="pass",
            code_hash="hash1",
            language="python",
            start_line=1,
            end_line=2,
        )
        insert_artifact(
            db_conn,
            function_id="test.py::bar",
            file_path="test.py",
            function_name="bar",
            signature="def bar():",
            body="pass",
            code_hash="hashbar",
            language="python",
            start_line=4,
            end_line=5,
        )
        db_conn.execute(
            "INSERT INTO dependencies (caller_id, callee_id, call_site, edge_hash, embedding, confidence) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            ["test.py::bar", "test.py::foo", "foo()", "edgehash", [0.0] * 384, "EXTRACTED"],
        )

        insert_artifact(
            db_conn,
            function_id="test.py::foo",
            file_path="test.py",
            function_name="foo",
            signature="def foo():",
            body="return 1",
            code_hash="hash2",
            language="python",
            start_line=1,
            end_line=2,
            embedding=[1.0] * 384,
        )

        row = db_conn.execute(
            "SELECT call_site, confidence FROM dependencies WHERE caller_id = ? AND callee_id = ?",
            ["test.py::bar", "test.py::foo"],
        ).fetchone()
        assert row == ("foo()", "EXTRACTED")

    def test_plain_update_with_no_embedding_survives_a_contract_reference(self, db_conn):
        """The FK-constraint bug above also hits a plain status/body update
        where `embedding` stays `None`, not just embedding writes —
        `drspec scan`'s `--no-embed` default makes this the common case."""
        insert_artifact(
            db_conn,
            function_id="test.py::foo",
            file_path="test.py",
            function_name="foo",
            signature="def foo():",
            body="pass",
            code_hash="hash1",
            language="python",
            start_line=1,
            end_line=2,
        )
        insert_contract(db_conn, function_id="test.py::foo", contract_json='{"intent": "test"}', confidence_score=0.5)

        changed = insert_artifact(
            db_conn,
            function_id="test.py::foo",
            file_path="test.py",
            function_name="foo",
            signature="def foo():",
            body="return 1",
            code_hash="hash2",
            language="python",
            start_line=1,
            end_line=2,
        )

        assert changed is True
        artifact = get_artifact(db_conn, "test.py::foo")
        assert artifact.code_hash == "hash2"

        assert get_contract(db_conn, "test.py::foo") is not None

    def test_embedding_cleared_to_null_when_changed_without_embedding(self, db_conn):
        """A node that had an embedding, updated with `embedding=None`, ends
        up with `NULL` rather than holding the stale vector from before."""
        insert_artifact(
            db_conn,
            function_id="test.py::foo",
            file_path="test.py",
            function_name="foo",
            signature="def foo():",
            body="pass",
            code_hash="hash1",
            language="python",
            start_line=1,
            end_line=2,
            embedding=[1.0] * 384,
        )

        insert_artifact(
            db_conn,
            function_id="test.py::foo",
            file_path="test.py",
            function_name="foo",
            signature="def foo():",
            body="return 1",
            code_hash="hash2",
            language="python",
            start_line=1,
            end_line=2,
        )

        assert db_conn.execute("SELECT COUNT(*) FROM artifact_embeddings WHERE function_id = ?", ["test.py::foo"]).fetchone() == (0,)

    def test_update_with_fk_reference_survives_an_outer_explicit_transaction(self, db_conn):
        """Wrapping the write path in one explicit transaction broke this
        workaround — DuckDB's FK checker doesn't honor an uncommitted
        same-transaction DELETE on a referencing row when later validating
        the UPDATE on the row it references. Must keep working under an
        open explicit transaction, not just DuckDB's default auto-commit."""
        insert_artifact(
            db_conn,
            function_id="test.py::foo",
            file_path="test.py",
            function_name="foo",
            signature="def foo():",
            body="pass",
            code_hash="hash1",
            language="python",
            start_line=1,
            end_line=2,
        )
        insert_contract(db_conn, function_id="test.py::foo", contract_json='{"intent": "test"}', confidence_score=0.5)

        db_conn.begin()
        changed = insert_artifact(
            db_conn,
            function_id="test.py::foo",
            file_path="test.py",
            function_name="foo",
            signature="def foo():",
            body="return 1",
            code_hash="hash2",
            language="python",
            start_line=1,
            end_line=2,
            embedding=[1.0] * 384,
        )
        db_conn.commit()

        assert changed is True
        artifact = get_artifact(db_conn, "test.py::foo")
        assert artifact.code_hash == "hash2"
        assert get_contract(db_conn, "test.py::foo") is not None


class TestListArtifacts:
    """Tests for list_artifacts function."""

    def test_list_artifacts_empty(self, db_conn):
        artifacts = list_artifacts(db_conn)
        assert len(artifacts) == 0

    def test_list_artifacts_all(self, db_conn):
        for i in range(3):
            insert_artifact(
                db_conn,
                function_id=f"test{i}.py::foo",
                file_path=f"test{i}.py",
                function_name="foo",
                signature="def foo():",
                body="pass",
                code_hash=f"hash{i}",
                language="python",
                start_line=1,
                end_line=2,
            )

        artifacts = list_artifacts(db_conn)
        assert len(artifacts) == 3
        assert all(isinstance(a, Artifact) for a in artifacts)

    def test_list_artifacts_filter_by_status(self, db_conn):
        for i, status in enumerate(["PENDING", "PENDING", "VERIFIED"]):
            insert_artifact(
                db_conn,
                function_id=f"test{i}.py::foo",
                file_path=f"test{i}.py",
                function_name="foo",
                signature="def foo():",
                body="pass",
                code_hash=f"hash{i}",
                language="python",
                start_line=1,
                end_line=2,
            )
            if status != "PENDING":
                update_artifact_status(db_conn, f"test{i}.py::foo", status)

        pending = list_artifacts(db_conn, status="PENDING")
        assert len(pending) == 2

        verified = list_artifacts(db_conn, status="VERIFIED")
        assert len(verified) == 1

    def test_list_artifacts_filter_by_file_path(self, db_conn):
        insert_artifact(
            db_conn,
            function_id="src/utils.py::foo",
            file_path="src/utils.py",
            function_name="foo",
            signature="def foo():",
            body="pass",
            code_hash="hash1",
            language="python",
            start_line=1,
            end_line=2,
        )
        insert_artifact(
            db_conn,
            function_id="src/core.py::bar",
            file_path="src/core.py",
            function_name="bar",
            signature="def bar():",
            body="pass",
            code_hash="hash2",
            language="python",
            start_line=1,
            end_line=2,
        )
        insert_artifact(
            db_conn,
            function_id="tests/test.py::baz",
            file_path="tests/test.py",
            function_name="baz",
            signature="def baz():",
            body="pass",
            code_hash="hash3",
            language="python",
            start_line=1,
            end_line=2,
        )

        src_artifacts = list_artifacts(db_conn, file_path="src/")
        assert len(src_artifacts) == 2

        tests_artifacts = list_artifacts(db_conn, file_path="tests/")
        assert len(tests_artifacts) == 1

    def test_list_artifacts_filter_by_language(self, db_conn):
        insert_artifact(
            db_conn,
            function_id="test.py::foo",
            file_path="test.py",
            function_name="foo",
            signature="def foo():",
            body="pass",
            code_hash="hash1",
            language="python",
            start_line=1,
            end_line=2,
        )
        insert_artifact(
            db_conn,
            function_id="test.js::bar",
            file_path="test.js",
            function_name="bar",
            signature="function bar() {}",
            body="{}",
            code_hash="hash2",
            language="javascript",
            start_line=1,
            end_line=1,
        )

        python_artifacts = list_artifacts(db_conn, language="python")
        assert len(python_artifacts) == 1
        assert python_artifacts[0].function_id == "test.py::foo"

        js_artifacts = list_artifacts(db_conn, language="javascript")
        assert len(js_artifacts) == 1
        assert js_artifacts[0].function_id == "test.js::bar"

    def test_list_artifacts_pagination(self, db_conn):
        for i in range(10):
            insert_artifact(
                db_conn,
                function_id=f"test{i:02d}.py::foo",
                file_path=f"test{i:02d}.py",
                function_name="foo",
                signature="def foo():",
                body="pass",
                code_hash=f"hash{i}",
                language="python",
                start_line=1,
                end_line=2,
            )

        page1 = list_artifacts(db_conn, limit=3, offset=0)
        assert len(page1) == 3

        page2 = list_artifacts(db_conn, limit=3, offset=3)
        assert len(page2) == 3

        page1_ids = {a.function_id for a in page1}
        page2_ids = {a.function_id for a in page2}
        assert page1_ids.isdisjoint(page2_ids)


class TestCountArtifacts:
    """Tests for count_artifacts function."""

    def test_count_artifacts_empty(self, db_conn):
        count = count_artifacts(db_conn)
        assert count == 0

    def test_count_artifacts_all(self, db_conn):
        for i in range(5):
            insert_artifact(
                db_conn,
                function_id=f"test{i}.py::foo",
                file_path=f"test{i}.py",
                function_name="foo",
                signature="def foo():",
                body="pass",
                code_hash=f"hash{i}",
                language="python",
                start_line=1,
                end_line=2,
            )

        count = count_artifacts(db_conn)
        assert count == 5

    def test_count_artifacts_by_status(self, db_conn):
        for i, status in enumerate(["PENDING", "PENDING", "VERIFIED", "STALE"]):
            insert_artifact(
                db_conn,
                function_id=f"test{i}.py::foo",
                file_path=f"test{i}.py",
                function_name="foo",
                signature="def foo():",
                body="pass",
                code_hash=f"hash{i}",
                language="python",
                start_line=1,
                end_line=2,
            )
            if status != "PENDING":
                update_artifact_status(db_conn, f"test{i}.py::foo", status)

        assert count_artifacts(db_conn, status="PENDING") == 2
        assert count_artifacts(db_conn, status="VERIFIED") == 1
        assert count_artifacts(db_conn, status="STALE") == 1


class TestListUnusedArtifacts:
    """Tests for list_unused_artifacts/count_unused_artifacts."""

    def test_a_function_with_no_edges_at_all_is_unused(self, db_conn):
        insert_artifact(
            db_conn, function_id="a.py::lonely", file_path="a.py", function_name="lonely",
            signature="def lonely():", body="pass", code_hash="h1", language="python",
            start_line=1, end_line=2,
        )

        assert [a.function_id for a in list_unused_artifacts(db_conn)] == ["a.py::lonely"]
        assert count_unused_artifacts(db_conn) == 1

    def test_a_function_with_a_caller_is_excluded(self, db_conn):
        insert_artifact(
            db_conn, function_id="a.py::caller", file_path="a.py", function_name="caller",
            signature="def caller():", body="pass", code_hash="h1", language="python",
            start_line=1, end_line=2,
        )
        insert_artifact(
            db_conn, function_id="a.py::callee", file_path="a.py", function_name="callee",
            signature="def callee():", body="pass", code_hash="h2", language="python",
            start_line=4, end_line=5,
        )
        insert_dependency(db_conn, "a.py::caller", "a.py::callee")

        unused = {a.function_id for a in list_unused_artifacts(db_conn)}
        assert unused == {"a.py::caller"}

    def test_a_function_with_only_a_reference_is_excluded(self, db_conn):
        """A `code_references` edge (a property read) counts as real usage
        too, same as `_caller_relations` treating calls/references uniformly."""
        insert_artifact(
            db_conn, function_id="a.py::Order.value", file_path="a.py", function_name="value",
            signature="def value(self):", body="pass", code_hash="h1", language="python",
            start_line=1, end_line=2, parent="Order", is_property=True,
        )
        insert_artifact(
            db_conn, function_id="a.py::show", file_path="a.py", function_name="show",
            signature="def show():", body="pass", code_hash="h2", language="python",
            start_line=4, end_line=5,
        )
        upsert_reference_edge(
            db_conn, "a.py::show", "a.py::Order.value",
            reference_site="o.value", edge_hash="rh1", embedding=None, confidence="EXTRACTED",
        )

        unused = {a.function_id for a in list_unused_artifacts(db_conn)}
        assert unused == {"a.py::show"}

    def test_dunder_methods_are_excluded_even_with_zero_callers(self, db_conn):
        insert_artifact(
            db_conn, function_id="a.py::Thing.__init__", file_path="a.py", function_name="__init__",
            signature="def __init__(self):", body="pass", code_hash="h1", language="python",
            start_line=1, end_line=2, parent="Thing",
        )

        assert list_unused_artifacts(db_conn) == []
        assert count_unused_artifacts(db_conn) == 0

    def test_a_leading_underscore_private_helper_is_not_excluded(self, db_conn):
        """Only dunder (`__x__`) is special-cased — a single-underscore
        helper with zero callers is exactly the dead-code candidate this is for."""
        insert_artifact(
            db_conn, function_id="a.py::_private_helper", file_path="a.py", function_name="_private_helper",
            signature="def _private_helper():", body="pass", code_hash="h1", language="python",
            start_line=1, end_line=2,
        )

        assert [a.function_id for a in list_unused_artifacts(db_conn)] == ["a.py::_private_helper"]

    def test_path_prefix_filters_results_and_count(self, db_conn):
        insert_artifact(
            db_conn, function_id="src/a.py::foo", file_path="src/a.py", function_name="foo",
            signature="def foo():", body="pass", code_hash="h1", language="python",
            start_line=1, end_line=2,
        )
        insert_artifact(
            db_conn, function_id="tests/b.py::bar", file_path="tests/b.py", function_name="bar",
            signature="def bar():", body="pass", code_hash="h2", language="python",
            start_line=1, end_line=2,
        )

        assert [a.function_id for a in list_unused_artifacts(db_conn, path_prefix="src/")] == ["src/a.py::foo"]
        assert count_unused_artifacts(db_conn, path_prefix="src/") == 1

    def test_limit_caps_results_but_not_the_total_count(self, db_conn):
        for i in range(3):
            insert_artifact(
                db_conn, function_id=f"a.py::f{i}", file_path="a.py", function_name=f"f{i}",
                signature=f"def f{i}():", body="pass", code_hash=f"h{i}", language="python",
                start_line=i, end_line=i + 1,
            )

        assert len(list_unused_artifacts(db_conn, limit=2)) == 2
        assert count_unused_artifacts(db_conn) == 3

    def test_empty_db_resolves_to_nothing(self, db_conn):
        assert list_unused_artifacts(db_conn) == []
        assert count_unused_artifacts(db_conn) == 0

    def test_a_function_called_only_from_a_tests_dir_file_is_still_included(self, db_conn):
        """The motivating case: a function whose only caller is its own
        dedicated test is still a real dead-code candidate, not silently excluded."""
        insert_artifact(
            db_conn, function_id="a.py::normalize_code", file_path="a.py", function_name="normalize_code",
            signature="def normalize_code():", body="pass", code_hash="h1", language="python",
            start_line=1, end_line=2,
        )
        insert_artifact(
            db_conn, function_id="tests/test_a.py::test_normalize", file_path="tests/test_a.py",
            function_name="test_normalize", signature="def test_normalize():", body="pass",
            code_hash="h2", language="python", start_line=1, end_line=2,
        )
        insert_dependency(db_conn, "tests/test_a.py::test_normalize", "a.py::normalize_code")

        unused = {a.function_id for a in list_unused_artifacts(db_conn)}
        assert "a.py::normalize_code" in unused

    def test_a_function_called_only_from_a_root_level_test_file_is_still_included(self, db_conn):
        """Matches `pyproject.toml`'s `test_*.py` discovery pattern, not just
        a `tests/` dir, so a project without one still gets this signal."""
        insert_artifact(
            db_conn, function_id="a.py::helper", file_path="a.py", function_name="helper",
            signature="def helper():", body="pass", code_hash="h1", language="python",
            start_line=1, end_line=2,
        )
        insert_artifact(
            db_conn, function_id="test_a.py::test_helper", file_path="test_a.py",
            function_name="test_helper", signature="def test_helper():", body="pass",
            code_hash="h2", language="python", start_line=1, end_line=2,
        )
        insert_dependency(db_conn, "test_a.py::test_helper", "a.py::helper")

        unused = {a.function_id for a in list_unused_artifacts(db_conn)}
        assert "a.py::helper" in unused

    def test_a_function_with_one_real_caller_and_one_test_caller_is_excluded(self, db_conn):
        """Any non-test caller counts as real usage, even if another caller
        happens to be a test."""
        insert_artifact(
            db_conn, function_id="a.py::used", file_path="a.py", function_name="used",
            signature="def used():", body="pass", code_hash="h1", language="python",
            start_line=1, end_line=2,
        )
        insert_artifact(
            db_conn, function_id="a.py::real_caller", file_path="a.py", function_name="real_caller",
            signature="def real_caller():", body="pass", code_hash="h2", language="python",
            start_line=3, end_line=4,
        )
        insert_artifact(
            db_conn, function_id="tests/test_a.py::test_used", file_path="tests/test_a.py",
            function_name="test_used", signature="def test_used():", body="pass",
            code_hash="h3", language="python", start_line=1, end_line=2,
        )
        insert_dependency(db_conn, "a.py::real_caller", "a.py::used")
        insert_dependency(db_conn, "tests/test_a.py::test_used", "a.py::used")

        unused = {a.function_id for a in list_unused_artifacts(db_conn)}
        assert "a.py::used" not in unused

    def test_a_test_only_caller_via_references_also_counts(self, db_conn):
        insert_artifact(
            db_conn, function_id="a.py::Thing.prop", file_path="a.py", function_name="prop",
            signature="def prop(self):", body="pass", code_hash="h1", language="python",
            start_line=1, end_line=2, parent="Thing", is_property=True,
        )
        insert_artifact(
            db_conn, function_id="tests/test_a.py::test_prop", file_path="tests/test_a.py",
            function_name="test_prop", signature="def test_prop():", body="pass",
            code_hash="h2", language="python", start_line=1, end_line=2,
        )
        upsert_reference_edge(
            db_conn, "tests/test_a.py::test_prop", "a.py::Thing.prop",
            reference_site="t.prop", edge_hash="rh1", embedding=None, confidence="EXTRACTED",
        )

        unused = {a.function_id for a in list_unused_artifacts(db_conn)}
        assert "a.py::Thing.prop" in unused


class TestOnlyTestCallerFunctionIds:
    """Tests for only_test_caller_function_ids."""

    def test_distinguishes_zero_callers_from_test_only_callers(self, db_conn):
        insert_artifact(
            db_conn, function_id="a.py::dead", file_path="a.py", function_name="dead",
            signature="def dead():", body="pass", code_hash="h1", language="python",
            start_line=1, end_line=2,
        )
        insert_artifact(
            db_conn, function_id="a.py::normalize_code", file_path="a.py", function_name="normalize_code",
            signature="def normalize_code():", body="pass", code_hash="h2", language="python",
            start_line=3, end_line=4,
        )
        insert_artifact(
            db_conn, function_id="tests/test_a.py::test_normalize", file_path="tests/test_a.py",
            function_name="test_normalize", signature="def test_normalize():", body="pass",
            code_hash="h3", language="python", start_line=1, end_line=2,
        )
        insert_dependency(db_conn, "tests/test_a.py::test_normalize", "a.py::normalize_code")

        result = only_test_caller_function_ids(db_conn, ["a.py::dead", "a.py::normalize_code"])

        assert result == {"a.py::normalize_code"}

    def test_empty_input_is_empty_output(self, db_conn):
        assert only_test_caller_function_ids(db_conn, []) == set()


def _insert_search_fixture(conn):
    """Three functions covering substring/regex/case cases, for
    `TestSearchArtifacts` below."""
    insert_artifact(
        conn,
        function_id="utils.py::process_order",
        file_path="utils.py",
        function_name="process_order",
        signature="def process_order(order):",
        body="def process_order(order):\n    return compute_total(order)\n",
        code_hash="hash1",
        language="python",
        start_line=1,
        end_line=2,
    )
    insert_artifact(
        conn,
        function_id="utils.py::compute_total",
        file_path="utils.py",
        function_name="compute_total",
        signature="def compute_total(order):",
        body="def compute_total(order):\n    return sum(i.price for i in order.items)\n",
        code_hash="hash2",
        language="python",
        start_line=4,
        end_line=5,
    )
    insert_artifact(
        conn,
        function_id="other.js::renderWidget",
        file_path="other.js",
        function_name="renderWidget",
        signature="function renderWidget() {",
        body="function renderWidget() {\n    // TODO: polish\n}\n",
        code_hash="hash3",
        language="javascript",
        start_line=1,
        end_line=3,
    )


class TestSearchArtifacts:
    """Tests for `search_artifacts`/`count_search_artifacts`/
    `list_search_file_paths`."""

    def test_literal_substring_match(self, db_conn):
        _insert_search_fixture(db_conn)

        results = search_artifacts(db_conn, "process")

        assert {a.function_id for a in results} == {"utils.py::process_order"}

    def test_match_against_body_not_just_name(self, db_conn):
        """`order.items` only appears in `compute_total`'s body, not its name."""
        _insert_search_fixture(db_conn)

        results = search_artifacts(db_conn, "order.items")

        assert {a.function_id for a in results} == {"utils.py::compute_total"}

    def test_case_insensitive(self, db_conn):
        _insert_search_fixture(db_conn)

        assert search_artifacts(db_conn, "PROCESS") == []
        results = search_artifacts(db_conn, "PROCESS", ignore_case=True)
        assert {a.function_id for a in results} == {"utils.py::process_order"}

    def test_regex_mode(self, db_conn):
        _insert_search_fixture(db_conn)

        results = search_artifacts(db_conn, "^compute_[a-z]+$", regex=True)

        assert {a.function_id for a in results} == {"utils.py::compute_total"}

    def test_literal_percent_and_underscore_are_not_wildcards(self, db_conn):
        """A literal `%`/`_` must match literally, not act as a LIKE wildcard."""
        _insert_search_fixture(db_conn)

        assert search_artifacts(db_conn, "%") == []
        results = search_artifacts(db_conn, "process_order")
        assert {a.function_id for a in results} == {"utils.py::process_order"}

    def test_invert_match(self, db_conn):
        _insert_search_fixture(db_conn)

        results = search_artifacts(db_conn, "order")

        assert {a.function_id for a in results} == {
            "utils.py::process_order",
            "utils.py::compute_total",
        }

        inverted = search_artifacts(db_conn, "order", invert_match=True)
        assert {a.function_id for a in inverted} == {"other.js::renderWidget"}

    def test_language_filter(self, db_conn):
        _insert_search_fixture(db_conn)

        results = search_artifacts(db_conn, "", language="javascript")

        assert {a.function_id for a in results} == {"other.js::renderWidget"}

    def test_path_prefix_filter(self, db_conn):
        _insert_search_fixture(db_conn)

        results = search_artifacts(db_conn, "", path_prefix="utils")

        assert {a.function_id for a in results} == {
            "utils.py::process_order",
            "utils.py::compute_total",
        }

    def test_status_filter(self, db_conn):
        _insert_search_fixture(db_conn)
        update_artifact_status(db_conn, "utils.py::process_order", "VERIFIED")

        results = search_artifacts(db_conn, "", status="VERIFIED")

        assert {a.function_id for a in results} == {"utils.py::process_order"}

    def test_confidence_filter_matches_caller_with_that_tier_edge(self, db_conn):
        """`--confidence` means "has at least one outgoing edge at this
        tier" — a function-level filter over an edge-level property."""
        _insert_search_fixture(db_conn)

        upsert_dependency_edge(
            db_conn,
            "utils.py::process_order",
            "utils.py::compute_total",
            "compute_total(order)",
            "edgehash1",
            [0.0] * 384,
            "EXTRACTED",
        )

        results = search_artifacts(db_conn, "", confidence="EXTRACTED")

        assert {a.function_id for a in results} == {"utils.py::process_order"}

    def test_limit_caps_results(self, db_conn):
        _insert_search_fixture(db_conn)

        results = search_artifacts(db_conn, "", limit=1)

        assert len(results) == 1

    def test_count_not_capped_by_limit(self, db_conn):
        _insert_search_fixture(db_conn)

        total = count_search_artifacts(db_conn, "order")

        assert total == 2

    def test_list_search_file_paths(self, db_conn):
        _insert_search_fixture(db_conn)

        files = list_search_file_paths(db_conn, "order")

        assert files == ["utils.py"]


class TestSemanticSearch:
    """Tests for `search_artifacts_semantic`."""

    def test_ranks_by_similarity_and_respects_filters(self, db_conn):
        # Orthogonal-ish unit-length vectors so similarity is unambiguous.
        close = [1.0] + [0.0] * 383
        far = [0.0, 1.0] + [0.0] * 382

        insert_artifact(
            db_conn,
            function_id="a.py::near",
            file_path="a.py",
            function_name="near",
            signature="def near():",
            body="pass",
            code_hash="h1",
            language="python",
            start_line=1,
            end_line=2,
            embedding=close,
        )
        insert_artifact(
            db_conn,
            function_id="a.py::far",
            file_path="a.py",
            function_name="far",
            signature="def far():",
            body="pass",
            code_hash="h2",
            language="python",
            start_line=3,
            end_line=4,
            embedding=far,
        )
        insert_artifact(
            db_conn,
            function_id="a.py::no_embedding",
            file_path="a.py",
            function_name="no_embedding",
            signature="def no_embedding():",
            body="pass",
            code_hash="h3",
            language="python",
            start_line=5,
            end_line=6,
        )

        results = search_artifacts_semantic(db_conn, close)

        assert [r.artifact.function_id for r in results] == ["a.py::near", "a.py::far"]
        assert results[0].score > results[1].score


class TestStatusConstants:
    """Tests for status constant values."""

    def test_valid_artifact_statuses(self):
        assert "PENDING" in VALID_ARTIFACT_STATUSES
        assert "VERIFIED" in VALID_ARTIFACT_STATUSES
        assert "NEEDS_REVIEW" in VALID_ARTIFACT_STATUSES
        assert "STALE" in VALID_ARTIFACT_STATUSES
        assert "BROKEN" in VALID_ARTIFACT_STATUSES
        assert len(VALID_ARTIFACT_STATUSES) == 5


class TestContractQueries:
    """Tests for contract query functions."""

    def test_insert_and_get_contract(self, db_conn):
        # contract has an FK on artifact
        insert_artifact(
            db_conn,
            function_id="test.py::foo",
            file_path="test.py",
            function_name="foo",
            signature="def foo():",
            body="pass",
            code_hash="abc",
            language="python",
            start_line=1,
            end_line=2,
        )

        insert_contract(
            db_conn,
            function_id="test.py::foo",
            contract_json='{"intent": "test"}',
            confidence_score=0.85,
        )

        contract = get_contract(db_conn, "test.py::foo")
        assert contract is not None
        assert contract["contract_json"] == '{"intent": "test"}'
        assert contract["confidence_score"] == pytest.approx(0.85, rel=1e-5)

    def test_get_contract_not_found(self, db_conn):
        contract = get_contract(db_conn, "nonexistent::foo")
        assert contract is None


class TestDependencyQueries:
    """Tests for dependency query functions."""

    def test_insert_and_get_dependencies(self, db_conn):
        for name in ["foo", "bar", "baz"]:
            insert_artifact(
                db_conn,
                function_id=f"test.py::{name}",
                file_path="test.py",
                function_name=name,
                signature=f"def {name}():",
                body="pass",
                code_hash=f"hash_{name}",
                language="python",
                start_line=1,
                end_line=2,
            )

        insert_dependency(db_conn, "test.py::foo", "test.py::bar")
        insert_dependency(db_conn, "test.py::foo", "test.py::baz")

        callees = get_callees(db_conn, "test.py::foo")
        assert len(callees) == 2
        assert "test.py::bar" in callees
        assert "test.py::baz" in callees

        callers = get_callers(db_conn, "test.py::bar")
        assert len(callers) == 1
        assert callers[0] == "test.py::foo"


class TestReasoningTraceQueries:
    """Tests for reasoning trace query functions."""

    def test_insert_and_get_traces(self, db_conn):
        insert_artifact(
            db_conn,
            function_id="test.py::foo",
            file_path="test.py",
            function_name="foo",
            signature="def foo():",
            body="pass",
            code_hash="abc",
            language="python",
            start_line=1,
            end_line=2,
        )

        insert_reasoning_trace(
            db_conn,
            function_id="test.py::foo",
            agent="proposer",
            trace_json='{"decision": "high confidence"}',
        )
        insert_reasoning_trace(
            db_conn,
            function_id="test.py::foo",
            agent="critic",
            trace_json='{"objection": "none"}',
        )

        traces = get_reasoning_traces(db_conn, "test.py::foo")
        assert len(traces) == 2

        proposer_traces = get_reasoning_traces(db_conn, "test.py::foo", agent="proposer")
        assert len(proposer_traces) == 1
        assert proposer_traces[0]["agent"] == "proposer"


class TestScannedFilesAndStalenessQueue:
    """Tests for the staleness-tracking tables."""

    def test_upsert_then_list_scanned_file(self, db_conn):
        upsert_scanned_file(db_conn, "a.py", 100.0, 50)
        assert list_scanned_files(db_conn) == [("a.py", 100.0, 50)]

    def test_upsert_is_an_update_not_a_duplicate(self, db_conn):
        upsert_scanned_file(db_conn, "a.py", 100.0, 50)
        upsert_scanned_file(db_conn, "a.py", 200.0, 60)
        assert list_scanned_files(db_conn) == [("a.py", 200.0, 60)]

    def test_delete_scanned_file(self, db_conn):
        upsert_scanned_file(db_conn, "a.py", 100.0, 50)
        upsert_scanned_file(db_conn, "b.py", 200.0, 60)
        delete_scanned_file(db_conn, "a.py")
        assert list_scanned_files(db_conn) == [("b.py", 200.0, 60)]

    def test_enqueue_then_list_staleness_queue(self, db_conn):
        enqueue_staleness(db_conn, "a.py", "mtime_changed")
        assert list_staleness_queue(db_conn) == ["a.py"]

    def test_enqueue_is_idempotent_not_a_duplicate(self, db_conn):
        enqueue_staleness(db_conn, "a.py", "mtime_changed")
        enqueue_staleness(db_conn, "a.py", "size_changed")
        assert list_staleness_queue(db_conn) == ["a.py"]

    def test_dequeue_staleness(self, db_conn):
        enqueue_staleness(db_conn, "a.py", "mtime_changed")
        dequeue_staleness(db_conn, "a.py")
        assert list_staleness_queue(db_conn) == []

    def test_dequeue_a_file_not_queued_is_a_no_op(self, db_conn):
        enqueue_staleness(db_conn, "a.py", "mtime_changed")
        dequeue_staleness(db_conn, "never_queued.py")  # must not raise
        assert list_staleness_queue(db_conn) == ["a.py"]  # and the file that is queued stays

    def test_stale_file_paths_among_only_returns_queued_ones(self, db_conn):
        enqueue_staleness(db_conn, "a.py", "mtime_changed")
        assert stale_file_paths_among(db_conn, ["a.py", "b.py"]) == {"a.py"}

    def test_stale_file_paths_among_empty_input_is_empty_output(self, db_conn):
        assert stale_file_paths_among(db_conn, []) == set()

    def test_stale_file_paths_among_tolerates_a_pre_822_database(self, db_conn):
        """A read-only command never runs DDL, so a project scanned before
        this table existed must degrade to "nothing flagged," not a raw
        CatalogException."""
        db_conn.execute("DROP TABLE staleness_queue")
        assert stale_file_paths_among(db_conn, ["a.py"]) == set()


class TestGetEdgeStats:
    """Tests for get_edge_stats."""

    def _insert_pair(self, conn, caller, callee):
        insert_artifact(
            conn, function_id=caller, file_path="a.py", function_name=caller.split("::")[-1],
            signature="def f():", body="pass", code_hash=caller, language="python", start_line=1, end_line=2,
        )
        insert_artifact(
            conn, function_id=callee, file_path="a.py", function_name=callee.split("::")[-1],
            signature="def f():", body="pass", code_hash=callee, language="python", start_line=3, end_line=4,
        )

    def test_empty_database_is_all_zeros(self, db_conn):
        stats = get_edge_stats(db_conn)
        assert stats == {
            "calls": {"total": 0, "by_confidence": {"EXTRACTED": 0, "INFERRED": 0, "AMBIGUOUS": 0}},
            "references": {"total": 0, "by_confidence": {"EXTRACTED": 0, "INFERRED": 0, "AMBIGUOUS": 0}},
        }

    def test_counts_calls_by_confidence_tier(self, db_conn):
        self._insert_pair(db_conn, "a.py::x1", "a.py::y1")
        self._insert_pair(db_conn, "a.py::x2", "a.py::y2")
        upsert_dependency_edge(db_conn, "a.py::x1", "a.py::y1", "y1()", "eh1", None, "EXTRACTED")
        upsert_dependency_edge(db_conn, "a.py::x2", "a.py::y2", "y2()", "eh2", None, "INFERRED")

        stats = get_edge_stats(db_conn)

        assert stats["calls"] == {
            "total": 2,
            "by_confidence": {"EXTRACTED": 1, "INFERRED": 1, "AMBIGUOUS": 0},
        }

    def test_counts_references_separately_from_calls(self, db_conn):
        self._insert_pair(db_conn, "a.py::reader", "a.py::Thing.prop")
        upsert_reference_edge(
            db_conn, "a.py::reader", "a.py::Thing.prop",
            reference_site="t.prop", edge_hash="rh1", embedding=None, confidence="EXTRACTED",
        )

        stats = get_edge_stats(db_conn)

        assert stats["references"]["total"] == 1
        assert stats["calls"]["total"] == 0
