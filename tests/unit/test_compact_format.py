"""The compact form of a tool result (`drspec.core.compact_format`): what the encoder writes for each shape of result, and that nothing is
lost. The decoder that shows it lives in `scripts/bench_output_format.py` (it exists for the tests and the benchmark, not for the
product); in memory, no project, no process."""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pytest

from drspec import mcp_server
from drspec.core import compact_format as compact
from drspec.core.index_notice import NOTE, UNSCANNED_HINT, UNUSED_CAVEAT

SCRIPT = Path(__file__).resolve().parent.parent.parent / "scripts" / "bench_output_format.py"
_spec = importlib.util.spec_from_file_location("bench_output_format", SCRIPT)
bench = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(bench)

pytestmark = pytest.mark.unit


def _record(path: str, name: str, **extra) -> dict:
    return {"function_id": f"{path}::{name}#abcd1234", "function_name": name.split(".")[-1], "file_path": path, "status": "PENDING",
            "signature": f"def {name.split('.')[-1]}(x):", "possibly_stale": False, **extra}


def _search(records: list, **extra) -> dict:
    return {"pattern": "p", "semantic": False, "files_searched": 3, "total_matches": len(records), "matches": records, "truncated": False, **extra}


def _round_trip(payload: dict) -> dict:
    return bench.decode(compact.encode(payload))


class TestRecords:
    PAYLOAD = _search([
        _record("src/drspec/core/a.py", "f"), _record("src/drspec/db/b.py", "g", status="STALE"), _record("src/drspec/core/a.py", "h", possibly_stale=True),
        _record("tests/unit/t.py", "TestX.test_y"),
    ])

    def test_the_payload_comes_back_with_its_files_grouped_as_the_text_format_prints_them(self):
        decoded = _round_trip(self.PAYLOAD)
        assert decoded == bench.canonical(self.PAYLOAD)
        assert [r["function_name"] for r in decoded["matches"]] == ["f", "h", "g", "test_y"]  # a.py's two functions together

    def test_what_the_reader_can_rebuild_is_not_written(self):
        text = compact.encode(self.PAYLOAD)
        assert text.startswith(compact.MARK + "\n")
        assert "PENDING" not in text.replace("status=STALE", "") and "possibly_stale=false" not in text
        assert text.count("src/drspec/core/") == 1  # the directory once for the two files below it
        assert "a.py::" not in text  # the id is written without its file

    def test_a_signature_is_clipped_at_80_characters_with_a_marker_and_only_then(self):
        long = _record("a.py", "f")
        long["signature"] = "def f(" + "x, " * 40 + "):"
        decoded = _round_trip(_search([long, _record("a.py", "g")]))
        assert len(decoded["matches"][0]["signature"]) == 80 and decoded["matches"][0]["signature"].endswith("…")
        assert decoded["matches"][1]["signature"] == "def g(x):"
        assert bench.decode(compact.encode(_search([long]), clip=False))["matches"][0]["signature"] == long["signature"]

    def test_a_function_name_that_is_not_the_last_part_of_its_id_is_kept(self):
        record = _record("a.py", "Outer.inner")
        record["function_name"] = "renamed"
        assert _round_trip(_search([record]))["matches"][0]["function_name"] == "renamed"

    def test_values_that_would_be_read_as_something_else_survive(self):
        record = _record("a.py", "f", relation="123", confidence="true", depth=2)
        record["signature"] = "tab\there\nand a newline"
        decoded = _round_trip(_search([record], note="12", pattern='"quoted"'))
        assert decoded["matches"][0] == compact.clipped(record) and decoded["note"] == "12" and decoded["pattern"] == '"quoted"'

    def test_records_that_lack_a_field_are_grouped_and_come_back_without_it(self):
        sparse = _search([_record("a.py", "f"), {"function_id": "b.py::g#1", "file_path": "b.py"}])  # the second has no status, signature or stale flag
        text = compact.encode(sparse)
        assert not text.startswith(compact.JSON_MARK) and "!st" in text and bench.decode(text) == compact.clipped(sparse)

    def test_a_shape_it_cannot_group_is_written_as_json_and_comes_back(self):
        odd = _search([{"function_id": "a.py::f#1", "file_path": "a.py", "x": 1, "y": 2}, {"function_id": "b.py::g#1", "file_path": "b.py", "y": 2, "x": 1}])  # keys in another order
        text = compact.encode(odd)
        assert text.startswith(compact.JSON_MARK) and bench.decode(text) == compact.clipped(odd)


