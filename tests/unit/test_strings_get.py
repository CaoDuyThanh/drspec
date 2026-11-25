"""`strings get`: the YAML sites of a key, the code that reads it by name, the sub-keys of a mapping, the notes that say what is and is not
found, the parse problems, the notice for a changed YAML file, and what a scan does to the YAML files (edited, deleted, switched off).
In memory: one small project scanned once for the read-only tests (KeptDuckDB, no `init`, no process); the scan-changing tests use their own
small project. Every file is changed in a tmp folder only."""

from __future__ import annotations

import json
import os
import sys

import duckdb
import pytest
from typer.testing import CliRunner

from drspec import mcp_server
from drspec.cli.app import app
from drspec.core import config_keys
from drspec.core import config_keys as ck
from drspec.core.strings import get_strings
from drspec.db import init_schema

runner = CliRunner()

PARAMS = "avoidance:\n  timeout: 5.0\n  retry:\n    count: 3\n  only_yaml: yes\n"
NODE_CPP = (
    "#include <ros/ros.h>\n"
    "void Node::init() { nh.getParam(\"avoidance/timeout\", timeout_); }\n"
    "void Node::comment() { // \"avoidance/only_in_comment\"\n}\n"
    "void Node::runtime(const std::string& name) { nh.getParam(\"avoidance/\" + name, v); }\n"
    "void Node::prefixed() { nh.getParam(\"/avoidance/timeout\", timeout_); }\n"
)
TOOL_PY = (
    "limit = rospy.get_param(\"avoidance/retry/count\")\n"
    "def load():\n"
    "    return rospy.get_param(\"read/only/in_code\")\n"
)
BROKEN_YAML = "avoidance:\n  timeout: [5.0\n"


@pytest.fixture(scope="module")
def project(tmp_path_factory):
    from tests.fakes import KeptDuckDB

    folder = tmp_path_factory.mktemp("strings")
    with pytest.MonkeyPatch.context() as patch:
        KeptDuckDB().install(patch)
        (folder / "_drspec").mkdir()
        (folder / "config").mkdir()
        (folder / "src").mkdir()
        (folder / "scripts").mkdir()
        (folder / "config" / "params.yaml").write_text(PARAMS)
        (folder / "config" / "broken.yaml").write_text(BROKEN_YAML)
        (folder / "src" / "node.cpp").write_text(NODE_CPP)
        (folder / "scripts" / "tool.py").write_text(TOOL_PY)
        connection = duckdb.connect(str(folder / "_drspec" / "contracts.db"))  # pattern-ok: duckdb-file kept in memory by `KeptDuckDB`
        init_schema(connection)
        connection.close()
        patch.chdir(folder)
        assert runner.invoke(app, ["--json", "scan", "."]).exit_code == 0
        yield folder


@pytest.fixture
def conn(project):
    connection = duckdb.connect(str(project / "_drspec" / "contracts.db"), read_only=True)  # pattern-ok: duckdb-file kept in memory by `KeptDuckDB`
    yield connection
    connection.close()


def _strings(*args):
    result = runner.invoke(app, ["--json", "strings", "get", *args])
    assert result.exit_code == 0, result.output
    return json.loads(result.output)["data"]


