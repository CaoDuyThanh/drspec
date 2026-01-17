"""Tests for the CLI output module."""

import json
from datetime import datetime
from enum import Enum
from pathlib import Path

import pytest

from drspec.cli.output import (
    ErrorCode,
    DrSpecEncoder,
    success_response,
    error_response,
    output_json,
    output_pretty,
    output,
)


class TestErrorCode:
    """Tests for the ErrorCode enum."""

    def test_error_codes_are_screaming_snake_case(self):
        """All error codes should use SCREAMING_SNAKE_CASE."""
        for code in ErrorCode:
            assert code.value == code.value.upper()
            assert "_" in code.value or code.value.isalpha()
            # Check it's snake case (no consecutive underscores, no leading/trailing)
            assert "__" not in code.value
            assert not code.value.startswith("_")
            assert not code.value.endswith("_")

    def test_common_error_codes_exist(self):
        """Verify all documented error codes exist."""
        assert ErrorCode.DB_NOT_INITIALIZED
        assert ErrorCode.CONTRACT_NOT_FOUND
        assert ErrorCode.INVALID_SCHEMA
        assert ErrorCode.QUEUE_EMPTY
        assert ErrorCode.FUNCTION_NOT_FOUND
        assert ErrorCode.PARSE_ERROR
        assert ErrorCode.VERIFICATION_FAILED


class TestSuccessResponse:
    """Tests for success_response function."""

    def test_success_response_structure(self):
        """Test success response has correct structure."""
        response = success_response({"key": "value"})

        assert response["success"] is True
        assert response["data"] == {"key": "value"}
        assert response["error"] is None

    def test_success_response_with_complex_data(self):
        """Test success response with nested data."""
        data = {
            "function_id": "src/utils.py::calculate",
            "contracts": [
                {"intent": "Add numbers"},
                {"intent": "Validate input"},
            ],
            "metadata": {
                "count": 42,
                "verified": True,
            },
        }
        response = success_response(data)

        assert response["success"] is True
        assert response["data"]["function_id"] == "src/utils.py::calculate"
        assert len(response["data"]["contracts"]) == 2

    def test_success_response_with_empty_data(self):
        """Test success response with empty data."""
        response = success_response({})

        assert response["success"] is True
        assert response["data"] == {}


class TestErrorResponse:
    """Tests for error_response function."""

    def test_error_response_structure(self):
        """Test error response has correct structure."""
        response = error_response("TEST_ERROR", "Something went wrong")

        assert response["success"] is False
        assert response["data"] is None
        assert response["error"]["code"] == "TEST_ERROR"
        assert response["error"]["message"] == "Something went wrong"
        assert response["error"]["details"] == {}

    def test_error_response_with_details(self):
        """Test error response with details."""
        details = {"path": "/some/path", "line": 42}
        response = error_response("PARSE_ERROR", "Failed to parse", details)

        assert response["error"]["details"] == details

    def test_error_response_with_error_code_enum(self):
        """Test error response accepts ErrorCode enum."""
        response = error_response(
            ErrorCode.FUNCTION_NOT_FOUND,
            "No function with ID test.py::foo"
        )

        assert response["error"]["code"] == "FUNCTION_NOT_FOUND"


class TestDrSpecEncoder:
    """Tests for custom JSON encoder."""

    def test_encodes_datetime(self):
        """Test datetime serialization to ISO format."""
        dt = datetime(2024, 1, 15, 10, 30, 0)
        result = json.dumps({"created_at": dt}, cls=DrSpecEncoder)
        data = json.loads(result)

        assert data["created_at"] == "2024-01-15T10:30:00"

    def test_encodes_enum(self):
        """Test enum serialization to value."""

        class Status(Enum):
            PENDING = "PENDING"
            VERIFIED = "VERIFIED"

        result = json.dumps({"status": Status.VERIFIED}, cls=DrSpecEncoder)
        data = json.loads(result)

        assert data["status"] == "VERIFIED"

    def test_encodes_path(self):
        """Test pathlib.Path serialization to string."""
        path = Path("/some/path/file.py")
        result = json.dumps({"file_path": path}, cls=DrSpecEncoder)
        data = json.loads(result)

        assert data["file_path"] == "/some/path/file.py"

    def test_encodes_nested_structures(self):
        """Test encoding nested structures with special types."""
        data = {
            "created_at": datetime(2024, 1, 15),
            "paths": [Path("/a"), Path("/b")],
            "status": ErrorCode.QUEUE_EMPTY,
        }
        result = json.dumps(data, cls=DrSpecEncoder)
        parsed = json.loads(result)

        assert parsed["created_at"] == "2024-01-15T00:00:00"
        assert parsed["paths"] == ["/a", "/b"]
        assert parsed["status"] == "QUEUE_EMPTY"


