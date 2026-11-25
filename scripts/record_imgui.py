"""Records what the C++ compiler says about the five Dear ImGui files, once, so that `corpus_check_cpp.py` needs no compiler.

Run by hand, from the drspec folder, when the ImGui checkout changes:

    python3 scripts/record_imgui.py /path/to/imgui

For each file it runs `g++ -g -c` and `nm -C -l` (the commands the test used to run) and writes
`scripts/corpus/imgui_defined.json`: the names the compiler defines with a body (`defined`), and the subset that is an explicit
definition in the source text (`explicit`), computed with the same regex the test used before.
"""

from __future__ import annotations

import json
import re
import subprocess
import sys
import tempfile
from pathlib import Path

FILES = ["imgui.cpp", "imgui_draw.cpp", "imgui_tables.cpp", "imgui_widgets.cpp", "imgui_demo.cpp"]


def defined_by_compiler(imgui: Path, name: str, workdir: Path) -> set[str]:
    obj = workdir / "x.o"
    done = subprocess.run(["g++", "-g", "-c", str(imgui / name), "-I", str(imgui), "-o", str(obj), "-w"], capture_output=True, text=True, check=False)
    if done.returncode != 0:
        raise SystemExit(done.stderr[:300])
    symbols = subprocess.run(["nm", "-C", "-l", str(obj)], capture_output=True, text=True, check=False).stdout
    defined = set()
    for line in symbols.splitlines():
        match = re.match(r"^[0-9a-f]+ ([TtWw]) (.*?)\t(.*):(\d+)$", line)
        if match and match.group(3).endswith(name):
            base = re.sub(r"<.*?>", "", re.split(r"\(", match.group(2), 1)[0] if "operator()" not in match.group(2) else "operator()").strip()
            defined.add(base.split("::")[-1])
    return defined


def explicit_by_source(text: str, defined: set[str]) -> set[str]:
    """The definitions with a body in the source (the test's filter, as it was: one search per name)."""
    return {
        n for n in defined
        if n and n != "operator()" and re.search(r"(?m)^[^/\n]*\b" + re.escape(n.split()[-1]) + r"\s*\([^;]*?\)\s*(?:const\s*)?(?:noexcept\s*)?(?::[^{;]*)?\{", text)
    }


def main() -> None:
    imgui = Path(sys.argv[1])
    record = {}
    with tempfile.TemporaryDirectory() as workdir:
        for name in FILES:
            defined = defined_by_compiler(imgui, name, Path(workdir))
            explicit = explicit_by_source((imgui / name).read_text(), defined)
            record[name] = {"defined": sorted(defined), "explicit": sorted(explicit)}
            print(name, len(defined), len(explicit))
    out = Path(__file__).parent / "corpus" / "imgui_defined.json"
    out.write_text(json.dumps(record, indent=1) + "\n")
    print("wrote", out)


if __name__ == "__main__":
    main()
