"""Check the C++ reading of drspec against a real clone of Dear ImGui. Run by hand when the C++ parser changes.

    python3 scripts/corpus_check_cpp.py /path/to/imgui [--skip-sweep]

It prints, for the clone:
  * the share of the functions `g++` defines with a body in five ImGui files that drspec finds, by name (the compiler's answer is
    the recorded one, `scripts/corpus/imgui_defined.json`, made by `scripts/record_imgui.py`; nothing is compiled here), and the
    functions it missed (the lost ones);
  * the functions that swallow the lines of several others, and the functions whose text is not where they say (the same rule as
    `corpus_check_c.py`, over every source file of the clone).
The exit status is 1 when a share is under 95% or a function swallows others or is misplaced.

It is not a test: it reads a clone that is not in this repository and takes a minute. What it guards is pinned, in small, by the unit
tests of `tests/unit/test_cpp_recovery.py` and `tests/unit/test_cpp_conditionals.py`.
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import re
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent / "src"))

RECORDED = HERE / "corpus" / "imgui_defined.json"
IMGUI_FILES = ["imgui.cpp", "imgui_draw.cpp", "imgui_tables.cpp", "imgui_widgets.cpp", "imgui_demo.cpp"]
MINIMUM_SHARE = 0.95

_spec = importlib.util.spec_from_file_location("corpus_check_c", HERE / "corpus_check_c.py")
corpus_check_c = importlib.util.module_from_spec(_spec)
sys.modules["corpus_check_c"] = corpus_check_c
_spec.loader.exec_module(corpus_check_c)  # type: ignore[union-attr]


def explicit_definitions(text: str, defined) -> set[str]:
    """The compiler's names that have an explicit definition with a body in `text`. A definition begins at a line that
    holds the name, so the line starts of the name's occurrences are the only places to try (the same test as one
    search of the whole text per name, at the cost of the occurrences only)."""
    result = set()
    for name in defined:
        if not name or name == "operator()":
            continue
        last = name.split()[-1]
        pattern = re.compile(r"(?m)^[^/\n]*\b" + re.escape(last) + r"\s*\([^;]*?\)\s*(?:const\s*)?(?:noexcept\s*)?(?::[^{;]*)?\{")
        at = text.find(last)
        while at != -1:
            if pattern.match(text, text.rfind("\n", 0, at) + 1):
                result.add(name)
                break
            at = text.find(last, at + 1)
    return result


def explicit_definitions_one_search_per_name(text: str, defined) -> set[str]:
    """The filter as it was written first (one search of the whole text per name); the reference for the indexed one."""
    return {
        n for n in defined
        if n and n != "operator()" and re.search(r"(?m)^[^/\n]*\b" + re.escape(n.split()[-1]) + r"\s*\([^;]*?\)\s*(?:const\s*)?(?:noexcept\s*)?(?::[^{;]*)?\{", text)
    }


def compiler_shares(imgui: Path, recorded: dict) -> dict:
    """For each recorded file: `(share, missed)` of the explicit definitions the compiler recorded that drspec finds."""
    from drspec.parsers.cpp_parser import CppParser

    shares = {}
    for name in IMGUI_FILES:
        text = (imgui / name).read_text()
        explicit = explicit_definitions(text, recorded[name]["defined"])
        found = {f.name.split("::")[-1] for f in CppParser().parse_file(str(imgui / name)).functions if "declaration" not in f.decorators}
        shares[name] = (len(explicit & found) / len(explicit), sorted(explicit - found))
    return shares


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("imgui", type=Path, help="the folder of a Dear ImGui clone")
    parser.add_argument("--skip-sweep", action="store_true", help="only the share of the compiler's functions found")
    arguments = parser.parse_args(argv)
    imgui = arguments.imgui.resolve()
    recorded = json.loads(RECORDED.read_text())
    started = time.perf_counter()
    status = 0
    for name, (share, missed) in compiler_shares(imgui, recorded).items():
        print(f"{name}: compiler's defined functions found {share:.1%} (minimum {MINIMUM_SHARE:.0%}); lost: {len(missed)}")
        for item in missed[:15]:
            print(f"  lost  {item}")
        status = status or int(share < MINIMUM_SHARE)
    if not arguments.skip_sweep:
        result = corpus_check_c.sweep(imgui)
        print(f"functions that swallow others: {len(result['swallowing'])}")
        for item in result["swallowing"][:30]:
            print(f"  swallowing  {item}")
        print(f"functions whose text is not where they say: {len(result['misplaced'])}")
        for item in result["misplaced"][:30]:
            print(f"  misplaced  {item}")
        status = status or int(bool(result["swallowing"] or result["misplaced"]))
    print(f"done in {time.perf_counter() - started:.0f} s")
    return status


if __name__ == "__main__":
    raise SystemExit(main())
