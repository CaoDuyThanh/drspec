"""Tests for the CLI output module: the JSON envelope, the plain-text rendering and its label vocabulary. The pretty output is compared whole: it is
what a person reads and what the agent prompts quote."""

import json
from datetime import datetime
from enum import Enum
from pathlib import Path

import pytest

from drspec.cli.output import (
    DrSpecEncoder,
    ErrorCode,
    error_response,
    output,
    output_json,
    output_pretty,
    success_response,
)

pytestmark = pytest.mark.unit  # dictionaries in, text on stdout out


class TestErrorCode:
    def test_error_codes_are_screaming_snake_case(self):
        for code in ErrorCode:
            assert code.value == code.value.upper()
            assert "_" in code.value or code.value.isalpha()
            # no consecutive underscores, no leading/trailing underscore
            assert "__" not in code.value
            assert not code.value.startswith("_")
            assert not code.value.endswith("_")

    def test_the_error_codes_are_the_published_set(self):
        # the codes are part of the JSON a caller (an agent, a script) matches on: adding one is a decision, removing or renaming one a break
        assert sorted(code.value for code in ErrorCode) == [
            "CONTRACT_NOT_FOUND", "DATABASE_BUSY", "DB_NOT_INITIALIZED", "EXECUTION_ERROR", "FUNCTION_NOT_FOUND", "INIT_FAILED", "INTERNAL_ERROR",
            "INVALID_FUNCTION_ID", "INVALID_INPUT", "INVALID_JSON", "INVALID_SCHEMA", "NOT_FOUND", "PARSE_ERROR", "PATH_NOT_FOUND",
            "PERMISSION_DENIED", "SCAN_ERROR", "STATUS_ERROR", "UNSUPPORTED_LANGUAGE", "VALIDATION_ERROR", "VERIFICATION_FAILED",
        ]
        assert all(code.name == code.value for code in ErrorCode)  # the member is named as its value


class TestSuccessResponse:
    def test_success_response_structure(self):
        response = success_response({"key": "value"})

        assert response == {"success": True, "data": {"key": "value"}, "error": None}

    def test_success_response_with_complex_data(self):
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

        assert response == {"success": True, "data": data, "error": None}  # the data is carried as given

    def test_success_response_with_empty_data(self):
        response = success_response({})

        assert response == {"success": True, "data": {}, "error": None}


class TestErrorResponse:
    def test_error_response_structure(self):
        response = error_response("TEST_ERROR", "Something went wrong")

        assert response == {"success": False, "data": None, "error": {"code": "TEST_ERROR", "message": "Something went wrong", "details": {}}}

    def test_error_response_with_details(self):
        details = {"path": "/some/path", "line": 42}
        response = error_response("PARSE_ERROR", "Failed to parse", details)

        assert response == {"success": False, "data": None, "error": {"code": "PARSE_ERROR", "message": "Failed to parse", "details": details}}

    def test_error_response_with_error_code_enum(self):
        response = error_response(
            ErrorCode.FUNCTION_NOT_FOUND,
            "No function with ID test.py::foo"
        )

        assert response["error"] == {"code": "FUNCTION_NOT_FOUND", "message": "No function with ID test.py::foo", "details": {}}
        assert type(response["error"]["code"]) is str  # the value of the enum, so that it is plain JSON


class TestDrSpecEncoder:
    def test_encodes_datetime(self):
        dt = datetime(2024, 1, 15, 10, 30, 0)
        result = json.dumps({"created_at": dt}, cls=DrSpecEncoder)
        data = json.loads(result)

        assert data["created_at"] == "2024-01-15T10:30:00"

    def test_encodes_enum(self):
        class Status(Enum):
            PENDING = "PENDING"
            VERIFIED = "VERIFIED"

        result = json.dumps({"status": Status.VERIFIED}, cls=DrSpecEncoder)
        data = json.loads(result)

        assert data["status"] == "VERIFIED"

    def test_encodes_path(self):
        path = Path("/some/path/file.py")
        result = json.dumps({"file_path": path}, cls=DrSpecEncoder)
        data = json.loads(result)

        assert data["file_path"] == "/some/path/file.py"

    def test_encodes_nested_structures(self):
        data = {
            "created_at": datetime(2024, 1, 15),
            "paths": [Path("/a"), Path("/b")],
            "status": ErrorCode.FUNCTION_NOT_FOUND,
        }
        result = json.dumps(data, cls=DrSpecEncoder)
        parsed = json.loads(result)

        assert parsed["created_at"] == "2024-01-15T00:00:00"
        assert parsed["paths"] == ["/a", "/b"]
        assert parsed["status"] == "FUNCTION_NOT_FOUND"


