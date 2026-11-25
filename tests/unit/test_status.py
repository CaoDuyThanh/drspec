
import duckdb
import pytest

from drspec.core.status import (
    StatusSummary,
    bulk_update_status,
    get_artifacts_by_status,
    get_broken_artifacts,
    get_file_status_summary,
    get_language_status_summary,
    get_pending_artifacts,
    get_review_artifacts,
    get_stale_artifacts,
    get_status_summary,
    mark_broken,
    mark_needs_review,
    mark_pending,
    mark_verified,
    reset_broken_to_pending,
    reset_stale_to_pending,
)
from drspec.db.connection import init_schema
from drspec.db.queries import insert_artifact, update_artifact_status


@pytest.fixture
def db_conn():
    conn = duckdb.connect(":memory:")  # (what is checked is what SQL says about the rows, not a file)
    init_schema(conn)
    yield conn
    conn.close()


def create_artifact(db_conn, name, file_path="test.py", language="python", status="PENDING"):
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
    def test_completion_rate_empty(self):
        summary = StatusSummary(
            total=0, pending=0, verified=0, needs_review=0, stale=0, broken=0
        )
        assert summary.completion_rate == 0.0

    def test_completion_rate_all_verified(self):
        summary = StatusSummary(
            total=10, pending=0, verified=10, needs_review=0, stale=0, broken=0
        )
        assert summary.completion_rate == 1.0

    def test_completion_rate_partial(self):
        summary = StatusSummary(
            total=10, pending=3, verified=5, needs_review=2, stale=0, broken=0
        )
        assert summary.completion_rate == 0.5

    def test_success_rate(self):
        summary = StatusSummary(
            total=10, pending=2, verified=5, needs_review=3, stale=0, broken=0
        )
        # (5 + 3) / 10 = 0.8
        assert summary.success_rate == 0.8

    def test_actionable_count(self):
        summary = StatusSummary(
            total=10, pending=2, verified=5, needs_review=1, stale=1, broken=1
        )
        assert summary.actionable == 5  # 2 + 1 + 1 + 1

    def test_to_dict(self):
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
    def test_empty_database(self, db_conn):
        summary = get_status_summary(db_conn)

        assert summary.total == 0
        assert summary.pending == 0
        assert summary.verified == 0

    def test_with_artifacts(self, db_conn):
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
    def test_get_pending(self, db_conn):
        create_artifact(db_conn, "pending1", status="PENDING")
        create_artifact(db_conn, "verified1", status="VERIFIED")

        artifacts = get_artifacts_by_status(db_conn, "PENDING")

        assert len(artifacts) == 1
        assert artifacts[0].function_name == "pending1"

    def test_invalid_status_raises(self, db_conn):
        with pytest.raises(ValueError, match="Invalid status"):
            get_artifacts_by_status(db_conn, "INVALID")


class TestStatusConvenienceFunctions:
    def test_get_stale_artifacts(self, db_conn):
        create_artifact(db_conn, "stale1", status="STALE")
        create_artifact(db_conn, "pending1", status="PENDING")

        artifacts = get_stale_artifacts(db_conn)

        assert len(artifacts) == 1
        assert artifacts[0].function_name == "stale1"

    def test_get_pending_artifacts(self, db_conn):
        create_artifact(db_conn, "pending1", status="PENDING")
        create_artifact(db_conn, "verified1", status="VERIFIED")

        artifacts = get_pending_artifacts(db_conn)

        assert len(artifacts) == 1
        assert artifacts[0].function_name == "pending1"

    def test_get_broken_artifacts(self, db_conn):
        create_artifact(db_conn, "broken1", status="BROKEN")
        create_artifact(db_conn, "pending1", status="PENDING")

        artifacts = get_broken_artifacts(db_conn)

        assert len(artifacts) == 1
        assert artifacts[0].function_name == "broken1"

    def test_get_review_artifacts(self, db_conn):
        create_artifact(db_conn, "review1", status="NEEDS_REVIEW")
        create_artifact(db_conn, "pending1", status="PENDING")

        artifacts = get_review_artifacts(db_conn)

        assert len(artifacts) == 1
        assert artifacts[0].function_name == "review1"


class TestMarkFunctions:
    def test_mark_verified(self, db_conn):
        create_artifact(db_conn, "foo")

        result = mark_verified(db_conn, "test.py::foo")

        assert result is True
        artifacts = get_artifacts_by_status(db_conn, "VERIFIED")
        assert len(artifacts) == 1

    def test_mark_needs_review(self, db_conn):
        create_artifact(db_conn, "foo")

        result = mark_needs_review(db_conn, "test.py::foo")

        assert result is True
        artifacts = get_artifacts_by_status(db_conn, "NEEDS_REVIEW")
        assert len(artifacts) == 1

    def test_mark_broken(self, db_conn):
        create_artifact(db_conn, "foo")

        result = mark_broken(db_conn, "test.py::foo")

        assert result is True
        artifacts = get_artifacts_by_status(db_conn, "BROKEN")
        assert len(artifacts) == 1

    def test_mark_pending(self, db_conn):
        create_artifact(db_conn, "foo", status="VERIFIED")

        result = mark_pending(db_conn, "test.py::foo")

        assert result is True
        artifacts = get_artifacts_by_status(db_conn, "PENDING")
        assert len(artifacts) == 1

    def test_mark_nonexistent(self, db_conn):
        result = mark_verified(db_conn, "nonexistent::foo")
        assert result is False


class TestFileSummary:
    def test_file_summary(self, db_conn):
        create_artifact(db_conn, "foo", file_path="src/utils.py", status="VERIFIED")
        create_artifact(db_conn, "bar", file_path="src/utils.py", status="PENDING")
        create_artifact(db_conn, "baz", file_path="tests/test.py", status="VERIFIED")

        summary = get_file_status_summary(db_conn, "src/utils.py")

        assert summary.total == 2
        assert summary.verified == 1
        assert summary.pending == 1


class TestLanguageSummary:
    def test_language_summary(self, db_conn):
        create_artifact(db_conn, "foo", language="python", status="VERIFIED")
        create_artifact(db_conn, "bar", language="python", status="PENDING")
        create_artifact(db_conn, "baz", file_path="test.js", language="javascript", status="VERIFIED")

        summary = get_language_status_summary(db_conn, "python")

        assert summary.total == 2
        assert summary.verified == 1
        assert summary.pending == 1


class TestBulkUpdate:
    def test_bulk_update(self, db_conn):
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
        with pytest.raises(ValueError, match="Invalid status"):
            bulk_update_status(db_conn, ["test.py::foo"], "INVALID")


class TestResetFunctions:
    def test_reset_stale_to_pending(self, db_conn):
        create_artifact(db_conn, "stale1", status="STALE")
        create_artifact(db_conn, "stale2", status="STALE")
        create_artifact(db_conn, "verified1", status="VERIFIED")

        reset_count = reset_stale_to_pending(db_conn)

        assert reset_count == 2
        assert len(get_artifacts_by_status(db_conn, "STALE")) == 0
        assert len(get_artifacts_by_status(db_conn, "PENDING")) == 2

    def test_reset_broken_to_pending(self, db_conn):
        create_artifact(db_conn, "broken1", status="BROKEN")
        create_artifact(db_conn, "broken2", status="BROKEN")
        create_artifact(db_conn, "verified1", status="VERIFIED")

        reset_count = reset_broken_to_pending(db_conn)

        assert reset_count == 2
        assert len(get_artifacts_by_status(db_conn, "BROKEN")) == 0
        assert len(get_artifacts_by_status(db_conn, "PENDING")) == 2