class TestTheSitesOfAKey:
    def test_a_key_lists_the_yaml_site_and_the_c_plus_plus_site(self, project, conn):
        data = get_strings(conn, "avoidance/timeout", root=project)
        assert data["yaml_sites"] == [{"file_path": "config/params.yaml", "line": 2, "value": "5.0"}]
        init, prefixed = data["code_sites"]  # (the leading-slash literal in Node::prefixed is the same key)
        assert init["function_name"] == "Node::init" and init["file_path"] == "src/node.cpp" and init["line"] == 2
        assert init["source"].startswith("void Node::init()") and "avoidance/timeout" in init["source"]
        assert prefixed["function_name"] == "Node::prefixed" and prefixed["line"] == 6
        assert "note" not in data  # both sides: nothing to say about what is missing

    def test_a_nested_key_lists_the_yaml_site_and_the_python_module_level_site(self, project, conn):
        data = get_strings(conn, "avoidance/retry/count", root=project)
        assert data["yaml_sites"] == [{"file_path": "config/params.yaml", "line": 4, "value": "3"}]
        (code,) = data["code_sites"]
        assert code["file_path"] == "scripts/tool.py" and code["line"] == 1
        assert code["function_name"] == "(module level)" and code["function_id"] is None

    def test_a_site_inside_a_function_is_named_with_its_function(self, project, conn):
        data = get_strings(conn, "read/only/in_code", root=project)
        (code,) = data["code_sites"]
        assert code["function_name"] == "load" and code["function_id"].startswith("scripts/tool.py::load#")

    def test_the_dotted_and_the_leading_slash_forms_find_the_same_sites(self, project, conn):
        plain = get_strings(conn, "avoidance/timeout", root=project)
        for form in ("avoidance.timeout", "/avoidance/timeout", "~/avoidance/timeout"):  # pattern-ok: outside-path the `~/` spelling of a key name (a key, not a filesystem path)
            other = get_strings(conn, form, root=project)
            assert other["yaml_sites"] == plain["yaml_sites"] and other["code_total"] == plain["code_total"], form

    def test_a_comment_a_run_time_key_and_a_substring_are_not_sites(self, project, conn):
        assert get_strings(conn, "avoidance/only_in_comment", root=project)["code_total"] == 0
        assert get_strings(conn, "avoidance/timeou", root=project)["code_total"] == 0
        assert get_strings(conn, "avoidance/timeout_s", root=project)["code_total"] == 0

    def test_a_key_built_at_run_time_is_not_found_and_the_note_says_so(self, project, conn):
        data = get_strings(conn, "avoidance/only_yaml", root=project)
        assert data["code_total"] == 0 and "no string literal equal to this key was found in code" in data["note"] and "built at run time" in data["note"]

    def test_the_prefix_form_lists_sub_keys_and_no_sites(self, project, conn):
        data = get_strings(conn, "avoidance/", root=project)
        assert data["leaves"] and "yaml_sites" not in data and "code_sites" not in data

    def test_a_limit_cuts_the_sites_and_the_total_is_kept(self, project, conn):
        data = get_strings(conn, "avoidance/timeout", limit=1, root=project)
        assert data["code_total"] == 2 and len(data["code_sites"]) == 1  # (the function and the prefixed one)


class TestTheNotes:
    def test_a_key_only_in_yaml_says_it_is_never_read_in_code(self, project, conn):
        data = get_strings(conn, "avoidance/only_yaml", root=project)
        assert data["yaml_total"] == 1 and data["code_total"] == 0
        assert data["note"].startswith("set in yaml; no string literal equal to this key was found in code")

    def test_a_key_only_in_code_says_it_is_not_set_in_any_yaml_file(self, project, conn):
        data = get_strings(conn, "read/only/in_code", root=project)
        assert data["yaml_total"] == 0 and data["code_total"] == 1
        assert data["note"].startswith("read in code, not set in any yaml file")

    def test_a_key_in_neither_says_so(self, project, conn):
        assert get_strings(conn, "nowhere/at/all", root=project)["note"].startswith("No yaml file sets this key")

    def test_a_mapping_lists_its_sub_keys_with_their_counts(self, project, conn):
        data = get_strings(conn, "avoidance", root=project)
        assert [row["key"] for row in data["leaves"]] == ["avoidance/only_yaml", "avoidance/retry/count", "avoidance/timeout"]
        assert "mapping in yaml" in data["note"] and data["sub_keys_total"] == 3  # the groups: only_yaml, retry, timeout

    def test_the_prefix_form_counts_the_keys_under_each_group(self, project, conn):
        groups = get_strings(conn, "avoidance/", root=project)["sub_keys_total"]
        assert groups == 3  # the groups under avoidance/: only_yaml, retry, timeout

    def test_an_empty_key_is_refused(self, project, conn):
        with pytest.raises(ValueError):
            get_strings(conn, "   ", root=project)


class TestAYamlFileThatDoesNotParse:
    def test_it_is_a_problem_with_its_line_and_its_keys_are_not_indexed(self, project, conn):
        data = get_strings(conn, "avoidance/timeout", root=project, problems=True)
        (problem,) = [row for row in data["problem_files"] if row["file_path"] == "config/broken.yaml"]
        assert problem["problem"]["line"] >= 2 and problem["problem"]["message"]
        assert "could not be read" in data["problem_note"] and "--problems" in data["problem_note"]
        assert all(site["file_path"] != "config/broken.yaml" for site in data["yaml_sites"])

    def test_the_scan_is_not_failed_by_it(self, project):
        assert runner.invoke(app, ["--json", "status"]).exit_code == 0


class TestStatusCountsTheYamlFiles:
    def test_the_counts_are_the_files_keys_and_problems(self, conn):
        counts = config_keys.config_counts(conn)
        assert counts == {"files": 2, "keys": 3, "files_with_problems": 1, "files_skipped": 0, "files_without_pyyaml": 0}  # params.yaml: timeout, retry/count, only_yaml


