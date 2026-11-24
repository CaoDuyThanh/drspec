"""Records what the C compiler says about raylib's own sources, once, so that `corpus_check_c.py` needs no compiler.

Run by hand, from the drspec folder, when the raylib checkout changes:

    python3 scripts/record_raylib.py /path/to/raylib

For each `.c` file of `src` and `examples` (outside `external/`) it runs `gcc -g -c` and `nm -C -l` (the commands
the tests used to run) and writes `scripts/corpus/raylib_defined.json`: for each folder, the files that compiled on this
machine (`files`, relative to raylib) and the `(file name, function name)` pairs gcc defines in them (`defined`).
"""

from __future__ import annotations

import json
import re
import subprocess
import sys
import tempfile
from pathlib import Path


def record_folder(raylib: Path, folder: str, workdir: Path) -> dict:
    files, defined = [], set()
    for source in sorted((raylib / folder).glob("**/*.c")):
        if "external" in source.parts:
            continue
        obj = workdir / "x.o"
        compiled = subprocess.run(
            ["gcc", "-g", "-c", "-w", str(source), "-I", str(raylib / "src"), "-I", str(raylib / "src" / "external"), "-I", str(source.parent), "-o", str(obj)],
            capture_output=True, text=True, check=False,
        )
        if compiled.returncode != 0:
            continue  # needs a platform's own headers
        files.append(str(source.relative_to(raylib)))
        for line in subprocess.run(["nm", "-C", "-l", str(obj)], capture_output=True, text=True, check=False).stdout.splitlines():
            match = re.match(r"^[0-9a-f]+ ([Tt]) (.*?)\t(.*):(\d+)$", line)
            if match and Path(match.group(3)).name == source.name:
                defined.add((source.name, match.group(2).split("(")[0].strip().split()[-1]))
    return {"files": files, "defined": sorted(defined)}


def main() -> None:
    raylib = Path(sys.argv[1])
    record = {}
    with tempfile.TemporaryDirectory() as workdir:
        for folder in ("src", "examples"):
            record[folder] = record_folder(raylib, folder, Path(workdir))
            print(folder, len(record[folder]["files"]), "files compiled,", len(record[folder]["defined"]), "functions defined")
    out = Path(__file__).parent / "corpus" / "raylib_defined.json"
    out.write_text(json.dumps(record, indent=1) + "\n")
    print("wrote", out)


if __name__ == "__main__":
    main()
