"""Tests for database query functions."""

import tempfile
from pathlib import Path

import pytest
import duckdb

from drspec.db.connection import init_schema
from drspec.db.queries import (
    Artifact,
    QueueItem,
    VALID_ARTIFACT_STATUSES,
    VALID_QUEUE_STATUSES,
    VALID_QUEUE_REASONS,
    insert_artifact,
    get_artifact,
    list_artifacts,
    count_artifacts,
    update_artifact_status,
    insert_contract,
    get_contract,
    list_contracts,
    queue_push,
    queue_pop,
    queue_peek,
    queue_get,
    queue_complete,
    queue_retry,
    queue_prioritize,
    queue_remove,
    queue_count,
    queue_clear_completed,
    insert_dependency,
    get_callers,
    get_callees,
    insert_reasoning_trace,
    get_reasoning_traces,
)


@pytest.fixture
def db_conn():
    """Create a test database connection with schema."""
    with tempfile.TemporaryDirectory() as tmpdir:
        db_path = Path(tmpdir) / "test.db"
        conn = duckdb.connect(str(db_path))
        init_schema(conn)
        yield conn
        conn.close()


class TestArtifactModel:
    """Tests for the Artifact dataclass."""

    def test_artifact_from_row(self):
        """Test creating Artifact from a database row tuple."""
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


class TestArtifactQueries:
    """Tests for artifact query functions."""

    def test_insert_and_get_artifact(self, db_conn):
        """Test inserting and retrieving an artifact."""
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
        """Test inserting artifact with parent class."""
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
        """Test getting non-existent artifact returns None."""
        artifact = get_artifact(db_conn, "nonexistent::foo")
        assert artifact is None

    def test_insert_artifact_no_change_on_same_hash(self, db_conn):
        """Test that inserting with same hash returns False (no change)."""
        # Insert initial
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

        # Insert again with same hash
        changed = insert_artifact(
            db_conn,
            function_id="test.py::foo",
            file_path="test.py",
            function_name="foo",
            signature="def foo():",  # Signature can change
            body="pass",
            code_hash="hash1",  # Same hash
            language="python",
            start_line=1,
            end_line=2,
        )

        assert changed is False

    def test_insert_artifact_changed_on_different_hash(self, db_conn):
        """Test that inserting with different hash returns True (changed)."""
        # Insert initial
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

        # Insert with different hash
        changed = insert_artifact(
            db_conn,
            function_id="test.py::foo",
            file_path="test.py",
            function_name="foo",
            signature="def foo(x):",
            body="return x",
            code_hash="hash2",  # Different hash
            language="python",
            start_line=1,
            end_line=2,
        )

        assert changed is True

        artifact = get_artifact(db_conn, "test.py::foo")
        assert artifact.signature == "def foo(x):"
        assert artifact.code_hash == "hash2"

    def test_hash_change_marks_verified_as_stale(self, db_conn):
        """Test that VERIFIED artifact becomes STALE when hash changes."""
        # Insert and verify
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

        # Update with different hash
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
        """Test that NEEDS_REVIEW artifact becomes STALE when hash changes."""
        # Insert and mark as needs_review
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

        # Update with different hash
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
        """Test that BROKEN artifact stays BROKEN when hash changes."""
        # Insert and mark as broken
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

        # Update with different hash
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
        """Test that PENDING artifact stays PENDING when hash changes."""
        # Insert as pending (default)
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

        # Update with different hash
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
        """Test updating artifact status."""
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
        """Test updating status of non-existent artifact returns False."""
        result = update_artifact_status(db_conn, "nonexistent::foo", "VERIFIED")
        assert result is False

    def test_update_artifact_status_invalid(self, db_conn):
        """Test updating with invalid status raises ValueError."""
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