class TestEachShapeOfResult:
    """One payload of the shape each tool returns: function search, deps_get (callers and callees), unused and status."""

    DEPS = {
        "function_id": "src/a.py::main#11111111", "function_name": "main", "file_path": "src/a.py", "status": "PENDING", "possibly_stale": False, "depth": 2,
        "direction": "both",
        "callees": [{"function_id": "src/a.py::helper#22222222", "function_name": "helper", "file_path": "src/a.py", "depth": 1, "relation": "calls",
                     "confidence": "EXTRACTED", "has_contract": False, "status": "PENDING", "possibly_stale": False},
                    {"function_id": "src/b/c.py::deep#33333333", "function_name": "deep", "file_path": "src/b/c.py", "depth": 2, "relation": "references",
                     "confidence": "INFERRED", "has_contract": True, "status": "VERIFIED", "possibly_stale": True}],
        "callers": [],
    }
    UNUSED = {
        "total_count": 2, "returned": 2, "limit": 100, "path_filter": None, "caveat": UNUSED_CAVEAT,
        "functions": [{"function_id": "src/a.py::old#44444444", "function_name": "old", "file_path": "src/a.py", "status": "PENDING", "possibly_stale": False, "only_test_callers": False},
                      {"function_id": "src/a.py::older#55555555", "function_name": "older", "file_path": "src/a.py", "status": "PENDING", "possibly_stale": False, "only_test_callers": True}],
    }
    STATUS = {"artifacts": {"total": 5, "by_status": {"pending": 5, "stale": 0}}, "edges": {"calls": {"total": 3}}, "summary": {"completion_rate": 0.0}}

    @pytest.mark.parametrize("payload", [DEPS, UNUSED, STATUS], ids=["deps_get", "unused", "status"])
    def test_it_decodes_to_the_payload(self, payload):
        assert _round_trip(payload) == bench.canonical(payload)

    def test_deps_get_lists_each_side_grouped_by_file(self):
        text = compact.encode(self.DEPS)
        assert "# callees function_id function_name file_path depth relation confidence has_contract status possibly_stale" in text
        assert "  helper#22222222\td=1" in text  # the defaults of relation, confidence, has_contract, status and possibly_stale are not written
        assert "callers: []" in text

    def test_the_long_caveat_is_a_code(self):
        text = compact.encode(self.UNUSED)
        assert UNUSED_CAVEAT not in text and "caveat: ~2" in text and "path_filter: null" in text

    def test_the_hint_of_a_line_search_is_a_code(self):
        payload = {"pattern": "x", "total_hits": 0, "unscanned_files": {"count": 1, "examples": ["a.c"], "hint": UNSCANNED_HINT}}
        text = compact.encode(payload)
        assert UNSCANNED_HINT not in text and "~1" in text and _round_trip(payload) == payload


