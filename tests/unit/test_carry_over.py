"""A renamed function keeps its contract and history (marked stale); a moved or
rewritten one does not."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Dict, List, Optional

import duckdb
import pytest

from drspec.core.carry_over import (
    CLEAR_MARGIN,
    RENAME_MIN_SIMILARITY,
    Candidate,
    body_tokens,
    carry_over_record,
    pair_functions,
    similarity,
)
from drspec.core.graph_sync import sync_scan_results as _raw_sync_scan_results
from drspec.core.scanner import Scanner
from drspec.db import init_schema
from drspec.db.queries import (
    get_contract,
    insert_contract,
    insert_reasoning_trace,
    insert_vision_finding,
    set_config,
    update_artifact_status,
)
from tests.helpers import fid
from tests.helpers import publish as sync_scan_results

BODY = (
    "def compute_total(items, rate):\n"
    "    total = 0\n"
    "    for item in items:\n"
    "        total += item.price * rate\n"
    "        if total > 1000:\n"
    "            total -= 10\n"
    "    return total\n"
)

OLD = fid("a.py", "compute_total", "items", "rate")


def _write(root: Path, files: Dict[str, Optional[str]]) -> None:
    for relative, content in files.items():
        path = root / relative
        if content is None:
            path.unlink()
            continue
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content)


def _sync(conn, root: Path, paths: List[str], deleted: List[str] = (), raw: bool = False):
    scanner = Scanner()
    functions, imports = [], {}
    for relative in paths:
        result = scanner.scan_file(str(root / relative), relative_to=root)
        assert result and result.parse_status == "successful", relative
        functions.extend(result.functions)
        imports.update(result.file_imports)
    run = _raw_sync_scan_results if raw else sync_scan_results
    return run(conn, functions=functions, scanned_file_paths=set(paths) | set(deleted), file_imports=imports)


@pytest.fixture
def project(tmp_path, monkeypatch):
    root = tmp_path / "project"
    root.mkdir()
    monkeypatch.chdir(root)
    conn = duckdb.connect(":memory:")  # the carried rows are read back through SQL: no file
    init_schema(conn)
    yield root, conn
    conn.close()


def _give_history(conn, function_id: str, confidence: float = 0.9) -> None:
    insert_contract(conn, function_id, json.dumps({"summary": "totals", "invariants": []}), confidence)
    insert_reasoning_trace(conn, function_id, "proposer", json.dumps({"why": "because"}))
    insert_vision_finding(conn, function_id, "outlier", "HIGH", "a spike")
    conn.execute("CREATE SEQUENCE IF NOT EXISTS seq_test_history")
    conn.execute(
        "CREATE TABLE IF NOT EXISTS learning_history (id INTEGER PRIMARY KEY DEFAULT nextval('seq_test_history'), commit_sha TEXT, function_id TEXT)"
    )
    conn.execute("INSERT INTO learning_history (commit_sha, function_id) VALUES ('abc', ?)", [function_id])
    conn.execute("INSERT INTO usage_stats (kind, item_id, related_id, pending, score, as_of) VALUES ('node', ?, '', 0, 7.5, 1)", [function_id])
    update_artifact_status(conn, function_id, "VERIFIED")


def _ids(conn) -> List[str]:
    return sorted(row[0] for row in conn.execute("SELECT function_id FROM artifacts").fetchall())


class TestARenameInsideTheFile:
    def test_a_pure_rename_moves_the_contract_and_everything_attached(self, project):
        root, conn = project
        _write(root, {"a.py": BODY})
        _sync(conn, root, ["a.py"])
        _give_history(conn, fid("a.py", "compute_total", "items", "rate"))

        _write(root, {"a.py": BODY.replace("compute_total", "sum_prices")})
        stats = _sync(conn, root, ["a.py"])

        assert _ids(conn) == [fid("a.py", "sum_prices", "items", "rate")]
        assert stats.carried_over == {fid("a.py", "sum_prices", "items", "rate"): fid("a.py", "compute_total", "items", "rate")}
        new = fid("a.py", "sum_prices", "items", "rate")
        contract = get_contract(conn, new)
        assert json.loads(contract["contract_json"])["summary"] == "totals"
        assert conn.execute("SELECT COUNT(*) FROM reasoning_traces WHERE function_id = ? AND agent = 'proposer'", [new]).fetchone()[0] == 1
        assert conn.execute("SELECT description FROM vision_findings WHERE function_id = ?", [new]).fetchall() == [("a spike",)]
        assert conn.execute("SELECT commit_sha FROM learning_history WHERE function_id = ?", [new]).fetchall() == [("abc",)]
        assert conn.execute("SELECT score FROM usage_stats WHERE kind = 'node' AND item_id = ?", [new]).fetchone()[0] == 7.5
        for table in ("contracts", "reasoning_traces", "vision_findings", "learning_history"):
            assert conn.execute(f"SELECT COUNT(*) FROM {table} WHERE function_id = ?", [OLD]).fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM usage_stats WHERE item_id = ?", [OLD]).fetchone()[0] == 0

    def test_the_new_function_is_stale_with_a_lowered_confidence_and_a_record_of_where_it_came_from(self, project):
        root, conn = project
        _write(root, {"a.py": BODY})
        _sync(conn, root, ["a.py"])
        _give_history(conn, fid("a.py", "compute_total", "items", "rate"), confidence=0.95)
        _write(root, {"a.py": BODY.replace("compute_total", "sum_prices")})
        _sync(conn, root, ["a.py"])

        new = fid("a.py", "sum_prices", "items", "rate")
        assert conn.execute("SELECT status FROM artifacts WHERE function_id = ?", [new]).fetchone()[0] == "STALE"
        assert get_contract(conn, new)["confidence_score"] == pytest.approx(0.69)  # below the default threshold of 70
        record = carry_over_record(conn, new)
        assert record["from"] == fid("a.py", "compute_total", "items", "rate") and record["reason"] == "renamed"
        assert record["original_confidence"] == 95 and record["lowered_confidence"] == 69
        assert conn.execute("SELECT value FROM config WHERE key = ?", ["artifact:stale_reason:" + new]).fetchone()[0] == "RENAMED"
        trace = conn.execute("SELECT trace_json FROM reasoning_traces WHERE function_id = ? AND agent = 'scan'", [new]).fetchone()
        assert json.loads(trace[0])["from"] == fid("a.py", "compute_total", "items", "rate")

    def test_a_confidence_already_below_the_threshold_is_kept(self, project):
        root, conn = project
        _write(root, {"a.py": BODY})
        _sync(conn, root, ["a.py"])
        _give_history(conn, fid("a.py", "compute_total", "items", "rate"), confidence=0.4)
        _write(root, {"a.py": BODY.replace("compute_total", "sum_prices")})
        _sync(conn, root, ["a.py"])
        assert get_contract(conn, fid("a.py", "sum_prices", "items", "rate"))["confidence_score"] == pytest.approx(0.4)

    def test_the_threshold_of_the_project_is_the_one_used(self, project):
        root, conn = project
        set_config(conn, "confidence_threshold", "90")
        _write(root, {"a.py": BODY})
        _sync(conn, root, ["a.py"])
        _give_history(conn, fid("a.py", "compute_total", "items", "rate"), confidence=0.95)
        _write(root, {"a.py": BODY.replace("compute_total", "sum_prices")})
        _sync(conn, root, ["a.py"])
        assert get_contract(conn, fid("a.py", "sum_prices", "items", "rate"))["confidence_score"] == pytest.approx(0.89)

    def test_a_rename_with_a_few_edited_lines_is_carried_over(self, project):
        root, conn = project
        _write(root, {"a.py": BODY})
        _sync(conn, root, ["a.py"])
        _give_history(conn, fid("a.py", "compute_total", "items", "rate"))
        edited = BODY.replace("compute_total", "sum_prices").replace("total -= 10", "total -= 25\n            total = max(total, 0)")
        _write(root, {"a.py": edited})
        stats = _sync(conn, root, ["a.py"])
        assert stats.carried_over == {fid("a.py", "sum_prices", "items", "rate"): fid("a.py", "compute_total", "items", "rate")}

    def test_a_complete_rewrite_is_not_carried_over(self, project):
        root, conn = project
        _write(root, {"a.py": BODY})
        _sync(conn, root, ["a.py"])
        _give_history(conn, fid("a.py", "compute_total", "items", "rate"))
        _write(root, {"a.py": "def other_thing(path):\n    with open(path) as handle:\n        return handle.read()\n"})
        stats = _sync(conn, root, ["a.py"])
        assert stats.carried_over == {}
        assert _ids(conn) == [fid("a.py", "other_thing", "path")]
        assert get_contract(conn, fid("a.py", "other_thing", "path")) is None
        assert conn.execute("SELECT COUNT(*) FROM contracts").fetchone()[0] == 0

    def test_a_function_without_any_history_is_left_alone(self, project):
        root, conn = project
        _write(root, {"a.py": BODY})
        _sync(conn, root, ["a.py"])
        _write(root, {"a.py": BODY.replace("compute_total", "sum_prices")})
        stats = _sync(conn, root, ["a.py"])
        assert stats.carried_over == {}
        assert conn.execute("SELECT status FROM artifacts").fetchone()[0] == "PENDING"


class TestASignatureChange:
    def test_adding_a_parameter_moves_the_contract_to_the_new_identifier(self, project):
        root, conn = project
        _write(root, {"a.py": BODY})
        _sync(conn, root, ["a.py"])
        _give_history(conn, OLD, confidence=0.9)

        _write(root, {"a.py": BODY.replace("(items, rate)", "(items, rate, cap)")})
        stats = _sync(conn, root, ["a.py"])

        new = fid("a.py", "compute_total", "items", "rate", "cap")
        assert stats.carried_over == {new: OLD}
        assert _ids(conn) == [new]
        assert get_contract(conn, new) is not None and get_contract(conn, OLD) is None
        assert conn.execute("SELECT status FROM artifacts WHERE function_id = ?", [new]).fetchone()[0] == "STALE"
        assert get_contract(conn, new)["confidence_score"] == pytest.approx(0.69)
        record = carry_over_record(conn, new)
        assert record["reason"] == "signature_changed" and record["original_confidence"] == 90 and record["from"] == OLD
        assert conn.execute("SELECT value FROM config WHERE key = ?", ["artifact:stale_reason:" + new]).fetchone()[0] == "SIGNATURE_CHANGED"

    def test_changing_only_a_default_does_not_move_anything(self, project):
        root, conn = project
        _write(root, {"a.py": BODY.replace("(items, rate)", "(items, rate=1)")})
        _sync(conn, root, ["a.py"])
        identifier = _ids(conn)[0]
        _give_history(conn, identifier)
        _write(root, {"a.py": BODY.replace("(items, rate)", "(items, rate=2)")})
        stats = _sync(conn, root, ["a.py"])
        assert stats.carried_over == {} and _ids(conn) == [identifier]
        assert carry_over_record(conn, identifier) is None and get_contract(conn, identifier) is not None


class TestCarriedMoreThanOnce:
    def test_the_original_confidence_survives_a_second_move(self, project):
        root, conn = project
        _write(root, {"a.py": BODY})
        _sync(conn, root, ["a.py"])
        _give_history(conn, OLD, confidence=0.9)

        _write(root, {"a.py": BODY.replace("(items, rate)", "(items, rate, cap)")})
        _sync(conn, root, ["a.py"])
        middle = fid("a.py", "compute_total", "items", "rate", "cap")
        _write(root, {"a.py": BODY.replace("(items, rate)", "(items, rate, cap)").replace("compute_total", "sum_prices")})
        stats = _sync(conn, root, ["a.py"])

        last = fid("a.py", "sum_prices", "items", "rate", "cap")
        assert stats.carried_over == {last: middle}
        record = carry_over_record(conn, last)
        assert record["original_confidence"] == 90 and record["from"] == middle and record["earlier"] == [OLD]
        assert get_contract(conn, last)["confidence_score"] == pytest.approx(0.69)

    def test_a_fresh_contract_replaces_the_record_of_what_was_carried(self, project):
        root, conn = project
        _write(root, {"a.py": BODY})
        _sync(conn, root, ["a.py"])
        _give_history(conn, OLD, confidence=0.9)
        _write(root, {"a.py": BODY.replace("compute_total", "sum_prices")})
        _sync(conn, root, ["a.py"])
        new = fid("a.py", "sum_prices", "items", "rate")
        assert carry_over_record(conn, new) is not None
        insert_contract(conn, new, json.dumps({"summary": "checked again"}), 0.85)
        assert carry_over_record(conn, new) is None

        _write(root, {"a.py": BODY.replace("compute_total", "total_of")})
        _sync(conn, root, ["a.py"])
        record = carry_over_record(conn, fid("a.py", "total_of", "items", "rate"))
        assert record["original_confidence"] == 85 and record["earlier"] == []


class TestWhatIsNotPaired:
    def test_a_move_to_another_file_is_not_carried_over_in_one_pass(self, project):
        root, conn = project
        _write(root, {"a.py": BODY, "b.py": "def unrelated():\n    return 1\n"})
        _sync(conn, root, ["a.py", "b.py"])
        _give_history(conn, fid("a.py", "compute_total", "items", "rate"))
        _write(root, {"a.py": "def left_behind():\n    return 0\n", "b.py": "def unrelated():\n    return 1\n\n\n" + BODY})
        stats = _sync(conn, root, ["a.py", "b.py"])
        assert stats.carried_over == {}
        assert get_contract(conn, fid("b.py", "compute_total", "items", "rate")) is None
        assert conn.execute("SELECT COUNT(*) FROM contracts").fetchone()[0] == 0

    def test_a_move_across_two_separate_scans_is_not_carried_over(self, project):
        root, conn = project
        _write(root, {"a.py": BODY, "b.py": "def unrelated():\n    return 1\n"})
        _sync(conn, root, ["a.py", "b.py"])
        _give_history(conn, fid("a.py", "compute_total", "items", "rate"))
        _write(root, {"a.py": "def left_behind():\n    return 0\n"})
        _sync(conn, root, ["a.py"])
        _write(root, {"b.py": "def unrelated():\n    return 1\n\n\n" + BODY})
        stats = _sync(conn, root, ["b.py"])
        assert stats.carried_over == {} and get_contract(conn, fid("b.py", "compute_total", "items", "rate")) is None

    def test_a_function_deleted_and_a_different_one_added_later_are_not_paired(self, project):
        root, conn = project
        _write(root, {"a.py": BODY})
        _sync(conn, root, ["a.py"])
        _give_history(conn, fid("a.py", "compute_total", "items", "rate"))
        _write(root, {"a.py": "def keep():\n    return 1\n"})
        _sync(conn, root, ["a.py"])
        _write(root, {"a.py": "def keep():\n    return 1\n\n\n" + BODY.replace("compute_total", "again")})
        stats = _sync(conn, root, ["a.py"])
        assert stats.carried_over == {}
        assert conn.execute("SELECT COUNT(*) FROM contracts").fetchone()[0] == 0

    def test_two_equally_good_candidates_are_not_paired(self, project):
        root, conn = project
        _write(root, {"a.py": BODY})
        _sync(conn, root, ["a.py"])
        _give_history(conn, fid("a.py", "compute_total", "items", "rate"))
        # two renamed copies: nothing says which one the old function became
        _write(root, {"a.py": BODY.replace("compute_total", "first_copy") + "\n\n" + BODY.replace("compute_total", "second_copy")})
        stats = _sync(conn, root, ["a.py"])
        assert stats.carried_over == {}
        assert conn.execute("SELECT COUNT(*) FROM contracts").fetchone()[0] == 0

    def test_two_near_identical_functions_both_replaced_by_two_new_ones_are_not_paired(self, project):
        root, conn = project
        _write(root, {"a.py": BODY.replace("compute_total", "price_a") + "\n\n" + BODY.replace("compute_total", "price_b")})
        _sync(conn, root, ["a.py"])
        _give_history(conn, fid("a.py", "price_a", "items", "rate"))
        _give_history(conn, fid("a.py", "price_b", "items", "rate"))
        _write(root, {"a.py": BODY.replace("compute_total", "price_c") + "\n\n" + BODY.replace("compute_total", "price_d")})
        stats = _sync(conn, root, ["a.py"])
        assert stats.carried_over == {}
        assert conn.execute("SELECT COUNT(*) FROM contracts").fetchone()[0] == 0

    def test_an_untouched_twin_does_not_stop_the_one_that_was_renamed(self, project):
        root, conn = project
        twin_a = BODY.replace("compute_total", "price_a")
        _write(root, {"a.py": twin_a + "\n\n" + BODY.replace("compute_total", "price_b")})
        _sync(conn, root, ["a.py"])
        _give_history(conn, fid("a.py", "price_a", "items", "rate"))
        _give_history(conn, fid("a.py", "price_b", "items", "rate"))
        _write(root, {"a.py": twin_a + "\n\n" + BODY.replace("compute_total", "price_c")})
        stats = _sync(conn, root, ["a.py"])
        assert stats.carried_over == {fid("a.py", "price_c", "items", "rate"): fid("a.py", "price_b", "items", "rate")}
        assert get_contract(conn, fid("a.py", "price_a", "items", "rate")) is not None


class TestInsideThePublish:
    def test_a_failure_in_the_middle_leaves_the_database_as_it_was(self, project, monkeypatch):
        from drspec.db.staging import publish_transaction

        root, conn = project
        _write(root, {"a.py": BODY})
        _sync(conn, root, ["a.py"])
        _give_history(conn, OLD)

        _write(root, {"a.py": BODY.replace("compute_total", "sum_prices")})

        def explode(*_args, **_kwargs):
            raise RuntimeError("boom")

        monkeypatch.setattr("drspec.core.graph_sync._sync_edges", explode)
        with pytest.raises(RuntimeError):
            with publish_transaction(conn) as write:
                _sync(write, root, ["a.py"], raw=True)

        assert [r[0] for r in conn.execute("SELECT function_id FROM artifacts").fetchall()] == [OLD]
        assert conn.execute("SELECT COUNT(*) FROM contracts WHERE function_id = ?", [OLD]).fetchone()[0] == 1
        assert conn.execute("SELECT COUNT(*) FROM config WHERE key LIKE 'carryover:%'").fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM pending_removals").fetchone()[0] == 0
        conn.close()


class TestUnrelatedFunctionsAreNotTakenForEachOther:
    MIXER = "def mixer(a, b):\n    total = a * b + 3\n    return total / 2\n"
    BLEND = 'def blend_names(first, second):\n    joined = first + " " + second\n    return joined.strip()\n'

    def test_a_rewrite_that_shares_only_the_shape_of_a_function_is_not_a_rename(self, project):
        root, conn = project
        _write(root, {"a.py": self.MIXER})
        _sync(conn, root, ["a.py"])
        _give_history(conn, fid("a.py", "mixer", "a", "b"))
        _write(root, {"a.py": self.BLEND})
        stats = _sync(conn, root, ["a.py"])
        assert stats.carried_over == {}
        assert conn.execute("SELECT COUNT(*) FROM contracts").fetchone()[0] == 0

    def test_keywords_punctuation_and_the_signature_do_not_count(self):
        old = Candidate("a.py::mixer#1", "mixer", self.MIXER, "def mixer(a, b):")
        new = Candidate("a.py::blend_names#2", "blend_names", self.BLEND, "def blend_names(first, second):")
        assert similarity(body_tokens(old), body_tokens(new)) < 0.3
        assert "return" not in body_tokens(old) and "def" not in body_tokens(old) and "mixer" not in body_tokens(old)

    def test_two_trivial_functions_are_not_paired(self):
        gone = [Candidate("a.py::old#1", "old", "def old():\n    return 1\n", "def old():")]
        new = [Candidate("a.py::new#2", "new", "def new():\n    return 1\n", "def new():")]
        assert pair_functions(gone, new) == []

    def test_the_same_qualified_name_is_still_paired_whatever_the_body(self):
        gone = [Candidate("a.py::K.run#11111111", "run", "def run(self):\n    return 1\n", "def run(self):")]
        new = [Candidate("a.py::K.run#22222222", "run", "def run(self, x):\n    return x * 2 + 7\n", "def run(self, x):")]
        assert [p.reason for p in pair_functions(gone, new)] == ["signature_changed"]


class TestThePairingRules:
    def _c(self, function_id: str, name: str, body: str) -> Candidate:
        return Candidate(function_id, name, body)

    def test_the_same_qualified_name_with_other_parameters_is_one_function(self):
        old = self._c("a.py::K.run#11111111", "run", "def run(self, a):\n    return a\n")
        new = self._c("a.py::K.run#22222222", "run", "def run(self, a, b):\n    return a + b + 1000\n")
        pairs = pair_functions([old], [new])
        assert [(p.old_id, p.new_id, p.reason) for p in pairs] == [("a.py::K.run#11111111", "a.py::K.run#22222222", "signature_changed")]

    def test_several_on_each_side_pair_by_the_closest_body(self):
        a = self._c("a.py::add#aaaaaaaa", "add", "def add(a, b):\n    return a + b\n")
        b = self._c("a.py::add#bbbbbbbb", "add", "def add(a, b, c):\n    total = a + b + c\n    log(total)\n    return total * 2\n")
        a2 = self._c("a.py::add#cccccccc", "add", "def add(a, b, c, d):\n    total = a + b + c\n    log(total)\n    return total * 2\n")
        b2 = self._c("a.py::add#dddddddd", "add", "def add(a: int, b: int):\n    return a + b\n")
        pairs = {(p.old_id, p.new_id) for p in pair_functions([a, b], [a2, b2])}
        assert pairs == {("a.py::add#aaaaaaaa", "a.py::add#dddddddd"), ("a.py::add#bbbbbbbb", "a.py::add#cccccccc")}

    def test_the_threshold_and_the_margin_are_what_the_rules_say(self):
        assert RENAME_MIN_SIMILARITY == 0.6 and CLEAR_MARGIN == 0.1