class TestTheMcpToolAndCompact:
    def test_the_tool_answers_in_json_and_its_compact_form_is_shorter_than_text(self, project):
        from drspec.core import compact_format

        db_path = project / "_drspec" / "contracts.db"
        data, failed = mcp_server._tool_strings_get(db_path, {"key": "avoidance/timeout"})
        assert not failed and data["yaml_total"] == 1 and data["code_total"] == 2
        assert len(compact_format.encode(data)) <= len(json.dumps(data, default=str)) and "~B" in compact_format.encode(data)

    def test_a_missing_key_is_an_error(self, project):
        assert mcp_server._tool_strings_get(project / "_drspec" / "contracts.db", {})[1] is True


class TestAChangedYamlFile:
    """Its own project (the scans change it): an edit, a delete and the switch are seen by the next scan, and the notice names the file."""

    @pytest.fixture
    def own(self, tmp_path):
        from tests.fakes import KeptDuckDB

        folder = tmp_path / "own"
        folder.mkdir()
        with pytest.MonkeyPatch.context() as patch:
            KeptDuckDB().install(patch)
            (folder / "_drspec").mkdir()
            (folder / "config.yaml").write_text("avoidance:\n  timeout: 5.0\n")
            (folder / "node.cpp").write_text(NODE_CPP)
            connection = duckdb.connect(str(folder / "_drspec" / "contracts.db"))  # pattern-ok: duckdb-file kept in memory by `KeptDuckDB`
            init_schema(connection)
            connection.close()
            patch.chdir(folder)
            assert runner.invoke(app, ["--json", "scan", "."]).exit_code == 0
            yield folder

    def test_an_edit_is_seen_by_the_next_scan_and_named_before_it(self, own):
        path = own / "config.yaml"
        stat = path.stat()
        path.write_text("avoidance:\n  timeout: 9.0\n")
        os.utime(path, ns=(stat.st_atime_ns, stat.st_mtime_ns + 5_000_000_000))
        before = _strings("avoidance/timeout")
        assert before["yaml_sites"][0]["value"] == "5.0"  # the index has not read the edit yet
        assert before["changed_since_scan"]["edited"] == ["config.yaml"] and "config.yaml" in before["note"]
        assert runner.invoke(app, ["--json", "scan", "."]).exit_code == 0
        assert _strings("avoidance/timeout")["yaml_sites"][0]["value"] == "9.0"

    def test_a_deleted_yaml_file_loses_its_keys_on_the_next_scan(self, own):
        (own / "config.yaml").unlink()
        assert runner.invoke(app, ["--json", "scan", "."]).exit_code == 0
        data = _strings("avoidance/timeout")
        assert data["yaml_total"] == 0 and "yaml_config" not in json.loads(runner.invoke(app, ["--json", "status"]).output)["data"]

    def test_switching_it_off_removes_the_keys_and_switching_on_reads_them_again(self, own):
        assert runner.invoke(app, ["--json", "config", "set", "strings.enabled", "false"]).exit_code == 0
        assert runner.invoke(app, ["--json", "scan", "."]).exit_code == 0
        assert _strings("avoidance/timeout")["yaml_total"] == 0
        assert runner.invoke(app, ["--json", "config", "set", "strings.enabled", "true"]).exit_code == 0
        assert runner.invoke(app, ["--json", "scan", "."]).exit_code == 0
        assert _strings("avoidance/timeout")["yaml_total"] == 1

    def test_a_project_without_yaml_files_has_no_yaml_rows_and_no_yaml_block_in_status(self, tmp_path):
        from tests.fakes import KeptDuckDB

        folder = tmp_path / "plain"
        folder.mkdir()
        with pytest.MonkeyPatch.context() as patch:
            KeptDuckDB().install(patch)
            (folder / "_drspec").mkdir()
            (folder / "a.cpp").write_text(NODE_CPP)
            connection = duckdb.connect(str(folder / "_drspec" / "contracts.db"))  # pattern-ok: duckdb-file kept in memory by `KeptDuckDB`
            init_schema(connection)
            connection.close()
            patch.chdir(folder)
            assert runner.invoke(app, ["--json", "scan", "."]).exit_code == 0
            connection = duckdb.connect(str(folder / "_drspec" / "contracts.db"), read_only=True)  # pattern-ok: duckdb-file kept in memory by `KeptDuckDB`
            try:
                assert connection.execute("SELECT count(*) FROM config_keys").fetchone()[0] == 0
                assert connection.execute("SELECT count(*) FROM config_files").fetchone()[0] == 0
            finally:
                connection.close()
            assert "yaml_config" not in json.loads(runner.invoke(app, ["--json", "status"]).output)["data"]


