"""A scan that carries over many contracts: the summary the person or the assistant reads lists the first twenty, with
the total. The stored stats keep every pair, because batches are merged from them."""

from __future__ import annotations

from drspec.cli.output import render_text, success_response
from drspec.core.graph_sync import SyncStats
from drspec.core.scan_service import new_run, stats_dict, summarize


def _summary_of(carried: dict) -> dict:
    run = new_run("x", 1)
    run["stats"] = stats_dict(SyncStats(carried_over=carried, functions_new=len(carried)))
    run["functions_found"] = len(carried)
    return {"path": ".", **summarize({"queued": 1, "scan_id": "x"}, run, "done")}


def test_three_hundred_carried_over_contracts_show_twenty_and_the_total():
    carried = {f"new{i}.py::f": f"old{i}.py::f" for i in range(300)}
    data = _summary_of(carried)
    assert data["carried_over"]["total"] == 300
    assert len(data["carried_over"]["examples"]) == 20
    assert list(data["carried_over"]["examples"])[:3] == ["new0.py::f", "new1.py::f", "new2.py::f"]


def test_the_text_for_three_hundred_carried_over_contracts_stays_short():
    # measured: 61 lines with the first twenty listed (the whole summary, not only the carried-over part); the unbounded
    # text was about 340 lines
    text = render_text(success_response(_summary_of({f"new{i}.py::f": f"old{i}.py::f" for i in range(300)})))
    assert len(text.splitlines()) < 70


def test_a_few_carried_over_contracts_are_all_listed():
    data = _summary_of({"a.py::f": "b.py::f", "c.py::g": "d.py::g"})
    assert data["carried_over"]["total"] == 2 and len(data["carried_over"]["examples"]) == 2