class TestHitsAndBlocks:
    HITS = {"pattern": "x", "semantic": False, "files_searched": 2, "total_hits": 3, "truncated": False, "hits": [
        {"file_path": "src/a.py", "line": 3, "text": "    x = 1  # tab\there", "function_id": "src/a.py::f#11111111"},
        {"file_path": "src/a.py", "line": 9, "text": "x", "function_id": "src/a.py::f#11111111"},
        {"file_path": "src/b.py", "line": 1, "text": "@1/ x", "function_id": None},
    ]}
    BLOCKS = {"pattern": "x", "total_hits": 1, "blocks": [
        {"file_path": "src/a.py", "start_line": 4, "end_line": 6, "matched_lines": [5], "functions": ["src/a.py::f#11111111", "src/other.py::g#22222222"],
         "context": [{"line": 4, "text": ""}, {"line": 5, "text": "  x = '@1/ src/drspec/core/'  "}, {"line": 6, "text": "-:>"}]},
    ]}

    def test_hits_come_back_exactly(self):
        assert _round_trip(self.HITS) == self.HITS

    def test_a_function_that_repeats_under_a_file_is_written_once(self):
        assert compact.encode(self.HITS).count("f#11111111") == 1

    def test_blocks_come_back_exactly_and_their_text_is_never_altered(self):
        text = compact.encode(self.BLOCKS)
        assert _round_trip(self.BLOCKS) == self.BLOCKS
        assert ":  x = '@1/ src/drspec/core/'  " in text.split("\n")  # the matched line, byte for byte, one character in front

    def test_a_function_of_another_file_is_written_whole(self):
        assert "!src/other.py::g#22222222" in compact.encode(self.BLOCKS)


class TestWhoReadsAndWritesAMember:
    """`refs_get`: owners, then their files, then their functions, then each access."""

    PAYLOAD = {
        "symbol": "Item::count", "member": "count", "kind": "any", "total_accesses": 3, "returned": 3, "truncated": False,
        "owners": [
            {"owner_class": "src/item.h::Item", "accesses": 3, "files": [
                {"file_path": "src/item.cpp", "functions": [
                    {"function_id": "src/item.cpp::Item::add#aaaaaaaa", "function_name": "Item::add", "accesses": [
                        {"kind": "write", "line": 10, "site": "this->count", "source": "count += n;"},
                        {"kind": "read", "line": 11, "site": "count", "source": "return count;", "before": [{"line": 9, "text": "int x;"}], "after": [{"line": 12, "text": "}"}]}]},
                ]},
                {"file_path": "src/ui/view.cpp", "functions": [
                    {"function_id": "src/ui/view.cpp::draw#bbbbbbbb", "function_name": "draw", "accesses": [{"kind": "read", "line": 3, "site": "item.count", "source": "use(item.count);"}]},
                ]},
            ]},
        ],
        "unresolved": {"count": 0, "functions": [], "functions_total": 0},
    }

    def test_it_comes_back_exactly(self):
        assert _round_trip(self.PAYLOAD) == self.PAYLOAD

    def test_it_is_grouped_owner_then_file_then_function_with_each_path_written_once(self):
        text = compact.encode(self.PAYLOAD)
        assert text.count("src/item.cpp") == 0 and " item.cpp" in text and text.split("\n").count(">src/") == 1 and ">src/ui/" in text
        assert "*src/item.h::Item\t3" in text and "  Item::add#aaaaaaaa" in text and "   10\twrite\tthis->count\tcount += n;" in text
        assert len(text) < len(json.dumps(self.PAYLOAD))

    def test_a_list_of_owners_of_another_shape_is_a_table_and_comes_back(self):
        odd = {"owners": [{"owner_class": "A", "extra": 1}, {"owner_class": "B", "extra": 2}]}
        assert "= owners owner_class extra" in compact.encode(odd).split("\n") and bench.decode(compact.encode(odd)) == odd


class TestTheNote:
    def test_the_note_that_follows_from_the_index_is_a_code(self):
        payload = _search([_record("a.py", "f")], index={"pending": 4, "total": 281}, note=NOTE.format(pending=4, total=281))
        assert "note: ~I" in compact.encode(payload) and _round_trip(payload) == compact.clipped(payload)

    def test_another_note_is_kept(self):
        payload = _search([_record("a.py", "f")], index={"pending": 4, "total": 281}, note="Showing 1 of 2")
        assert _round_trip(payload)["note"] == "Showing 1 of 2"

    def test_a_value_that_looks_like_a_code_makes_the_result_json(self):
        payload = {"pattern": "~I", "n": 1}
        assert compact.encode(payload).startswith("~J") and bench.decode(compact.encode(payload)) == payload