class TestWithoutPyYAMLTheScanStillWorks:
    """An install without PyYAML: the scan of a project with yaml files succeeds, the yaml files are skipped with the reason, and
    `strings get` and `status` say why (in memory: PyYAML is blocked in `sys.modules` for the scan of this module's own project)."""

    @pytest.fixture
    def scanned(self, tmp_path, monkeypatch):
        from tests.fakes import KeptDuckDB

        folder = tmp_path / "noyaml"
        folder.mkdir()
        with pytest.MonkeyPatch.context() as patch:
            KeptDuckDB().install(patch)
            (folder / "_drspec").mkdir()
            (folder / "config.yaml").write_text("avoidance:\n  timeout: 5.0\n")
            (folder / "node.cpp").write_text(NODE_CPP)
            connection = duckdb.connect(str(folder / "_drspec" / "contracts.db"))  # pattern-ok: duckdb-file kept in memory by `KeptDuckDB`
            init_schema(connection)
            connection.close()
            patch.chdir(folder)
            monkeypatch.setitem(sys.modules, "yaml", None)  # PyYAML not installed
            result = runner.invoke(app, ["--json", "scan", "."])
            yield folder, result

    def test_the_scan_succeeds_and_says_once_that_the_yaml_file_was_not_read(self, scanned):
        folder, result = scanned
        assert result.exit_code == 0, result.output
        data = json.loads(result.output)["data"]
        assert data["note"].count("yaml file(s) not read") == 1 and "PyYAML is not installed" in data["note"]
        assert data["state"] == "done"

    def test_the_code_is_still_indexed(self, scanned):
        folder, _result = scanned
        connection = duckdb.connect(str(folder / "_drspec" / "contracts.db"), read_only=True)  # pattern-ok: duckdb-file kept in memory by `KeptDuckDB`
        try:
            assert connection.execute("SELECT count(*) FROM artifacts WHERE function_name = 'init'").fetchone()[0] == 1
            assert connection.execute("SELECT count(*) FROM config_keys").fetchone()[0] == 0
            assert connection.execute("SELECT skipped FROM config_files WHERE file_path = 'config.yaml'").fetchone()[0] == ck.YAML_MISSING
        finally:
            connection.close()

    def test_strings_get_and_status_say_why(self, scanned, monkeypatch):
        folder, _result = scanned
        monkeypatch.setitem(sys.modules, "yaml", None)
        connection = duckdb.connect(str(folder / "_drspec" / "contracts.db"), read_only=True)  # pattern-ok: duckdb-file kept in memory by `KeptDuckDB`
        try:
            data = get_strings(connection, "avoidance/timeout", root=folder)
        finally:
            connection.close()
        assert data["files_without_pyyaml"] == 1 and data["yaml_total"] == 0 and data["code_total"] == 2  # (Node::init and Node::prefixed)
        assert "not known whether it is set in yaml" in data["note"] and "PyYAML is not installed" in data["pyyaml_note"]
        assert "read in code, not set in any yaml file" not in data["note"]  # (it is not known, so the note does not claim it)
        status = json.loads(runner.invoke(app, ["--json", "status"]).output)["data"]
        assert status["yaml_config"]["files_without_pyyaml"] == 1 and status["yaml_config"]["message"] == ck.YAML_MISSING


ROBOT_YAML = "robot:\n  speed:\n    max: 1.0\n    min: 0.1\n  name: demo\nhigh/robot:\n  speed:\n    max: 2.0\n"
LOADER_CPP = (
    "#include <string>\n"
    "struct NodeHandle { template <class T> bool getParam(const std::string&, T&) const { return true; } };\n"
    "double readMax(const NodeHandle& nh) { double v = 0.0; nh.getParam(\"robot/speed/max\", v); return v; }\n"
    "double readMaxWithProfile(const NodeHandle& nh, const std::string& profile) {\n"
    "    double v = 0.0;\n"
    "    const std::string suffix = \"robot/speed/max\";\n"
    "    nh.getParam(profile + suffix, v);\n"
    "    return v;\n"
    "}\n"
)