class TestOutputJson:
    """Tests for output_json function."""

    def test_output_json_compact(self, capsys):
        """Test compact JSON output (no whitespace)."""
        response = success_response({"key": "value"})
        output_json(response, pretty=False)

        captured = capsys.readouterr()
        # Compact JSON has no spaces
        assert " " not in captured.out.strip()
        assert json.loads(captured.out)

    def test_output_json_pretty(self, capsys):
        """Test pretty JSON output (indented)."""
        response = success_response({"key": "value"})
        output_json(response, pretty=True)

        captured = capsys.readouterr()
        # Pretty JSON has indentation
        assert "  " in captured.out
        assert json.loads(captured.out)

    def test_output_json_default_is_compact(self, capsys):
        """Test default output is compact."""
        response = success_response({"key": "value"})
        output_json(response)

        captured = capsys.readouterr()
        assert " " not in captured.out.strip()


class TestOutputPretty:
    """Tests for output_pretty function."""

    def test_output_success_pretty(self, capsys):
        """Test pretty output for success response."""
        response = success_response({
            "message": "Operation completed",
            "count": 42,
        })
        output_pretty(response)

        captured = capsys.readouterr()
        assert "Operation completed" in captured.out
        assert "Count: 42" in captured.out

    def test_output_error_pretty(self, capsys):
        """Test pretty output for error response."""
        response = error_response(
            "TEST_ERROR",
            "Something went wrong",
            {"path": "/test"}
        )
        output_pretty(response)

        captured = capsys.readouterr()
        assert "Error:" in captured.out
        assert "Something went wrong" in captured.out
        assert "TEST_ERROR" in captured.out

    def test_output_success_with_list(self, capsys):
        """Test pretty output with list values."""
        response = success_response({
            "message": "Found files",
            "files": ["a.py", "b.py", "c.py"],
        })
        output_pretty(response)

        captured = capsys.readouterr()
        assert "a.py" in captured.out
        assert "b.py" in captured.out

    def test_output_success_with_bool(self, capsys):
        """Test pretty output with boolean values."""
        response = success_response({
            "verified": True,
            "has_errors": False,
        })
        output_pretty(response)

        captured = capsys.readouterr()
        assert "Yes" in captured.out
        assert "No" in captured.out


class TestOutput:
    """Tests for the main output function."""

    def test_output_default_is_json(self, capsys):
        """Test default output mode is JSON."""
        response = success_response({"key": "value"})
        output(response)

        captured = capsys.readouterr()
        parsed = json.loads(captured.out)
        assert parsed["success"] is True

    def test_output_json_compact(self, capsys):
        """Test JSON output in compact mode."""
        response = success_response({"key": "value"})
        output(response, json_output=True, pretty=False)

        captured = capsys.readouterr()
        assert " " not in captured.out.strip()

    def test_output_json_pretty(self, capsys):
        """Test JSON output in pretty mode."""
        response = success_response({"key": "value"})
        output(response, json_output=True, pretty=True)

        captured = capsys.readouterr()
        assert "  " in captured.out

    def test_output_human_readable(self, capsys):
        """Test human-readable output mode."""
        response = success_response({
            "message": "Success!",
            "count": 5,
        })
        output(response, json_output=False)

        captured = capsys.readouterr()
        # Should not be valid JSON
        with pytest.raises(json.JSONDecodeError):
            json.loads(captured.out)
        # But should contain human-readable content
        assert "Success!" in captured.out