class TestTheLegend:
    def test_it_names_the_marker_the_function_id_rule_and_the_defaults(self):
        text = compact.legend()
        assert compact.MARK in text and compact.JSON_MARK in text
        assert "THE FUNCTION ID" in text and "name#hash" in text and "src/a/b.py::name#hash" in text
        assert 'status="PENDING"' in text and "s=signature" in text and "source_get" in text

    def test_every_marker_the_encoder_writes_is_explained(self):
        text = compact.legend()
        markers = [compact.MARK, compact.JSON_MARK, compact.NOTE_CODE, *compact.CONSTANTS, compact.CLIP_MARK, "# key", ">dir/", "name#hash", "^ key",
                   "% key", "& key", "= key", "|cell", "`~` a missing field", "{ key", "!k", "f[a,b]", "*Owner", "@a-b", "!id", "`=`", "`:`", "`-`"]
        assert [marker for marker in markers if marker not in text] == []

    def test_the_id_rule_is_the_one_the_encoder_uses(self):
        payload = _search([_record("src/drspec/core/a.py", "f")])
        text = compact.encode(payload)
        assert ">src/drspec/core/" in text and " a.py" in text and "  f#abcd1234" in text  # file of the group + name#hash
        assert bench.function_id_of("f#abcd1234", "src/drspec/core/a.py") == payload["matches"][0]["function_id"]


def _sites(count: int = 39) -> list:
    folders = ["scripts", "src/drspec/cli/commands", "src/drspec/core", "tests/unit"]
    return [{"function_id": f"{folders[i % 4]}/m{i % 7}.py::{'Test.' if i % 4 == 3 else ''}function_number_{i}#{i:08x}", "file_path": f"{folders[i % 4]}/m{i % 7}.py",
             "call": f'text{i}.encode("utf-8")', "reason": "defined in several places", "line": 20 + i * 7} for i in range(count)]


def _fix_sites(sites: list) -> list:
    return sorted(sites, key=lambda site: (site["file_path"], site["line"]))


DEPS_UNRESOLVED = {
    "function_id": "src/a.py::encode#2f19f4cd", "function_name": "encode", "file_path": "src/a.py", "status": "PENDING", "possibly_stale": False, "depth": 1, "direction": "both",
    "callees": [{"function_id": "src/a.py::_encode#4530d954", "function_name": "_encode", "file_path": "src/a.py", "depth": 1, "relation": "calls", "has_contract": False,
                 "status": "PENDING", "possibly_stale": False}],
    "callers": [],
    "unresolved_callers": {"name": "encode", "call_sites": 39, "functions": 39, "message": "no resolved callers; 39 call sites in 39 functions name encode but could not be resolved",
                           "sites": _fix_sites(_sites())},
}
REFS_UNRESOLVED = {
    "symbol": "Item::count", "member": "count", "kind": "any", "total_accesses": 0, "returned": 0, "truncated": False, "owners": [],
    "unresolved": {"count": 6, "functions_total": 2, "note": "6 access(es) to a member named 'count' in 2 function(s) were left unresolved.",
                   "functions": [{"function_id": f"src/ui/view{i}.cpp::draw{i}#0000000{i}", "function_name": f"draw{i}", "file_path": f"src/ui/view{i}.cpp",
                                  "lines": [{"line": 10 + j, "kind": "read", "site": "item.count", "receiver": "item"} for j in range(3)]} for i in range(2)]},
}


#: `refs_get` of a base of write chains: the base is a read, and the mark says what is written through it, on its access and only there.
WRITTEN_THROUGH = {
    "symbol": "Ctx::avoid", "member": "avoid", "kind": "any", "total_accesses": 3, "returned": 3, "truncated": False,
    "owners": [{"owner_class": "Ctx", "accesses": 3, "files": [
        {"file_path": "src/writer.cpp", "functions": [
            {"function_id": "src/writer.cpp::writer#aaaaaaaa", "function_name": "writer", "accesses": [
                {"kind": "read", "line": 4, "site": "c.avoid", "source": "c.avoid.n = 1;", "written_through": "n"},
                {"kind": "read", "line": 5, "site": "c.avoid", "source": "c.avoid.n += 2;", "written_through": "n"}]}]},
        {"file_path": "src/reader.cpp", "functions": [
            {"function_id": "src/reader.cpp::reader#bbbbbbbb", "function_name": "reader", "accesses": [
                {"kind": "read", "line": 8, "site": "c.avoid", "source": "return c.avoid.n;"}]}]}]}],
    "unresolved": {"count": 0, "functions": [], "functions_total": 0},
}