@pytest.fixture(scope="module")
def robot(tmp_path_factory):
    """The repro of T-191: a profile key read through a run-time prefix, a leaf set, and two yaml files that do not parse. Scanned once."""
    from tests.fakes import KeptDuckDB

    folder = tmp_path_factory.mktemp("robot")
    with pytest.MonkeyPatch.context() as patch:
        KeptDuckDB().install(patch)
        (folder / "_drspec").mkdir()
        (folder / "config").mkdir()
        (folder / "src").mkdir()
        (folder / "broken").mkdir()
        (folder / "config" / "robot.yaml").write_text(ROBOT_YAML)
        (folder / "src" / "loader.cpp").write_text(LOADER_CPP)
        for index in (1, 2):
            (folder / "broken" / f"bad_{index}.yaml").write_text(f"key_{index}: [unclosed\n  other: value\n")
        connection = duckdb.connect(str(folder / "_drspec" / "contracts.db"))  # pattern-ok: duckdb-file kept in memory by `KeptDuckDB`
        init_schema(connection)
        connection.close()
        patch.chdir(folder)
        assert runner.invoke(app, ["--json", "scan", "."]).exit_code == 0
        yield folder


def _robot_conn(folder):
    return duckdb.connect(str(folder / "_drspec" / "contracts.db"), read_only=True)  # pattern-ok: duckdb-file kept in memory by `KeptDuckDB`


class TestAProfilePrefixIsAPossibleSite:
    """`high/robot/speed/max` is read as `profile + suffix`: no literal equals it, but `robot/speed/max` is in code. That is a hint, not a read."""

    def test_the_verdict_is_no_literal_found_not_never_read(self, robot):
        conn = _robot_conn(robot)
        try:
            data = get_strings(conn, "high/robot/speed/max", root=robot)
        finally:
            conn.close()
        assert data["code_total"] == 0 and data["yaml_total"] == 1
        assert "never read" not in data["note"] and "no string literal equal to this key was found in code" in data["note"]

    def test_the_hint_names_the_functions_where_the_rest_of_the_key_is_a_literal(self, robot):
        conn = _robot_conn(robot)
        try:
            data = get_strings(conn, "high/robot/speed/max", root=robot)
        finally:
            conn.close()
        names = [(site["function_name"], site["line"], site["literal"]) for site in data["possible_prefix_sites"]]
        assert ("readMax", 3, "robot/speed/max") in names and ("readMaxWithProfile", 6, "robot/speed/max") in names  # (line 6: the suffix literal)
        assert len(names) <= 3 and all(site["file_path"] == "src/loader.cpp" for site in data["possible_prefix_sites"])

    def test_the_hints_are_not_reads(self, robot):
        conn = _robot_conn(robot)
        try:
            data = get_strings(conn, "high/robot/speed/max", root=robot)
        finally:
            conn.close()
        assert data["code_total"] == 0 and data["code_sites"] == []  # `Code Total` stays 0

    def test_a_key_with_a_read_in_code_gets_no_hint(self, robot):
        conn = _robot_conn(robot)
        try:
            data = get_strings(conn, "robot/speed/max", root=robot)
        finally:
            conn.close()
        assert data["code_total"] == 2 and "possible_prefix_sites" not in data

    def test_the_cli_text_names_the_function(self, robot, monkeypatch):
        monkeypatch.chdir(robot)
        text = runner.invoke(app, ["strings", "get", "high/robot/speed/max"]).output
        assert "Code Total: 0" in text and "Possible Prefix Sites:" in text and "readMax" in text and "readMaxWithProfile" in text
        assert "never read" not in text

    def test_a_tail_needs_two_segments(self):
        from drspec.core.strings import tails_of

        assert tails_of("high/robot/speed/max") == ["robot/speed/max", "speed/max"]
        assert tails_of("high/max") == [] and tails_of("robot/max") == []  # (a single word is not a hint)


class TestTheProblemFilesAreAListOnRequest:
    def test_the_answer_says_how_many_and_how_to_list_them(self, robot):
        conn = _robot_conn(robot)
        try:
            data = get_strings(conn, "robot/speed/min", root=robot)
        finally:
            conn.close()
        assert data["problem_files_total"] == 2 and "problem_files" not in data
        assert "`drspec strings get --problems` lists them" in data["problem_note"]

    def test_problems_lists_them_capped_by_the_limit(self, robot):
        conn = _robot_conn(robot)
        try:
            data = get_strings(conn, "robot/speed/min", limit=1, root=robot, problems=True)
        finally:
            conn.close()
        assert len(data["problem_files"]) == 1 and data["problem_files_more"] == 1

    def test_the_cli_option_lists_them_and_the_default_does_not(self, robot, monkeypatch):
        monkeypatch.chdir(robot)
        plain = runner.invoke(app, ["strings", "get", "robot/speed/min"]).output
        listed = runner.invoke(app, ["strings", "get", "robot/speed/min", "--problems", "--limit", "1"]).output
        assert "Problem Files:" not in plain and "Problem Files Total: 2" in plain
        assert "Problem Files:" in listed and "Problem Files More: 1" in listed

    def test_the_short_answer_is_under_fifteen_lines(self, robot, monkeypatch):
        monkeypatch.chdir(robot)
        assert len(runner.invoke(app, ["strings", "get", "robot/speed/min"]).output.strip().split("\n")) < 15


