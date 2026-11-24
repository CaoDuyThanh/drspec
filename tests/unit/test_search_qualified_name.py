"""`search` also matches the qualified name of a function (`Class::method`, `ns::Class::method`: the form the ids, `refs get` and `deps get` print), so a
class name finds its inline methods; a function found through that name alone is marked `matched_in: qualified_name`. A line search is unchanged.
In memory: one small project scanned once for the module (no `init`, no process)."""

from __future__ import annotations

import json

import duckdb
import pytest
from typer.testing import CliRunner

from drspec import mcp_server
from drspec.cli.app import app
from drspec.db import init_schema

runner = CliRunner()

PRODUCER_CPP = """#include <memory>
struct Context { double counter = 0.0; };
class Producer {
public:
    void clear(const std::shared_ptr<Context>& context) { context->counter += 1.0; }
    int size() const { return 3; }
};
int main_loop() { return 0; }
"""
ZMP_CPP = """namespace zmp {
class Widget {
public:
    void draw(int x) { paint(x); }
};
void helper(int v) { use(v); }
}
"""


@pytest.fixture(scope="module")
def project(tmp_path_factory):
    from tests.fakes import KeptDuckDB

    folder = tmp_path_factory.mktemp("qualified")
    with pytest.MonkeyPatch.context() as patch:
        KeptDuckDB().install(patch)
        (folder / "_drspec").mkdir()
        (folder / "src").mkdir()
        (folder / "src" / "producer.cpp").write_text(PRODUCER_CPP)
        (folder / "src" / "zmp.cpp").write_text(ZMP_CPP)
        connection = duckdb.connect(str(folder / "_drspec" / "contracts.db"))  # pattern-ok: duckdb-file kept in memory by `KeptDuckDB`
        init_schema(connection)
        connection.close()
        patch.chdir(folder)
        assert runner.invoke(app, ["--json", "scan", "."]).exit_code == 0
        yield folder


def _search(*args):
    result = runner.invoke(app, ["--json", "search", *args])
    assert result.exit_code == 0, result.output
    return json.loads(result.output)["data"]


def _names(data):
    return [match["function_id"].split("::", 1)[1].rsplit("#", 1)[0] for match in data["matches"]]


class TestAQualifiedNameIsFound:
    def test_the_bare_name_finds_the_method_as_before(self, project):
        data = _search("clear")
        assert _names(data) == ["Producer::clear"] and "matched_in" not in data["matches"][0]

    def test_the_qualified_name_finds_the_method_plain_and_regex(self, project):
        for args in (["Producer::clear"], ["--regex", "Producer::clear"], ["--regex", "^Producer::clear$"], ["-E", "Producer::cl.ar"]):
            data = _search(*args)
            assert _names(data) == ["Producer::clear"], args
            assert data["matches"][0]["matched_in"] == "qualified_name"

    def test_a_class_name_finds_its_inline_methods(self, project):
        data = _search("Producer")
        assert sorted(_names(data)) == ["Producer::clear", "Producer::size"] and data["total_matches"] == 2
        assert {match["matched_in"] for match in data["matches"]} == {"qualified_name"}

    def test_a_function_is_listed_once_when_the_name_and_the_qualified_name_both_match(self, project):
        data = _search("size")
        assert _names(data) == ["Producer::size"] and "matched_in" not in data["matches"][0]
        both = _search("Producer::size")
        assert _names(both) == ["Producer::size"] and both["total_matches"] == 1

    def test_the_marker_is_only_for_a_function_the_qualified_name_alone_found(self, project):
        data = _search("Context")  # the signature of `clear` holds it, so `clear` is not marked; the struct has no methods
        assert [("matched_in" in match) for match in data["matches"]] == [False]

    def test_ignore_case(self, project):
        assert _names(_search("-i", "producer::CLEAR")) == ["Producer::clear"]
        assert _names(_search("producer::CLEAR")) == []

    def test_a_namespace_in_the_qualified_name(self, project):
        assert _names(_search("zmp::Widget::draw")) == ["zmp::Widget::draw"]
        assert _names(_search("Widget::draw")) == ["zmp::Widget::draw"]
        assert _names(_search("--regex", "^zmp::Widget::")) == ["zmp::Widget::draw"]

    def test_a_free_function_in_a_namespace(self, project):
        data = _search("zmp::helper")
        assert _names(data) == ["zmp::helper"] and data["matches"][0]["matched_in"] == "qualified_name"

    def test_a_namespace_finds_everything_in_it(self, project):
        assert sorted(_names(_search("zmp"))) == ["zmp::Widget::draw", "zmp::helper"]

    def test_a_free_function_with_no_owner_is_its_own_name(self, project):
        data = _search("main_loop")
        assert _names(data) == ["main_loop"] and "matched_in" not in data["matches"][0]

    def test_the_exact_qualified_name_comes_first(self, project):
        names = _names(_search("--regex", "Producer::(clear|size)|Widget"))
        assert set(names) == {"Producer::clear", "Producer::size", "zmp::Widget::draw"}
        assert _names(_search("Producer::size"))[0] == "Producer::size"

    def test_the_count_and_the_file_list_follow_the_same_rule(self, project):
        assert _search("-c", "Producer")["count"] == 2
        assert _search("-l", "Producer::clear")["files"] == ["src/producer.cpp"]

    def test_invert_match_leaves_out_what_the_qualified_name_matches(self, project):
        names = _names(_search("-v", "Producer::clear"))
        assert "Producer::clear" not in names and "Producer::size" in names

    def test_the_mcp_tool_matches_and_marks_it_too(self, project):
        db_path = project / "_drspec" / "contracts.db"
        found, failed = mcp_server._tool_search(db_path, {"pattern": "Producer::clear"})
        assert not failed and [m["function_name"] for m in found["matches"]] == ["clear"] and found["matches"][0]["matched_in"] == "qualified_name"
        assert [m["function_name"] for m in mcp_server._tool_search(db_path, {"pattern": "Producer", "regex": True})[0]["matches"]] == ["clear", "size"]

    def test_a_line_search_is_unchanged(self, project):
        none = _search("Producer::clear", "--lines")
        assert none["hits"] == []  # (the text has no `Producer::clear`: the files are read as they are, not matched by qualified name)
        found = _search("Producer", "--lines")
        assert found["hits"] and "matched_in" not in json.dumps(found)
