"""Results that list several functions of one file print the file once."""

from __future__ import annotations

import json
import re

import pytest
from typer.testing import CliRunner

from drspec.cli.app import app
from drspec.contracts import Contract
from tests.helpers import fid

FILES = {
    "a.py": "def alpha():\n    return 1\n\n\ndef alpha_two(x):\n    return x\n\n\nclass K:\n    def alpha_method(self):\n        return 2\n",
    "b.py": "from a import alpha\n\n\ndef beta():\n    return alpha()\n",
    "web/c.js": "function alphaScript(a) { return a; }\n",
}


@pytest.fixture
def project(kept_project):
    tmp_path = kept_project  # the working folder, with an empty project database in memory: `init` is not the objective here
    for name, source in FILES.items():
        (tmp_path / name).parent.mkdir(exist_ok=True)
        (tmp_path / name).write_text(source)
    runner = CliRunner()
    assert runner.invoke(app, ["--json", "scan", "."]).exit_code == 0
    return runner


def _text(runner, *args):
    result = runner.invoke(app, list(args))
    assert result.exit_code == 0, result.output
    return result.output


class TestSearch:
    def test_each_file_is_printed_once_with_its_language_and_count(self, project):
        text = _text(project, "search", "alpha")
        assert re.findall(r"^  File: (\S+)", text, re.M) == ["a.py", "web/c.js", "b.py"]  # best match first: names that contain "alpha", then the body that only calls it
        assert "File: a.py   (python, 3 matches)" in text
        assert "File: web/c.js   (javascript, 1 match)" in text
        assert text.count("a.py") == 1  # the path is not repeated for each function

    def test_the_header_states_how_to_rebuild_an_identifier(self, project):
        text = _text(project, "search", "alpha")
        assert "Function ID: <File>::<ID>" in text
        assert text.count("Function ID:") == 1

    def test_file_plus_the_printed_id_is_accepted_by_other_commands(self, project):
        text = _text(project, "search", "alpha")
        found = 0
        current = None
        for line in text.splitlines():
            if line.startswith("  File: "):
                current = line.split()[1]
            elif line.strip().startswith("ID: "):
                identifier = current + "::" + line.strip()[4:]
                result = project.invoke(app, ["--json", "source", "get", identifier])
                assert result.exit_code == 0, (identifier, result.output)
                found += 1
        assert found == 5

    def test_every_field_stays_on_every_function_and_there_is_no_name_line(self, project):
        text = _text(project, "search", "alpha")
        assert text.count("      Status: PENDING") == 5
        assert text.count("      Possibly Stale: No") == 5
        assert text.count("      Signature: ") == 5
        assert "Name:" not in text

    def test_a_method_shows_its_class(self, project):
        assert "ID: K.alpha_method#" in _text(project, "search", "alpha_method")

    def test_json_is_unchanged(self, project):
        result = project.invoke(app, ["--json", "search", "alpha", "--code-only"])
        data = json.loads(result.output)["data"]
        first = data["matches"][0]
        assert set(first) == {"function_id", "function_name", "file_path", "language", "status", "signature", "possibly_stale"}
        default = json.loads(project.invoke(app, ["--json", "search", "alpha"]).output)["data"]["matches"][0]
        assert default == first  # documents are off by default, so the default listing is the old one
        assert first["function_id"] == fid("a.py", "alpha")

    def test_one_match_per_file_is_not_longer_than_a_group_line_more(self, project):
        grouped = _text(project, "search", "beta")
        assert grouped.count("\n") <= 12


class TestOtherListings:
    def test_unused_groups_by_file(self, project):
        text = _text(project, "deps", "unused")
        assert "File: a.py" in text and "Function ID: <File>::<ID>" in text

    def test_callers_are_grouped_by_the_file_they_are_in(self, project):
        text = _text(project, "deps", "get", fid("a.py", "alpha"), "--direction", "callers")
        assert "File: b.py   (1 function)" in text
        assert re.search(r"<--calls-- beta#[0-9a-f]{8}", text)

    def test_contract_list_groups_by_file(self, project):
        result = project.invoke(
            app, ["--json", "contract", "save", fid("a.py", "alpha"), "--confidence", "80"],
            input=Contract.from_dict(dict(
                function_signature="def alpha():", intent_summary="Return one",
                invariants=[dict(name="rule0", logic="Output is positive", criticality="HIGH", on_fail="error")],
            )).to_json(),
        )
        assert result.exit_code == 0, result.output
        text = _text(project, "contract", "list")
        assert "File: a.py" in text and "ID: alpha#" in text


class TestRankedResults:
    def test_a_ranking_is_printed_in_rank_order_not_regrouped_by_file(self):
        from drspec.cli.output import render_text

        matches = [
            {"function_id": "b.py::far#11111111", "function_name": "far", "file_path": "b.py", "language": "python", "score": 0.445},
            {"function_id": "a.py::near#22222222", "function_name": "near", "file_path": "a.py", "language": "python", "score": 0.314},
            {"function_id": "b.py::other#33333333", "function_name": "other", "file_path": "b.py", "language": "python", "score": 0.2},
        ]
        text = render_text({"success": True, "data": {"semantic": True, "matches": matches}})
        order = [line.split(": ", 1)[1] for line in text.splitlines() if line.startswith("  ID: ")]
        assert order == ["b.py::far#11111111", "a.py::near#22222222", "b.py::other#33333333"]
        assert "File:" in text and "Score: 0.445" in text and "Function ID: <File>::<ID>" not in text

    def test_an_unranked_list_is_still_grouped(self):
        from drspec.cli.output import render_text

        matches = [
            {"function_id": "b.py::x#11111111", "file_path": "b.py", "language": "python"},
            {"function_id": "a.py::y#22222222", "file_path": "a.py", "language": "python"},
            {"function_id": "b.py::z#33333333", "file_path": "b.py", "language": "python"},
        ]
        text = render_text({"success": True, "data": {"matches": matches}})
        assert text.count("File: b.py") == 1