class TestAPrefixListsEachLeaf:
    def test_each_leaf_has_its_value_its_site_and_its_reader_or_none(self, robot):
        conn = _robot_conn(robot)
        try:
            data = get_strings(conn, "robot/", root=robot)
        finally:
            conn.close()
        assert data["leaves_total"] == 3
        by_key = {leaf["key"]: leaf for leaf in data["leaves"]}
        assert by_key["robot/speed/max"]["value"] == "1.0" and by_key["robot/speed/max"]["line"] == 3
        assert by_key["robot/speed/max"]["read_function"] == "readMax" and by_key["robot/speed/max"]["read_file"] == "src/loader.cpp"
        assert by_key["robot/speed/min"]["read_file"] is None and by_key["robot/name"]["read_file"] is None

    def test_the_limit_caps_the_leaves_and_says_how_many_more(self, robot):
        conn = _robot_conn(robot)
        try:
            data = get_strings(conn, "robot/", limit=2, root=robot)
        finally:
            conn.close()
        assert len(data["leaves"]) == 2 and data["leaves_more"] == 1 and "1 more leaves: narrow the prefix" in data["note"]

    def test_each_leaf_is_one_line_in_the_text(self, robot, monkeypatch):
        monkeypatch.chdir(robot)
        text = runner.invoke(app, ["strings", "get", "robot/"]).output
        assert text.count("config/robot.yaml") == 1 and "  config/robot.yaml (3)\n" in text
        assert "    3  robot/speed/max = 1.0  read: src/loader.cpp readMax\n" in text
        assert "    4  robot/speed/min = 0.1  read: none\n" in text
        assert "Sub Keys:" not in text  # the groups are one summary line

    def test_json_has_the_leaves_and_the_compact_table_is_shorter_than_the_text(self, robot, monkeypatch):
        from drspec.cli.output import render_text, success_response
        from drspec.core import compact_format

        monkeypatch.chdir(robot)
        data = json.loads(runner.invoke(app, ["--json", "strings", "get", "robot/"]).output)["data"]
        assert [leaf["key"] for leaf in data["leaves"]] == ["robot/name", "robot/speed/max", "robot/speed/min"]
        # A listing of a real size (30 leaves): the table's header is paid once, so it is shorter than the labelled text.
        big = dict(data, leaves=[dict(leaf, key=f"robot/group_{i}/value_{i}", value=f"{i}.5", line=i + 3) for i, leaf in enumerate(data["leaves"] * 10)][:30], leaves_total=30)
        assert len(compact_format.encode(big)) < len(render_text(success_response(big)))

    def test_the_mcp_tool_gives_the_same_leaves(self, robot):
        from drspec import mcp_server

        data, failed = mcp_server._tool_strings_get(robot / "_drspec" / "contracts.db", {"key": "robot/", "limit": 1})  # noqa
        assert not failed and len(data["leaves"]) == 1 and data["leaves_more"] == 2
        assert mcp_server._tool_strings_get(robot / "_drspec" / "contracts.db", {"key": "robot/", "problems": True})[0]["problem_files_total"] == 2


def _leaf(key, value, line, path, reader_file=None, reader_function=None):
    return {"key": key, "value": value, "file_path": path, "line": line, "read_file": reader_file, "read_function": reader_function, "read_line": None}


def _text_of(leaves, **extra):
    from drspec.cli.output import render_text, success_response

    return render_text(success_response({"key": "robot/", "leaves_total": len(leaves), "leaves": leaves, **extra})) + "\n"  # (as the CLI prints it)