class TestOutputJson:
    def test_output_json_compact(self, capsys):
        response = success_response({"key": "value"})
        output_json(response)

        assert capsys.readouterr().out == '{"success":true,"data":{"key":"value"},"error":null}\n'  # one compact line


class TestOutputPretty:
    def test_output_success_pretty(self, capsys):
        response = success_response({
            "message": "Operation completed",
            "count": 42,
        })
        output_pretty(response)

        assert capsys.readouterr().out == "✓ Operation completed\n\nCount: 42\n"

    def test_output_error_pretty(self, capsys):
        response = error_response(
            "TEST_ERROR",
            "Something went wrong",
            {"path": "/test"}
        )
        output_pretty(response)

        assert capsys.readouterr().out == "✗ Error: Something went wrong\n  Code: TEST_ERROR\n  Details:\n    Path: /test\n"

    def test_output_success_with_list(self, capsys):
        response = success_response({
            "message": "Found files",
            "files": ["a.py", "b.py", "c.py"],
        })
        output_pretty(response)

        assert capsys.readouterr().out == "✓ Found files\n\nFiles:\n  - a.py\n  - b.py\n  - c.py\n"

    def test_output_success_with_bool(self, capsys):
        response = success_response({
            "verified": True,
            "has_errors": False,
        })
        output_pretty(response)

        assert capsys.readouterr().out == "Verified: Yes\nHas Errors: No\n"