@pytest.mark.unit
class TestTheWriteThroughMarkIsKept:
    def test_it_comes_back_exactly_and_only_the_marked_base_has_the_field(self):
        assert _round_trip(WRITTEN_THROUGH) == WRITTEN_THROUGH
        written = compact.encode(WRITTEN_THROUGH)
        assert written.startswith("~B")  # not the JSON fallback
        assert written.count('{"written_through":"n"}') == 2 and written.count("written_through") == 2  # only on the two marked accesses


@pytest.mark.unit
class TestNestedListsAreGrouped:
    """A list of records inside a field (the unresolved call sites of `deps_get`, the unresolved accesses of `refs_get`) is grouped like the lists at the top."""

    def test_the_unresolved_call_sites_of_a_deps_get_are_shorter_than_in_the_text_form_and_come_back(self):
        text = mcp_server._render_tool_result(DEPS_UNRESOLVED, None)
        written = compact.encode(DEPS_UNRESOLVED)
        assert len(written) < len(text) and len(written) < len(json.dumps(DEPS_UNRESOLVED)) / 2
        assert bench.decode(written) == DEPS_UNRESOLVED
        assert "{ unresolved_callers" in written.split("\n") and written.count("defined in several places") == 1  # the reason once, not once per site

    def test_a_field_that_is_the_same_in_every_record_is_written_once(self):
        written = compact.encode(DEPS_UNRESOLVED)
        header = next(line for line in written.split("\n") if line.startswith("# sites"))
        assert header.endswith("\treason=defined in several places") and ("\tcall=" not in header)

    def test_the_records_under_a_record_follow_it_as_rows(self):
        written = compact.encode(REFS_UNRESOLVED)
        assert "{ unresolved" in written.split("\n") and "# functions function_id function_name file_path lines[line,kind,site,receiver]" in written
        assert "   10\tread\titem.count\titem" in written.split("\n")  # a row of three spaces and the cells under its function
        assert bench.decode(written) == REFS_UNRESOLVED

    def test_a_record_with_an_empty_list_under_it_comes_back_with_the_empty_list(self):
        payload = {"x": 1, "functions": [{"function_id": "a.py::f#1", "file_path": "a.py", "lines": []}, {"function_id": "a.py::g#2", "file_path": "a.py", "lines": [{"n": 1}]}]}
        assert bench.decode(compact.encode(payload)) == payload

    def test_a_list_of_records_that_are_not_functions_is_a_table_with_a_marker_for_a_missing_field(self):
        rows = [{"event": "pass", "ms": 12.5, "note": "~starts with a tilde"}, {"event": "rescan", "ms": 3}, {"ms": 4, "event": "request", "sql": "SELECT 1"}]
        written = compact.encode({"slowest": rows})
        assert "= slowest event ms note sql" in written and "|rescan\t3\t~\t~" in written.split("\n")
        assert bench.decode(written) == {"slowest": rows}

    def test_a_value_that_is_the_same_in_all_rows_of_a_table_is_written_once_and_a_row_that_lacks_it_blocks_that(self):
        same = [{"kind": "k", "n": 1}, {"kind": "k", "n": 2}]
        assert "= rows kind n\tkind=k" in compact.encode({"rows": same}) and bench.decode(compact.encode({"rows": same})) == {"rows": same}
        lacking = [{"kind": "k", "n": 1}, {"n": 2}]
        assert "\tkind=" not in compact.encode({"rows": lacking}) and bench.decode(compact.encode({"rows": lacking})) == {"rows": lacking}


def _settings(count: int = 12) -> list:
    return [{"key": f"worker.setting_{i}", "value": i * 10, "default": i * 10, "type": "int", "description": f"What setting {i} does, in a sentence of some length."} for i in range(count)]