class TestTheTextGroupsLeavesByFile:
    """The text form prints a yaml file once above its leaves (with the count), and a reader file once per run of lines that name it. The
    JSON and the compact form are the same as before (a pinned compact form below)."""

    def test_one_group_prints_the_file_once_with_its_count_and_no_path_on_the_leaves(self):
        text = _text_of([
            _leaf("robot/name", "demo", 5, "config/robot.yaml"),
            _leaf("robot/speed/max", "1.0", 3, "config/robot.yaml", "src/loader.cpp", "readMax"),
            _leaf("robot/speed/min", "0.1", 4, "config/robot.yaml"),
        ])
        assert text.count("config/robot.yaml") == 1 and "  config/robot.yaml (3)\n" in text
        assert "    5  robot/name = demo  read: none\n" in text
        assert "    3  robot/speed/max = 1.0  read: src/loader.cpp readMax\n" in text  # (the first reader of a run names its file)
        assert "    4  robot/speed/min = 0.1  read: none\n" in text

    def test_several_groups_each_file_once_in_the_order_of_their_first_leaf(self):
        text = _text_of([
            _leaf("a/x", "1", 2, "config/b.yaml"),
            _leaf("a/y", "2", 3, "config/a.yaml"),
            _leaf("a/z", "3", 4, "config/b.yaml"),
        ])
        assert text.index("config/b.yaml (2)") < text.index("config/a.yaml") and text.count("config/b.yaml") == 1
        assert text.index("    2  a/x = 1") < text.index("    4  a/z = 3") < text.index("    3  a/y = 2")  # b's two leaves, then a's leaf

    def test_a_single_leaf_group_has_no_count(self):
        text = _text_of([_leaf("a/x", "1", 2, "config/only.yaml")])
        assert "  config/only.yaml\n" in text and "(1)" not in text and "    2  a/x = 1  read: none\n" in text

    def test_a_leaf_with_no_reader_says_none_and_the_next_reader_names_its_file_again(self):
        text = _text_of([
            _leaf("a/x", "1", 2, "config/a.yaml", "src/f.cpp", "first"),
            _leaf("a/y", "2", 3, "config/a.yaml"),
            _leaf("a/z", "3", 4, "config/a.yaml", "src/f.cpp", "third"),
        ])
        assert "    2  a/x = 1  read: src/f.cpp first\n" in text
        assert "    3  a/y = 2  read: none\n" in text
        assert "    4  a/z = 3  read: src/f.cpp third\n" in text  # (after a leaf with no reader the file is named again)

    def test_the_same_reader_file_on_consecutive_lines_prints_the_function_only(self):
        text = _text_of([
            _leaf("a/x", "1", 2, "config/a.yaml", "src/f.cpp", "readMax"),
            _leaf("a/y", "2", 3, "config/a.yaml", "src/f.cpp", "readMin"),
            _leaf("a/z", "3", 4, "config/a.yaml", "src/g.cpp", "other"),
        ])
        assert "    2  a/x = 1  read: src/f.cpp readMax\n" in text
        assert "    3  a/y = 2  read: readMin\n" in text  # the line above names the same file: the function only
        assert "    4  a/z = 3  read: src/g.cpp other\n" in text

    def test_a_new_group_names_its_reader_file_even_when_the_last_group_did(self):
        text = _text_of([
            _leaf("a/x", "1", 2, "config/a.yaml", "src/f.cpp", "readMax"),
            _leaf("a/y", "2", 3, "config/b.yaml", "src/f.cpp", "readMin"),
        ])
        assert "    3  a/y = 2  read: src/f.cpp readMin\n" in text  # (the group's header is the line above, not a reader)

    def test_the_limit_and_the_more_line_are_unchanged(self, robot, monkeypatch):
        monkeypatch.chdir(robot)
        text = runner.invoke(app, ["strings", "get", "robot/", "--limit", "2"]).output
        assert text.count("    ") >= 2 and "1 more leaves: narrow the prefix" in text
        data = json.loads(runner.invoke(app, ["--json", "strings", "get", "robot/", "--limit", "2"]).output)["data"]
        assert data["leaves_total"] == 3 and data["leaves_more"] == 1 and len(data["leaves"]) == 2

    def test_json_and_compact_are_the_same_as_before_the_grouping(self):
        from drspec.core import compact_format

        leaves = [
            _leaf("robot/name", "demo", 5, "config/robot.yaml"),
            _leaf("robot/speed/max", "1.0", 3, "config/robot.yaml", "src/loader.cpp", "readMax"),
            _leaf("robot/speed/min", "0.1", 4, "config/robot.yaml"),
            _leaf("tune/a", "2", 1, "config/tune.yaml", "src/loader.cpp", "readMaxWithProfile"),
        ]
        data = {"key": "robot/", "leaves_total": 4, "leaves": leaves, "code_files_read": 1, "sub_keys_total": 2}
        # (pinned from the encoder, which this change does not touch: the same bytes as before the grouping)
        assert compact_format.encode(data) == (
            "~B\nkey: robot/\nleaves_total: 4\n"
            "= leaves key value file_path line read_file read_function read_line\tread_line=null\n"
            "|robot/name\tdemo\tconfig/robot.yaml\t5\tnull\tnull\n"
            "|robot/speed/max\t\"1.0\"\tconfig/robot.yaml\t3\tsrc/loader.cpp\treadMax\n"
            "|robot/speed/min\t\"0.1\"\tconfig/robot.yaml\t4\tnull\tnull\n"
            "|tune/a\t\"2\"\tconfig/tune.yaml\t1\tsrc/loader.cpp\treadMaxWithProfile\n"
            "code_files_read: 1\nsub_keys_total: 2"
        )


