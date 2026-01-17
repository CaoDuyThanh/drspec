"""Tests for the status tracking system."""

import tempfile
from pathlib import Path

import pytest
import duckdb

from drspec.db.connection import init_schema
from drspec.db.queries import insert_artifact, update_artifact_status
from drspec.core.status import (
    StatusSummary,
    get_status_summary,
    get_artifacts_by_status,
    get_stale_artifacts,
    get_pending_artifacts,
    get_broken_artifacts,
    get_review_artifacts,
    mark_verified,
    mark_needs_review,
    mark_broken,
    mark_pending,
    get_file_status_summary,
    get_language_status_summary,
    bulk_update_status,
    reset_stale_to_pending,
    reset_broken_to_pending,
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


def create_artifact(db_conn, name, file_path="test.py", language="python", status="PENDING"):
    """Helper to create a test artifact."""
    insert_artifact(
        db_conn,
        function_id=f"{file_path}::{name}",
        file_path=file_path,
        function_name=name,
        signature=f"def {name}():",
        body="pass",
        code_hash=f"hash_{name}",
        language=language,
        start_line=1,
        end_line=2,
    )
    if status != "PENDING":
        update_artifact_status(db_conn, f"{file_path}::{name}", status)


class TestStatusSummary:
    """Tests for StatusSummary dataclass."""

    def test_completion_rate_empty(self):
        """Test completion rate with no artifacts."""
        summary = StatusSummary(
            total=0, pending=0, verified=0, needs_review=0, stale=0, broken=0
        )
        assert summary.completion_rate == 0.0

    def test_completion_rate_all_verified(self):
        """Test completion rate with all verified."""
        summary = StatusSummary(
            total=10, pending=0, verified=10, needs_review=0, stale=0, broken=0
        )
        assert summary.completion_rate == 1.0

    def test_completion_rate_partial(self):
        """Test completion rate with partial verification."""
        summary = StatusSummary(
            total=10, pending=3, verified=5, needs_review=2, stale=0, broken=0
        )
        assert summary.completion_rate == 0.5

    def test_success_rate(self):
        """Test success rate calculation."""
        summary = StatusSummary(
            total=10, pending=2, verified=5, needs_review=3, stale=0, broken=0
        )
        # (5 + 3) / 10 = 0.8
        assert summary.success_rate == 0.8

    def test_actionable_count(self):
        """Test actionable count calculation."""
        summary = StatusSummary(
            total=10, pending=2, verified=5, needs_review=1, stale=1, broken=1
        )
        assert summary.actionable == 5  # 2 + 1 + 1 + 1

    def test_to_dict(self):
        """Test dictionary conversion."""
        summary = StatusSummary(
            total=10, pending=3, verified=5, needs_review=1, stale=1, broken=0
        )
        d = summary.to_dict()

        assert d["total"] == 10
        assert d["pending"] == 3
        assert d["verified"] == 5
        assert d["needs_review"] == 1
        assert d["stale"] == 1
        assert d["broken"] == 0
        assert "completion_rate" in d
        assert "success_rate" in d
        assert "actionable" in d


class TestGetStatusSummary:
    """Tests for get_status_summary function."""

    def test_empty_database(self, db_conn):
        """Test status summary with no artifacts."""
        summary = get_status_summary(db_conn)

        assert summary.total == 0
        assert summary.pending == 0
        assert summary.verified == 0

    def test_with_artifacts(self, db_conn):
        """Test status summary with various statuses."""
        # Create artifacts with different statuses
        create_artifact(db_conn, "pending1", status="PENDING")
        create_artifact(db_conn, "pending2", status="PENDING")
        create_artifact(db_conn, "verified1", status="VERIFIED")
        create_artifact(db_conn, "review1", status="NEEDS_REVIEW")
        create_artifact(db_conn, "stale1", status="STALE")

        summary = get_status_summary(db_conn)

        assert summary.total == 5
        assert summary.pending == 2
        assert summary.verified == 1
        assert summary.needs_review == 1
        assert summary.stale == 1
        assert summary.broken == 0


class TestGetArtifactsByStatus:
    """Tests for get_artifacts_by_status function."""

    def test_get_pending(self, db_conn):
        """Test getting pending artifacts."""
        create_artifact(db_conn, "pending1", status="PENDING")
        create_artifact(db_conn, "verified1", status="VERIFIED")

        artifacts = get_artifacts_by_status(db_conn, "PENDING")

        assert len(artifacts) == 1
        assert artifacts[0].function_name == "pending1"

    def test_invalid_status_raises(self, db_conn):
        """Test that invalid status raises ValueError."""
        with pytest.raises(ValueError, match="Invalid status"):
            get_artifacts_by_status(db_conn, "INVALID")


class TestStatusConvenienceFunctions:
    """Tests for convenience functions."""

    def test_get_stale_artifacts(self, db_conn):
        """Test getting stale artifacts."""
        create_artifact(db_conn, "stale1", status="STALE")
        create_artifact(db_conn, "pending1", status="PENDING")

        artifacts = get_stale_artifacts(db_conn)

        assert len(artifacts) == 1
        assert artifacts[0].function_name == "stale1"

    def test_get_pending_artifacts(self, db_conn):
        """Test getting pending artifacts."""
        create_artifact(db_conn, "pending1", status="PENDING")
        create_artifact(db_conn, "verified1", status="VERIFIED")

        artifacts = get_pending_artifacts(db_conn)

        assert len(artifacts) == 1
        assert artifacts[0].function_name == "pending1"

    def test_get_broken_artifacts(self, db_conn):
        """Test getting broken artifacts."""
        create_artifact(db_conn, "broken1", status="BROKEN")
        create_artifact(db_conn, "pending1", status="PENDING")

        artifacts = get_broken_artifacts(db_conn)

        assert len(artifacts) == 1
        assert artifacts[0].function_name == "broken1"

    def test_get_review_artifacts(self, db_conn):
        """Test getting artifacts needing review."""
        create_artifact(db_conn, "review1", status="NEEDS_REVIEW")
        create_artifact(db_conn, "pending1", status="PENDING")

        artifacts = get_review_artifacts(db_conn)

        assert len(artifacts) == 1
        assert artifacts[0].function_name == "review1"


class TestMarkFunctions:
    """Tests for mark_* status functions."""

    def test_mark_verified(self, db_conn):
        """Test marking artifact as verified."""
        create_artifact(db_conn, "foo")

        result = mark_verified(db_conn, "test.py::foo")

        assert result is True
        artifacts = get_artifacts_by_status(db_conn, "VERIFIED")
        assert len(artifacts) == 1

    def test_mark_needs_review(self, db_conn):
        """Test marking artifact as needs review."""
        create_artifact(db_conn, "foo")

        result = mark_needs_review(db_conn, "test.py::foo")

        assert result is True
        artifacts = get_artifacts_by_status(db_conn, "NEEDS_REVIEW")
        assert len(artifacts) == 1

    def test_mark_broken(self, db_conn):
        """Test marking artifact as broken."""
        create_artifact(db_conn, "foo")

        result = mark_broken(db_conn, "test.py::foo")

        assert result is True
        artifacts = get_artifacts_by_status(db_conn, "BROKEN")
        assert len(artifacts) == 1

    def test_mark_pending(self, db_conn):
        """Test resetting artifact to pending."""
        create_artifact(db_conn, "foo", status="VERIFIED")

        result = mark_pending(db_conn, "test.py::foo")

        assert result is True
        artifacts = get_artifacts_by_status(db_conn, "PENDING")
        assert len(artifacts) == 1

    def test_mark_nonexistent(self, db_conn):
        """Test marking non-existent artifact returns False."""
        result = mark_verified(db_conn, "nonexistent::foo")
        assert result is False


class TestFileSummary:
    """Tests for get_file_status_summary."""

    def test_file_summary(self, db_conn):
        """Test status summary for a specific file."""
        create_artifact(db_conn, "foo", file_path="src/utils.py", status="VERIFIED")
        create_artifact(db_conn, "bar", file_path="src/utils.py", status="PENDING")
        create_artifact(db_conn, "baz", file_path="tests/test.py", status="VERIFIED")

        summary = get_file_status_summary(db_conn, "src/utils.py")

        assert summary.total == 2
        assert summary.verified == 1
        assert summary.pending == 1


class TestLanguageSummary:
    """Tests for get_language_status_summary."""

    def test_language_summary(self, db_conn):
        """Test status summary for a specific language."""
        create_artifact(db_conn, "foo", language="python", status="VERIFIED")
        create_artifact(db_conn, "bar", language="python", status="PENDING")
        create_artifact(db_conn, "baz", file_path="test.js", language="javascript", status="VERIFIED")

        summary = get_language_status_summary(db_conn, "python")

        assert summary.total == 2
        assert summary.verified == 1
        assert summary.pending == 1


class TestBulkUpdate:
    """Tests for bulk_update_status."""

    def test_bulk_update(self, db_conn):
        """Test bulk status update."""
        create_artifact(db_conn, "foo")
        create_artifact(db_conn, "bar")
        create_artifact(db_conn, "baz")

        updated = bulk_update_status(
            db_conn,
            ["test.py::foo", "test.py::bar"],
            "VERIFIED",
        )

        assert updated == 2
        assert len(get_artifacts_by_status(db_conn, "VERIFIED")) == 2
        assert len(get_artifacts_by_status(db_conn, "PENDING")) == 1

    def test_bulk_update_invalid_status(self, db_conn):
        """Test bulk update with invalid status raises ValueError."""
        with pytest.raises(ValueError, match="Invalid status"):
            bulk_update_status(db_conn, ["test.py::foo"], "INVALID")


class TestResetFunctions:
    """Tests for reset_* functions."""

    def test_reset_stale_to_pending(self, db_conn):
        """Test resetting stale artifacts to pending."""
        create_artifact(db_conn, "stale1", status="STALE")
        create_artifact(db_conn, "stale2", status="STALE")
        create_artifact(db_conn, "verified1", status="VERIFIED")

        reset_count = reset_stale_to_pending(db_conn)

        assert reset_count == 2
        assert len(get_artifacts_by_status(db_conn, "STALE")) == 0
        assert len(get_artifacts_by_status(db_conn, "PENDING")) == 2

    def test_reset_broken_to_pending(self, db_conn):
        """Test resetting broken artifacts to pending."""
        create_artifact(db_conn, "broken1", status="BROKEN")
        create_artifact(db_conn, "broken2", status="BROKEN")
        create_artifact(db_conn, "verified1", status="VERIFIED")

        reset_count = reset_broken_to_pending(db_conn)

        assert reset_count == 2
        assert len(get_artifacts_by_status(db_conn, "BROKEN")) == 0
        assert len(get_artifacts_by_status(db_conn, "PENDING")) == 2