class TestLabelVocabulary:
    """One shared label vocabulary and record/edge rendering grammar across
    every command, not a per-command format."""

    def test_same_label_across_two_different_commands(self, capsys):
        """`ID`/`Status`/`File` must render identically for the same
        underlying field names regardless of which command produced them —
        e.g. `search`'s matches and `status`'s nested artifact summary."""
        search_like = success_response({
            "matches": [
                {"function_id": "a.py::foo", "file_path": "a.py", "status": "VERIFIED"},
            ],
        })
        output_pretty(search_like)
        search_out = capsys.readouterr().out

        deps_like = success_response({
            "function_id": "a.py::foo",
            "file_path": "a.py",
            "status": "VERIFIED",
        })
        output_pretty(deps_like)
        deps_out = capsys.readouterr().out

        # a list of matches is grouped by file (the file is the header, the ID is given within it); a single record lists its fields in order
        assert search_out == "Function ID: <File>::<ID>\nMatches:\n  File: a.py   (1 match)\n    ID: foo\n      Status: VERIFIED\n"
        assert deps_out == "ID: a.py::foo\nFile: a.py\nStatus: VERIFIED\n"
        for label in ("ID:", "File:", "Status:"):  # the same three labels, whichever command's shape
            assert label in search_out and label in deps_out

    def test_record_list_header_not_duplicated_inline(self, capsys):
        """A list-of-records block gets one header line naming the record by
        its identifying field, which must not also be repeated as an
        indented field underneath itself."""
        response = success_response({
            "matches": [
                {"function_id": "a.py::foo", "status": "VERIFIED"},
            ],
        })
        output_pretty(response)
        out = capsys.readouterr().out

        assert out == "Matches:\n  ID: a.py::foo\n    Status: VERIFIED\n"  # the identifying field is the header and not repeated underneath

    def test_edge_list_uses_arrow_notation(self, capsys):
        """`deps get`'s callees/callers lists use arrow notation instead of
        generic record blocks."""
        response = success_response({
            "function_id": "a.py::f",
            "callees": [
                {"function_id": "b.py::helper", "relation": "calls", "status": "VERIFIED"},
            ],
            "callers": [
                {"function_id": "c.py::caller", "relation": "references", "status": "PENDING"},
            ],
        })
        output_pretty(response)
        out = capsys.readouterr().out

        assert out == (
            "ID: a.py::f\nCallees:\n  --calls--> b.py::helper\n    Status: VERIFIED\nCallers:\n  <--references-- c.py::caller\n    Status: PENDING\n"
        )  # the edge's own function_id and relation are not repeated as plain fields

    def test_multi_section_nesting_stays_indented(self, capsys):
        """A deeply nested multi-section response (`status`'s own shape)
        keeps consistent, increasing indentation per nesting level."""
        response = success_response({
            "artifacts": {
                "total": 2,
                "by_status": {"PENDING": 2, "VERIFIED": 0},
            },
        })
        output_pretty(response)
        # nested sections indent one level deeper than their parent
        assert capsys.readouterr().out == "Artifacts:\n  Total: 2\n  By Status:\n    Pending: 2\n    Verified: 0\n"

    def test_ids_used_as_dictionary_keys_print_verbatim(self, capsys):
        """`scan`'s `resolution_reasons` is keyed by function ID; title-casing
        the key would print `Calc.Py::Add`, an ID that fails when copied
        back into `deps_get`/`source_get`."""
        response = success_response({
            "resolution_reasons": {"calc.py::add": "symbols_changed", "src/app.py::Run.go": "forced"},
        })
        output_pretty(response)
        out = capsys.readouterr().out

        assert out == "Resolution Reasons:\n  calc.py::add: symbols_changed\n  src/app.py::Run.go: forced\n"  # the ids verbatim, not "Calc.Py::Add"

    def test_field_name_keys_still_get_title_cased(self, capsys):
        output_pretty(success_response({"files_scanned": 3, "by_status": {"PENDING": 1}}))
        out = capsys.readouterr().out

        assert out == "Files Scanned: 3\nBy Status:\n  Pending: 1\n"


class TestOutput:
    def test_output_default_is_plain_text(self, capsys):
        response = success_response({"key": "value"})
        output(response)

        assert capsys.readouterr().out == "Key: value\n"  # plain text unless asked for JSON

    def test_output_json_compact(self, capsys):
        response = success_response({"key": "value"})
        output(response, json_output=True)

        assert capsys.readouterr().out == '{"success":true,"data":{"key":"value"},"error":null}\n'

    def test_output_human_readable(self, capsys):
        response = success_response({
            "message": "Success!",
            "count": 5,
        })
        output(response, json_output=False)

        assert capsys.readouterr().out == "✓ Success!\n\nCount: 5\n"


class TestClippedLines:
    def test_a_clipped_hit_says_how_long_the_line_was(self, capsys):
        output_pretty(success_response({"hits": [
            {"file_path": "a.js", "line": 1, "text": "…needle…", "line_length": 5000, "function_id": None},
            {"file_path": "b.js", "line": 2, "text": "needle", "function_id": None},
        ]}))
        out = capsys.readouterr().out

        assert out == "Hits:\n  a.js:1:…needle…  [clipped, line is 5000 chars]\n  b.js:2:needle\n"  # only the clipped line says how long it was

    def test_a_clipped_context_line_says_how_long_it_was(self, capsys):
        output_pretty(success_response({"blocks": [{
            "file_path": "a.js", "start_line": 1, "end_line": 2, "matched_lines": [2], "functions": [],
            "context": [{"line": 1, "text": "…zzz…", "line_length": 5000}, {"line": 2, "text": "needle"}],
        }]}))
        out = capsys.readouterr().out

        assert out == "Blocks:\n  a.js (lines 1-2)\n    1- …zzz…  [clipped, line is 5000 chars]\n    2: needle\n"