class TestRepeatedYamlSitesAreGroupedByFile:
    def test_a_file_that_sets_the_key_twice_prints_its_path_once(self):
        from drspec.cli.output import render_text, success_response

        sites = [{"file_path": "config/a.yaml", "line": 2, "value": "1"}, {"file_path": "config/a.yaml", "line": 9, "value": "2"},
                 {"file_path": "config/b.yaml", "line": 1, "value": "3"}]
        text = render_text(success_response({"key": "k", "yaml_total": 3, "yaml_sites": sites}))
        assert text.count("config/a.yaml") == 1 and "  config/a.yaml (2)\n    2  value 1\n    9  value 2\n" in text
        assert "  config/b.yaml\n    1  value 3" in text

    def test_a_single_site_keeps_its_own_block(self):
        from drspec.cli.output import render_text, success_response

        text = render_text(success_response({"key": "k", "yaml_total": 1, "yaml_sites": [{"file_path": "config/a.yaml", "line": 2, "value": "1"}]}))
        assert "  File: config/a.yaml\n  Line: 2\n  Value: 1" in text


CAM_FILES = {
    "config/cam_header.yaml": "%YAML:1.0\n---\nwinSizeMax: 23\nwinSizeStep: 10\n",
    "config/cam_matrix.yaml": (
        "%YAML:1.0\n---\nimage_width: 640\ncamera_matrix: !!opencv-matrix\n   rows: 3\n   cols: 3\n   dt: d\n"
        "   data: [ 500., 0., 320., 0., 500., 240., 0., 0., 1. ]\n"
    ),
    "config/really_bad.yaml": "- name: a\n  port: -\n",
}


@pytest.fixture(scope="module")
def opencv(tmp_path_factory):
    from tests.fakes import KeptDuckDB

    folder = tmp_path_factory.mktemp("opencv")
    with pytest.MonkeyPatch.context() as patch:
        KeptDuckDB().install(patch)
        (folder / "_drspec").mkdir()
        for name, text in CAM_FILES.items():
            (folder / name).parent.mkdir(exist_ok=True)
            (folder / name).write_text(text)
        connection = duckdb.connect(str(folder / "_drspec" / "contracts.db"))  # pattern-ok: duckdb-file kept in memory by `KeptDuckDB`
        init_schema(connection)
        connection.close()
        patch.chdir(folder)
        assert runner.invoke(app, ["--json", "scan", "."]).exit_code == 0
        yield folder


class TestOpenCVFilesAreReadAndTheRestAreNot:
    def test_the_opencv_keys_are_found_with_their_real_lines(self, opencv):
        conn = duckdb.connect(str(opencv / "_drspec" / "contracts.db"), read_only=True)  # pattern-ok: duckdb-file kept in memory by `KeptDuckDB`
        try:
            header = get_strings(conn, "winSizeMax", root=opencv)
            matrix = get_strings(conn, "camera_matrix/data", root=opencv)
        finally:
            conn.close()
        assert header["yaml_sites"] == [{"file_path": "config/cam_header.yaml", "line": 3, "value": "23"}]
        assert matrix["yaml_sites"] == [{"file_path": "config/cam_matrix.yaml", "line": 8, "value": "[500., 0., 320., 0., 500., 240., 0., 0., 1.]"}]

    def test_only_the_really_invalid_file_is_a_problem(self, opencv):
        conn = duckdb.connect(str(opencv / "_drspec" / "contracts.db"), read_only=True)  # pattern-ok: duckdb-file kept in memory by `KeptDuckDB`
        try:
            data = get_strings(conn, "anything", root=opencv, problems=True)
        finally:
            conn.close()
        assert data["problem_files_total"] == 1
        (problem,) = data["problem_files"]
        assert problem["file_path"] == "config/really_bad.yaml" and problem["problem"]["line"] == 2

    def test_the_cli_lists_it_with_its_line_and_message(self, opencv, monkeypatch):
        monkeypatch.chdir(opencv)
        text = runner.invoke(app, ["strings", "get", "anything", "--problems"]).output
        assert "config/really_bad.yaml" in text and "Line: 2" in text and "cam_" not in text.split("Problem Files:")[1]