class TestListArtifacts:
    """Tests for list_artifacts function."""

    def test_list_artifacts_empty(self, db_conn):
        """Test listing artifacts when none exist."""
        artifacts = list_artifacts(db_conn)
        assert len(artifacts) == 0

    def test_list_artifacts_all(self, db_conn):
        """Test listing all artifacts."""
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
        """Test filtering artifacts by status."""
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
        """Test filtering artifacts by file path prefix."""
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
        """Test filtering artifacts by language."""
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
        """Test pagination with limit and offset."""
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

        # Ensure no overlap
        page1_ids = {a.function_id for a in page1}
        page2_ids = {a.function_id for a in page2}
        assert page1_ids.isdisjoint(page2_ids)


class TestCountArtifacts:
    """Tests for count_artifacts function."""

    def test_count_artifacts_empty(self, db_conn):
        """Test counting artifacts when none exist."""
        count = count_artifacts(db_conn)
        assert count == 0

    def test_count_artifacts_all(self, db_conn):
        """Test counting all artifacts."""
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
        """Test counting artifacts by status."""
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


class TestStatusConstants:
    """Tests for status constant values."""

    def test_valid_artifact_statuses(self):
        """Test that valid artifact statuses are defined."""
        assert "PENDING" in VALID_ARTIFACT_STATUSES
        assert "VERIFIED" in VALID_ARTIFACT_STATUSES
        assert "NEEDS_REVIEW" in VALID_ARTIFACT_STATUSES
        assert "STALE" in VALID_ARTIFACT_STATUSES
        assert "BROKEN" in VALID_ARTIFACT_STATUSES
        assert len(VALID_ARTIFACT_STATUSES) == 5

    def test_valid_queue_statuses(self):
        """Test that valid queue statuses are defined."""
        assert "PENDING" in VALID_QUEUE_STATUSES
        assert "PROCESSING" in VALID_QUEUE_STATUSES
        assert "COMPLETED" in VALID_QUEUE_STATUSES
        assert "FAILED" in VALID_QUEUE_STATUSES
        assert len(VALID_QUEUE_STATUSES) == 4


class TestContractQueries:
    """Tests for contract query functions."""

    def test_insert_and_get_contract(self, db_conn):
        """Test inserting and retrieving a contract."""
        # Must have artifact first (foreign key)
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
        # Use pytest.approx for floating point comparison
        assert contract["confidence_score"] == pytest.approx(0.85, rel=1e-5)

    def test_get_contract_not_found(self, db_conn):
        """Test getting non-existent contract returns None."""
        contract = get_contract(db_conn, "nonexistent::foo")
        assert contract is None

    def test_list_contracts(self, db_conn):
        """Test listing contracts."""
        # Create artifacts and contracts
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
            if i < 2:
                update_artifact_status(db_conn, f"test{i}.py::foo", "VERIFIED")
            insert_contract(
                db_conn,
                function_id=f"test{i}.py::foo",
                contract_json=f'{{"id": {i}}}',
                confidence_score=0.5 + i * 0.1,
            )

        contracts = list_contracts(db_conn)
        assert len(contracts) == 3

    def test_list_contracts_with_status_filter(self, db_conn):
        """Test listing contracts with status filter."""
        # Create artifacts with different statuses
        for i, status in enumerate(["VERIFIED", "VERIFIED", "PENDING"]):
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
            insert_contract(
                db_conn,
                function_id=f"test{i}.py::foo",
                contract_json=f'{{"id": {i}}}',
                confidence_score=0.8,
            )

        verified = list_contracts(db_conn, status="VERIFIED")
        assert len(verified) == 2


class TestQueueItemModel:
    """Tests for the QueueItem dataclass."""

    def test_queue_item_from_row(self):
        """Test creating QueueItem from a database row tuple."""
        from datetime import datetime

        row = (
            "test.py::foo",  # function_id
            50,  # priority
            "PENDING",  # status
            "NEW",  # reason
            0,  # attempts
            3,  # max_attempts
            None,  # error_message
            datetime(2024, 1, 1),  # created_at
            datetime(2024, 1, 2),  # updated_at
        )

        item = QueueItem.from_row(row)

        assert item.function_id == "test.py::foo"
        assert item.priority == 50
        assert item.status == "PENDING"
        assert item.reason == "NEW"
        assert item.attempts == 0
        assert item.max_attempts == 3
        assert item.error_message is None


