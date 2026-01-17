"""Tests for the drspec status command."""

import json
import tempfile
from pathlib import Path

from typer.testing import CliRunner

from drspec.cli.app import app


runner = CliRunner()


class TestStatusCommand:
    """Tests for status command."""

    def test_status_requires_init(self):
        """Test status fails if drspec not initialized."""
        with tempfile.TemporaryDirectory() as tmpdir:
            with runner.isolated_filesystem(temp_dir=tmpdir):
                result = runner.invoke(app, ["status"])

                assert result.exit_code == 1
                response = json.loads(result.output)
                assert response["success"] is False
                assert response["error"]["code"] == "DB_NOT_INITIALIZED"

    def test_status_empty_project(self):
        """Test status on empty initialized project."""
        with tempfile.TemporaryDirectory() as tmpdir:
            with runner.isolated_filesystem(temp_dir=tmpdir):
                # Initialize
                runner.invoke(app, ["init"])

                result = runner.invoke(app, ["status"])

                assert result.exit_code == 0
                response = json.loads(result.output)
                assert response["success"] is True

                # Check artifacts structure
                assert "artifacts" in response["data"]
                assert response["data"]["artifacts"]["total"] == 0
                assert "by_status" in response["data"]["artifacts"]

                # Check queue structure
                assert "queue" in response["data"]
                assert response["data"]["queue"]["total"] == 0
                assert "by_status" in response["data"]["queue"]

                # Check contracts structure
                assert "contracts" in response["data"]
                assert response["data"]["contracts"]["total"] == 0
                assert "confidence" in response["data"]["contracts"]

                # Check summary
                assert "summary" in response["data"]
                assert "items_needing_attention" in response["data"]["summary"]
                assert "completion_rate" in response["data"]["summary"]

    def test_status_after_scan(self):
        """Test status after scanning some files."""
        with tempfile.TemporaryDirectory() as tmpdir:
            with runner.isolated_filesystem(temp_dir=tmpdir):
                # Initialize
                runner.invoke(app, ["init"])

                # Create and scan some files
                Path("test.py").write_text('''
def hello():
    return "world"

def add(a, b):
    return a + b
''')
                runner.invoke(app, ["scan", "test.py"])

                # Get status
                result = runner.invoke(app, ["status"])

                assert result.exit_code == 0
                response = json.loads(result.output)
                assert response["success"] is True

                # Should have 2 artifacts
                assert response["data"]["artifacts"]["total"] == 2
                # New artifacts are PENDING
                assert response["data"]["artifacts"]["by_status"]["PENDING"] == 2

                # Queue should have entries (since scan queues new functions)
                assert response["data"]["queue"]["total"] == 2

                # Items needing attention should be 2 (pending)
                assert response["data"]["summary"]["items_needing_attention"] == 2

    def test_status_artifact_statuses(self):
        """Test status counts all artifact status types."""
        with tempfile.TemporaryDirectory() as tmpdir:
            with runner.isolated_filesystem(temp_dir=tmpdir):
                # Initialize
                runner.invoke(app, ["init"])

                result = runner.invoke(app, ["status"])

                response = json.loads(result.output)
                by_status = response["data"]["artifacts"]["by_status"]

                # All statuses should be present
                assert "PENDING" in by_status
                assert "VERIFIED" in by_status
                assert "NEEDS_REVIEW" in by_status
                assert "STALE" in by_status
                assert "BROKEN" in by_status

    def test_status_queue_statuses(self):
        """Test status counts all queue status types."""
        with tempfile.TemporaryDirectory() as tmpdir:
            with runner.isolated_filesystem(temp_dir=tmpdir):
                # Initialize
                runner.invoke(app, ["init"])

                result = runner.invoke(app, ["status"])

                response = json.loads(result.output)
                by_status = response["data"]["queue"]["by_status"]

                # All queue statuses should be present
                assert "PENDING" in by_status
                assert "PROCESSING" in by_status
                assert "COMPLETED" in by_status
                assert "FAILED" in by_status

    def test_status_contracts_confidence(self):
        """Test status includes confidence distribution."""
        with tempfile.TemporaryDirectory() as tmpdir:
            with runner.isolated_filesystem(temp_dir=tmpdir):
                # Initialize
                runner.invoke(app, ["init"])

                result = runner.invoke(app, ["status"])

                response = json.loads(result.output)
                confidence = response["data"]["contracts"]["confidence"]

                # Should have average and distribution
                assert "average" in confidence
                assert "distribution" in confidence

                # Distribution should have all buckets
                dist = confidence["distribution"]
                assert "below_50" in dist
                assert "50_to_70" in dist
                assert "70_to_90" in dist
                assert "above_90" in dist

    def test_status_returns_json(self):
        """Test that status command returns valid JSON."""
        with tempfile.TemporaryDirectory() as tmpdir:
            with runner.isolated_filesystem(temp_dir=tmpdir):
                # Initialize
                runner.invoke(app, ["init"])

                result = runner.invoke(app, ["status"])

                # Should not raise when parsing
                response = json.loads(result.output)
                assert "success" in response
                assert "data" in response
                assert "error" in response

    def test_status_completion_rate_zero_artifacts(self):
        """Test completion rate is 0 when no artifacts."""
        with tempfile.TemporaryDirectory() as tmpdir:
            with runner.isolated_filesystem(temp_dir=tmpdir):
                # Initialize
                runner.invoke(app, ["init"])

                result = runner.invoke(app, ["status"])

                response = json.loads(result.output)
                assert response["data"]["summary"]["completion_rate"] == 0.0