# One payload of the shape each tool returns; `grouped` marks the shapes that hold lists, which the compact form must write shorter on its own
# (the others are small, and `format: "compact"` sends the plain text when that is shorter).
SHAPES = {
    "search, functions": (_search([_record(f"src/drspec/core/m{i % 5}.py", f"function_{i}") for i in range(30)]), True),
    "search, hits": ({"pattern": "x", "semantic": False, "files_searched": 9, "total_hits": 12, "truncated": False, "hits": [
        {"file_path": f"src/m{i % 3}.py", "line": i * 4, "text": f"    value = compute({i})", "function_id": f"src/m{i % 3}.py::run#0000{i % 3}000"} for i in range(12)]}, True),
    "search, blocks": (TestHitsAndBlocks.BLOCKS, False),
    "deps_get": (TestEachShapeOfResult.DEPS, True),
    "deps_get, unresolved call sites": (DEPS_UNRESOLVED, True),
    "unused": (TestEachShapeOfResult.UNUSED, True),
    "status": (TestEachShapeOfResult.STATUS, False),
    "refs_get": (TestWhoReadsAndWritesAMember.PAYLOAD, True),
    "refs_get, written through": (WRITTEN_THROUGH, True),
    "refs_get, unresolved by file": ({
        "symbol": "Context::counter", "member": "counter", "kind": "any", "total_accesses": 1, "returned": 1, "truncated": False, "owners": [],
        "unresolved": {"count": 4, "functions_total": 4, "files": [{"file_path": "m.cpp", "accesses": 3, "functions": 3}, {"file_path": "other.cpp", "accesses": 1, "functions": 1}],
                       "note": "4 access(es) to a member named 'counter' in 4 function(s) were left unresolved: the receiver's type is not known, so they may read or write this member. Counted by file under `files`; `unresolved: full` lists each one."},
    }, True),
    "refs_get, unresolved accesses": (REFS_UNRESOLVED, True),
    "strings_get, leaves under a prefix": ({
        "key": "avoidance/", "sub_keys_total": 3, "leaves_total": 30, "code_files_read": 2,
        "leaves": [{"key": f"avoidance/group_{i}/value", "value": f"{i}.5", "file_path": f"config/part_{i % 6}.yaml", "line": i + 2,
                    "read_file": "src/node.cpp" if i % 3 else None, "read_function": "Node::init" if i % 3 else None, "read_line": 2 if i % 3 else None} for i in range(30)],
        "leaves_more": 0, "problem_files_total": 1,
        "problem_note": "1 yaml file(s) could not be read (not parsed, too large, or PyYAML missing): their keys are not indexed. `drspec strings get --problems` lists them.",
    }, False),  # not "grouped": the text groups the yaml file and the reader once per run, and compact (unchanged) repeats them on each row, so on a listing whose leaves share long reader paths the text is shorter; the MCP format sends the shorter one (T-193 note)
    "strings_get, yaml and code sites": ({
        "key": "avoidance/timeout", "config": {"files": 2, "keys": 4, "files_with_problems": 1, "files_skipped": 0}, "code_files_read": 2,
        "yaml_total": 2, "yaml_sites": [{"file_path": f"config/p{i}.yaml", "line": i + 2, "value": "5.0"} for i in range(2)],
        "code_total": 3, "code_sites": [{"function_id": f"src/node{i}.cpp::Node::init#0000000{i}", "function_name": "Node::init", "file_path": f"src/node{i}.cpp", "line": 2, "source": "void Node::init() { nh.getParam(\"avoidance/timeout\", timeout_); }"} for i in range(2)]
                       + [{"function_id": None, "function_name": "(module level)", "file_path": "scripts/tool.py", "line": 1, "source": "limit = rospy.get_param(\"avoidance/timeout\")"}],
        "problem_files": [{"file_path": "config/broken.yaml", "problem": {"line": 3, "message": "did not find expected ',' or ']'"}, "skipped": None}],
        "problem_note": "1 yaml file(s) did not parse or were not read: their keys are not indexed. Fix the syntax (or make the file smaller) and scan again.",
    }, True),
    "config_get, all settings": ({"settings": _settings(), "count": 12}, True),
    "config_get, one": ({"key": "worker.poll_interval", "value": 60.0}, False),
    "config_set": ({"key": "worker.poll_interval", "value": 30.0, "previous": 60.0}, False),
    "contract_list": ({"total": 3, "contracts": [{"function_id": f"src/a.py::f{i}#0000000{i}", "status": "VERIFIED", "confidence": 0.9, "updated_at": "2026-11-20T10:00:00"} for i in range(3)]}, True),
    "contract_get": ({"function_id": "src/a.py::f#1", "status": "VERIFIED", "contract": {"invariants": [{"id": "i1", "text": "x > 0", "kind": "pre"}, {"id": "i2", "text": "y", "kind": "post"}]}}, False),
    "contract_save": ({"function_id": "src/a.py::f#1", "status": "NEEDS_REVIEW", "saved": True}, False),
    "learn_history": ({"commits": [{"sha": f"abc{i}", "message": "fix", "files": i, "date": "2026-11-20"} for i in range(5)]}, True),
    "learn_stats": ({"patterns": 3, "commits": 12, "last": "2026-11-20"}, False),
    "trace_summary": ({"summary": [{"event": "request", "count": 9, "total_ms": 12.5}, {"event": "pass", "count": 2, "total_ms": 9.0, "peak_rss_mb": 120}],
                       "slowest": [{"t": "2026-11-21T10:00:00", "event": "request", "ms": 500.0, "op": "remediate"}, {"t": "2026-11-21T10:00:01", "event": "pass", "ms": 90.0, "batch": 200}]}, True),
    "vision_list": ({"findings": [{"id": i, "category": "gap", "severity": "low", "text": f"Finding number {i}", "status": "open"} for i in range(4)]}, True),
    "scan": ({"scan_id": "ab12", "queued": 3, "up_to_date": 10, "files": [{"file_path": f"src/f{i}.py", "state": "done", "functions": i} for i in range(3)]}, True),
    "worker_status": ({"running": True, "pid": 123, "staleness_queue_size": 0, "backlog": {"pending": 0, "retry": 0}, "embedding": {"enabled": True, "state": "idle"}}, False),
    "worker_stop": ({"stopped": True, "pid": 123}, False),
    "verify_run": ({"function_id": "src/a.py::f#1", "passed": False, "checks": [{"invariant": "i1", "passed": False, "message": "x was 0"}, {"invariant": "i2", "passed": True, "message": ""}]}, True),
    "ack": ({"identifier": "x", "acknowledged": True}, False),
    "init": ({"initialized": True, "path": "_drspec", "ide": ["claude-code"]}, False),
    "deps_plot": ({"function_id": "src/a.py::f#1", "path": "_drspec/plots/f.html", "nodes": 3}, False),
}


@pytest.mark.unit
class TestNeverLongerThanTheTextForm:
    """The guard: for a payload of each shape the tools return, `format: "compact"` is not longer than `text`. The lists the compact form groups
    must be shorter on their own (not only because a short result is sent as text), so a shape that falls back to one JSON line fails here."""

    @pytest.mark.parametrize("name", list(SHAPES))
    def test_compact_is_not_longer_than_text(self, name):
        payload, grouped = SHAPES[name]
        text = mcp_server._render_tool_result(payload, None)
        assert len(mcp_server._render_tool_result(payload, "compact")) <= len(text)
        if grouped:
            assert len(compact.encode(payload)) < len(text), name

    @pytest.mark.parametrize("name", list(SHAPES))
    def test_what_is_written_comes_back(self, name):
        payload, _ = SHAPES[name]
        assert bench.decode(compact.encode(payload)) == bench.canonical(payload)

    def test_no_list_of_records_is_left_as_a_line_of_json_with_the_keys_repeated(self):
        import re

        for name, (payload, _grouped) in SHAPES.items():
            for line in compact.encode(payload).split("\n"):
                if re.match(r"^[A-Za-z_]+: \[", line):
                    assert '{"' not in line, f"{name}: {line[:80]}"
