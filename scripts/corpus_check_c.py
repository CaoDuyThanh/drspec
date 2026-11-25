"""Check the C reading of drspec against a real clone of raylib. Run by hand when the C or C++ parser changes.

    python3 scripts/corpus_check_c.py /path/to/raylib [--skip-sweep]

It prints, for the clone:
  * the share of the functions `gcc` defines in raylib's `src` and `examples` that drspec finds (the compiler's answer is the
    recorded one, `scripts/corpus/raylib_defined.json`, made by `scripts/record_raylib.py`; nothing is compiled here), and the
    functions it missed (the lost ones);
  * the functions that swallow the lines of several others (more than 3 others' starts, over 300 lines);
  * the functions whose text does not start on the line they say;
  * the functions each file skipped by name.
The exit status is 1 when the share is under 99% or a function swallows others or is misplaced.

It is not a test: it reads a clone that is not in this repository and takes a minute or two (`miniaudio.h` alone is twenty
seconds). What it guards is pinned, in small, by the unit tests of `tests/unit/test_c_support.py`,
`tests/unit/test_cpp_conditionals.py` and `tests/unit/test_cpp_recovery.py` (the committed excerpts under `tests/fixtures/`).
"""

from __future__ import annotations

import argparse
import bisect
import functools
import json
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

RECORDED = Path(__file__).resolve().parent / "corpus" / "raylib_defined.json"
MINIMUM_SHARE = 0.99
SOURCE_SUFFIXES = (".c", ".h", ".cpp", ".hpp", ".cc")


def swallowing_and_misplaced(functions, lines):
    """`(swallowing, misplaced)`: the functions that hold the lines of more than three others while being over 300 lines long,
    and the functions whose text does not start on the line they say (it may start up to three lines earlier, with the macros
    written in front of it on lines of their own). `functions` are anything with `name`, `start_line`, `end_line` and `body`."""
    swallowing, misplaced = [], []
    starts = sorted(f.start_line for f in functions)
    for function in functions:
        inner = bisect.bisect_right(starts, function.end_line) - bisect.bisect_right(starts, function.start_line)
        if inner > 3 and function.end_line - function.start_line > 300:
            swallowing.append((function.name, inner))
        first = function.body.split("\n", 1)[0].strip()
        window = " ".join(lines[max(0, function.start_line - 4) : function.start_line])
        if first and first not in window:
            misplaced.append((function.name, function.start_line))
    return swallowing, misplaced


@functools.lru_cache(maxsize=None)
def scanned(path: str, root: str):
    """A file scanned once for the whole run (the comparison and the sweep read the same files)."""
    from drspec.core.scanner import Scanner

    return Scanner().scan_file(path, relative_to=Path(root))


def compiler_share(raylib: Path, recorded: dict) -> tuple:
    """`(share, missed)` over `src` and `examples`: the recorded `(file name, function)` pairs gcc defines, and which drspec found."""
    defined, found = set(), set()
    for folder in ("src", "examples"):
        for pair in recorded[folder]["defined"]:
            defined.add(tuple(pair))
        for name in recorded[folder]["files"]:
            found |= {(Path(name).name, f.name) for f in scanned(str(raylib / name), str(raylib)).functions}
    return len(defined & found) / len(defined), sorted(defined - found)


def corpus_files(root: Path):
    return sorted(p for p in root.rglob("*") if p.suffix in SOURCE_SUFFIXES and ".git" not in p.parts)


def sweep(root: Path) -> dict:
    """Swallowing, misplaced and skipped functions over every source file of the clone."""
    swallowing, misplaced, skipped = [], [], {}
    for path in corpus_files(root):
        result = scanned(str(path), str(root))
        relative = str(path.relative_to(root))
        found_swallowing, found_misplaced = swallowing_and_misplaced(result.functions, path.read_text(errors="replace").split("\n"))
        swallowing += [(relative, *item) for item in found_swallowing]
        misplaced += [(relative, *item) for item in found_misplaced]
        if result.skipped_functions:
            skipped[relative] = [entry["name"] for entry in result.skipped_functions]
    return {"swallowing": swallowing, "misplaced": misplaced, "skipped": skipped}


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("raylib", type=Path, help="the folder of a raylib clone")
    parser.add_argument("--skip-sweep", action="store_true", help="only the share of the compiler's functions found")
    arguments = parser.parse_args(argv)
    raylib = arguments.raylib.resolve()
    recorded = json.loads(RECORDED.read_text())
    started = time.perf_counter()
    share, missed = compiler_share(raylib, recorded)
    print(f"gcc's defined functions found: {share:.1%} (minimum {MINIMUM_SHARE:.0%}); lost: {len(missed)}")
    for pair in missed[:30]:
        print(f"  lost  {pair[0]}  {pair[1]}")
    status = 0 if share >= MINIMUM_SHARE else 1
    if not arguments.skip_sweep:
        result = sweep(raylib)
        print(f"functions that swallow others: {len(result['swallowing'])}")
        for item in result["swallowing"][:30]:
            print(f"  swallowing  {item}")
        print(f"functions whose text is not where they say: {len(result['misplaced'])}")
        for item in result["misplaced"][:30]:
            print(f"  misplaced  {item}")
        print(f"files with functions skipped by name: {len(result['skipped'])} ({sum(map(len, result['skipped'].values()))} functions)")
        for name, names in sorted(result["skipped"].items(), key=lambda pair: -len(pair[1]))[:10]:
            print(f"  skipped  {name}: {len(names)} (for example {names[0]})")
        status = status or int(bool(result["swallowing"] or result["misplaced"]))
    print(f"done in {time.perf_counter() - started:.0f} s")
    return status


if __name__ == "__main__":
    raise SystemExit(main())