class TestQueueQueries:
    """Tests for queue query functions."""

    def test_queue_push_and_pop(self, db_conn):
        """Test pushing and popping from queue."""
        # Must have artifact first
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

        queue_push(db_conn, "test.py::foo", priority=50)

        item = queue_pop(db_conn)
        assert item is not None
        assert isinstance(item, QueueItem)
        assert item.function_id == "test.py::foo"
        assert item.status == "PROCESSING"
        assert item.reason == "NEW"
        assert item.attempts == 1

    def test_queue_push_with_reason(self, db_conn):
        """Test pushing with different reasons."""
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

        queue_push(db_conn, "test.py::foo", reason="HASH_MISMATCH")

        item = queue_get(db_conn, "test.py::foo")
        assert item.reason == "HASH_MISMATCH"

    def test_queue_push_invalid_reason(self, db_conn):
        """Test pushing with invalid reason raises ValueError."""
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

        with pytest.raises(ValueError, match="Invalid reason"):
            queue_push(db_conn, "test.py::foo", reason="INVALID")

    def test_queue_pop_empty(self, db_conn):
        """Test popping from empty queue returns None."""
        item = queue_pop(db_conn)
        assert item is None

    def test_queue_pop_increments_attempts(self, db_conn):
        """Test popping increments the attempt counter."""
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

        queue_push(db_conn, "test.py::foo")

        # Pop once
        item = queue_pop(db_conn)
        assert item.attempts == 1

        # Reset and pop again
        queue_retry(db_conn, "test.py::foo")
        item = queue_pop(db_conn)
        assert item.attempts == 2

    def test_queue_pop_respects_max_attempts(self, db_conn):
        """Test pop doesn't return items that exceeded max attempts."""
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

        queue_push(db_conn, "test.py::foo")

        # Pop 3 times (max_attempts default is 3)
        for _ in range(3):
            item = queue_pop(db_conn)
            if item:
                queue_retry(db_conn, "test.py::foo")

        # After 3 attempts, should not be returned
        queue_retry(db_conn, "test.py::foo")  # Reset status but keep attempts
        # Actually attempts don't reset on retry - let's verify with queue_get
        item = queue_get(db_conn, "test.py::foo")
        # With 3 attempts done, pop should return None
        # Need to manually set back to PENDING
        db_conn.execute(
            "UPDATE queue SET status = 'PENDING' WHERE function_id = ?",
            ["test.py::foo"],
        )
        item = queue_pop(db_conn)
        assert item is None  # Max attempts reached

    def test_queue_peek(self, db_conn):
        """Test peeking at queue items."""
        # Create artifacts and queue items
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
            queue_push(db_conn, f"test{i}.py::foo", priority=100 - i)

        items = queue_peek(db_conn, count=3)
        assert len(items) == 3
        assert all(isinstance(item, QueueItem) for item in items)
        # All should still be PENDING (not consumed)
        assert all(item.status == "PENDING" for item in items)

    def test_queue_peek_include_all(self, db_conn):
        """Test peeking with include_all flag."""
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

        queue_push(db_conn, "test.py::foo")
        queue_pop(db_conn)  # Sets to PROCESSING

        # Without include_all, should not see processing item
        items = queue_peek(db_conn)
        assert len(items) == 0

        # With include_all, should see it
        items = queue_peek(db_conn, include_all=True)
        assert len(items) == 1
        assert items[0].status == "PROCESSING"

    def test_queue_priority_ordering(self, db_conn):
        """Test queue items are ordered by priority."""
        # Create artifacts
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

        # Add with different priorities (lower = higher priority)
        queue_push(db_conn, "test0.py::foo", priority=100)
        queue_push(db_conn, "test1.py::foo", priority=1)  # Highest priority
        queue_push(db_conn, "test2.py::foo", priority=50)

        item = queue_pop(db_conn)
        assert item.function_id == "test1.py::foo"

    def test_queue_complete_success(self, db_conn):
        """Test marking queue item as completed."""
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
        queue_push(db_conn, "test.py::foo")
        queue_pop(db_conn)

        result = queue_complete(db_conn, "test.py::foo", success=True)
        assert result is True

        item = queue_get(db_conn, "test.py::foo")
        assert item.status == "COMPLETED"

    def test_queue_complete_failure(self, db_conn):
        """Test marking queue item as failed with error message."""
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
        queue_push(db_conn, "test.py::foo")
        queue_pop(db_conn)

        result = queue_complete(
            db_conn, "test.py::foo", success=False, error_message="Contract generation failed"
        )
        assert result is True

        item = queue_get(db_conn, "test.py::foo")
        assert item.status == "FAILED"
        assert item.error_message == "Contract generation failed"

    def test_queue_retry(self, db_conn):
        """Test retrying a failed queue item."""
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
        queue_push(db_conn, "test.py::foo")
        queue_pop(db_conn)
        queue_complete(db_conn, "test.py::foo", success=False, error_message="Error")

        result = queue_retry(db_conn, "test.py::foo", reason="MANUAL_RETRY")
        assert result is True

        item = queue_get(db_conn, "test.py::foo")
        assert item.status == "PENDING"
        assert item.reason == "MANUAL_RETRY"
        assert item.error_message is None

    def test_queue_retry_invalid_reason(self, db_conn):
        """Test retrying with invalid reason raises ValueError."""
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
        queue_push(db_conn, "test.py::foo")

        with pytest.raises(ValueError, match="Invalid reason"):
            queue_retry(db_conn, "test.py::foo", reason="INVALID")

    def test_queue_prioritize(self, db_conn):
        """Test updating queue item priority."""
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
        queue_push(db_conn, "test.py::foo", priority=100)

        result = queue_prioritize(db_conn, "test.py::foo", priority=1)
        assert result is True

        item = queue_get(db_conn, "test.py::foo")
        assert item.priority == 1

    def test_queue_remove(self, db_conn):
        """Test removing item from queue."""
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
        queue_push(db_conn, "test.py::foo")

        result = queue_remove(db_conn, "test.py::foo")
        assert result is True

        item = queue_get(db_conn, "test.py::foo")
        assert item is None

    def test_queue_remove_not_found(self, db_conn):
        """Test removing non-existent item returns False."""
        result = queue_remove(db_conn, "nonexistent::foo")
        assert result is False

    def test_queue_count(self, db_conn):
        """Test counting queue items."""
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
            queue_push(db_conn, f"test{i}.py::foo")

        assert queue_count(db_conn) == 5
        assert queue_count(db_conn, status="PENDING") == 5

        # Pop one
        queue_pop(db_conn)
        assert queue_count(db_conn, status="PENDING") == 4
        assert queue_count(db_conn, status="PROCESSING") == 1

    def test_queue_clear_completed(self, db_conn):
        """Test clearing completed items."""
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
            queue_push(db_conn, f"test{i}.py::foo")
            if i < 2:
                queue_pop(db_conn)
                queue_complete(db_conn, f"test{i}.py::foo", success=True)

        assert queue_count(db_conn) == 3
        assert queue_count(db_conn, status="COMPLETED") == 2

        removed = queue_clear_completed(db_conn)
        assert removed == 2
        assert queue_count(db_conn) == 1


class TestQueueReasonConstants:
    """Tests for queue reason constants."""

    def test_valid_queue_reasons(self):
        """Test that valid queue reasons are defined."""
        assert "NEW" in VALID_QUEUE_REASONS
        assert "HASH_MISMATCH" in VALID_QUEUE_REASONS
        assert "DEPENDENCY_CHANGED" in VALID_QUEUE_REASONS
        assert "MANUAL_RETRY" in VALID_QUEUE_REASONS
        assert len(VALID_QUEUE_REASONS) == 4


class TestDependencyQueries:
    """Tests for dependency query functions."""

    def test_insert_and_get_dependencies(self, db_conn):
        """Test inserting and retrieving dependencies."""
        # Create artifacts
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

        # foo calls bar and baz
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
        """Test inserting and retrieving reasoning traces."""
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
