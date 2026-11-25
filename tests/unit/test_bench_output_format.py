"""The prototype of scripts/bench_output_format.py: the encoders and decoders of the shorter result formats, in process, on hand-made payloads
(no project, no index, no transcripts)."""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parent.parent.parent / "scripts" / "bench_output_format.py"
_spec = importlib.util.spec_from_file_location("bench_output_format", SCRIPT)
bench = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(bench)

pytestmark = pytest.mark.unit

LONG_NAME = "TestAVeryLongClassName.test_a_name_that_goes_on_and_on_past_forty"


def _record(path: str, name: str, **extra) -> dict:
    return {"function_id": f"{path}::{name}#abcd1234", "function_name": name.split(".")[-1], "file_path": path, "status": "PENDING",
            "signature": f"def {name.split('.')[-1]}(x):", "possibly_stale": False, **extra}


def _search(records: list, **extra) -> dict:
    return {"pattern": "p", "semantic": False, "files_searched": 3, "total_matches": len(records), "matches": records, "truncated": False, **extra}


def _round_trip(payload: dict, aliases: bool = False) -> dict:
    return bench.decode(bench.encode(payload, aliases=aliases))


class TestAliasesInsideOneResult:
    PAYLOAD = _search([_record("src/drspec/core/rust/x.py", LONG_NAME), _record("src/drspec/core/rust/y.py", LONG_NAME)] + [
        _record(f"src/drspec/core/{folder}/z.py", "g") for folder in ("cpp", "js", "py", "go")
    ])

    def test_a_directory_prefix_and_a_long_name_that_repeat_get_an_alias_and_come_back(self):
        text = bench.encode(self.PAYLOAD, aliases=True)
        assert text.split("\n")[1].startswith("~A ") and "@1/" in text
        assert text.count(LONG_NAME) == 1  # in the legend only
        assert _round_trip(self.PAYLOAD, aliases=True) == bench.canonical(self.PAYLOAD)

    def test_a_result_with_nothing_that_repeats_has_no_legend(self):
        text = bench.encode(_search([_record("a.py", "f")]), aliases=True)
        assert "~A" not in text

    def test_the_printed_id_is_turned_back_into_the_id_the_tools_take(self):
        assert bench.function_id_of("f#abcd1234", "src/a.py") == "src/a.py::f#abcd1234"
        assert bench.function_id_of("@2#abcd1234", "src/a.py", {"@2": LONG_NAME}) == f"src/a.py::{LONG_NAME}#abcd1234"


class TestTheSharedDictionary:
    TEXTS = [f"File: src/drspec/core/graph_sync.py\n  ID: _sync_edges#{number:08d}\n@property x\nSrc: src/drspec/core/graph_sync.py" for number in range(120)]

    def test_it_is_rebuilt_every_50_results_from_the_previous_100_and_its_legend_is_sent_then(self):
        sent = [position for position, (_text, legend, _entries) in enumerate(bench.shared_dictionary(self.TEXTS)) if legend]
        assert sent == [50, 100]
        assert len(bench.shared_dictionary(self.TEXTS)[110][2]) <= 30

    def test_the_text_expands_back_to_what_it_was_even_with_an_at_sign_in_it(self):
        for text, _legend, entries in bench.shared_dictionary(self.TEXTS)[-3:]:
            assert entries
            assert bench.expand_dictionary(text, entries) in self.TEXTS and len(text) < len(self.TEXTS[0])

    def test_a_result_before_the_first_dictionary_is_sent_as_it_is(self):
        text, legend, entries = bench.shared_dictionary(self.TEXTS)[7]
        assert text == self.TEXTS[7] and legend == "" and entries == []


class TestTheHarness:
    def test_the_queries_of_the_four_tools_are_taken_from_a_transcript_and_the_others_left(self, tmp_path):
        lines = [
            {"message": {"content": [{"type": "tool_use", "id": "1", "name": "mcp__drspec__search", "input": {"pattern": "x"}}]}},
            {"message": {"content": [{"type": "tool_result", "tool_use_id": "1", "content": "result"}]}},
            {"message": {"content": [{"type": "tool_use", "id": "2", "name": "mcp__drspec__source_get", "input": {"function_id": "a::b"}}]}},
            {"message": {"content": [{"type": "tool_result", "tool_use_id": "2", "content": "body"}]}},
            {"message": {"content": [{"type": "tool_use", "id": "3", "name": "Bash", "input": {}}]}},
        ]
        (tmp_path / "s.jsonl").write_text("\n".join(json.dumps(line) for line in lines) + "\nnot json\n")
        assert bench.extract_queries(str(tmp_path / "*.jsonl")) == [("search", {"pattern": "x"})]

    def test_every_format_of_a_result_round_trips_and_a_broken_one_is_reported(self):
        from drspec import mcp_server

        payload = _search([_record("src/a.py", "f"), _record("src/b.py", "g", possibly_stale=True)])
        results = [{"tool": "search", "mode": "functions", "payload": payload, "text": mcp_server._render_tool_result(payload, None)}]
        rendered = bench.render_all(results)
        assert bench.check_lossless(results, rendered) == []
        rendered[0]["B"] = rendered[0]["B"].replace("\n b.py", "\n c.py")  # (a.py and b.py are under one `>src/` line)
        assert bench.check_lossless(results, rendered) != []
        table = bench.table(results, bench.render_all(results))
        assert "search functions" in table and "tokenizer:" in table

    def test_the_questions_have_answers_and_every_result_they_name_is_in_the_file(self):
        document = json.loads(bench.QUESTIONS_FILE.read_text())
        assert len(document["questions"]) == 20
        shown = {result["result"] for result in document["results"]}
        assert all(question["result"] in shown and question["answer"] not in (None, "") for question in document["questions"])
        assert all({"A", "B", "C", "D"} <= set(result["formats"]) for result in document["results"])
