"""Where a Rust crate starts, and which file each `mod name;` declaration means.

Crates come from `Cargo.toml` (the package, its `[lib]` and `[[bin]]` paths, workspace members) when the
project folder is known, and from the usual file names (`src/lib.rs`, `src/main.rs`, `src/bin/*.rs`)
otherwise. A file no crate reaches is a root of its own, so what it names inside itself still resolves.
"""

from __future__ import annotations

import posixpath
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Set

_MAX_MEMBERS = 200


@dataclass
class Crate:
    name: str
    roots: List[str] = field(default_factory=list)
    """Project-relative root files: the library's first, then the binaries'."""
    folder: str = ""


def parse_manifest(text: str) -> Dict[str, Dict[str, object]]:
    """The small part of TOML a manifest needs: `[section]` / `[[section]]` headers with `key = "string"` and
    `key = ["a", "b"]` lines. Returns {section: {key: value}}; repeated `[[bin]]` sections are `bin#0`, `bin#1`."""
    sections: Dict[str, Dict[str, object]] = {"": {}}
    current = ""
    counts: Dict[str, int] = {}
    lines = iter(text.splitlines())
    for raw in lines:
        line = raw.split("#", 1)[0].strip() if '"' not in raw else raw.strip()
        if not line:
            continue
        header = re.match(r"^\[\[\s*([^\]]+?)\s*\]\]$", line)
        if header:
            name = header.group(1)
            counts[name] = counts.get(name, -1) + 1
            current = f"{name}#{counts[name]}"
            sections.setdefault(current, {})
            continue
        header = re.match(r"^\[\s*([^\]]+?)\s*\]$", line)
        if header:
            current = header.group(1)
            sections.setdefault(current, {})
            continue
        pair = re.match(r"^([A-Za-z0-9_\-\.\"]+)\s*=\s*(.*)$", line)
        if not pair:
            continue
        key, value = pair.group(1).strip('"'), pair.group(2).strip()
        if value.startswith("["):
            while "]" not in value:
                value += " " + next(lines, "]").split("#", 1)[0].strip()
            sections[current][key] = re.findall(r'"([^"]*)"', value)
        else:
            quoted = re.match(r'^"([^"]*)"', value)
            sections[current][key] = quoted.group(1) if quoted else value
    return sections


def _read(path: Path) -> Dict[str, Dict[str, object]]:
    try:
        return parse_manifest(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError):
        return {}


def _crate_from_manifest(folder: str, manifest: Dict[str, Dict[str, object]], known: Set[str]) -> Optional[Crate]:
    package = manifest.get("package", {})
    name = package.get("name")
    if not isinstance(name, str):
        return None
    name = name.replace("-", "_")
    crate = Crate(name=name, folder=folder)
    library = manifest.get("lib", {})
    lib_path = posixpath.normpath(posixpath.join(folder, str(library.get("path", "src/lib.rs"))))
    if lib_path in known:
        crate.roots.append(lib_path)
    bins = [section for key, section in manifest.items() if key.startswith("bin#")]
    for path in [posixpath.normpath(posixpath.join(folder, str(b["path"]))) for b in bins if "path" in b] + [posixpath.join(folder, "src/main.rs")]:
        if path in known and path not in crate.roots:
            crate.roots.append(path)
    prefix = posixpath.join(folder, "src/bin") + "/"
    for path in sorted(known):
        if path.startswith(prefix) and path.endswith(".rs") and path.count("/") == prefix.count("/") and path not in crate.roots:
            crate.roots.append(path)
    return crate if crate.roots else None


def discover_crates(root: Optional[Path], known_files: Iterable[str]) -> List[Crate]:
    """Every crate of the project with its root files."""
    known = set(known_files)
    crates: List[Crate] = []
    seen_folders: Set[str] = set()

    def add(folder: str) -> None:
        if folder in seen_folders or root is None:
            return
        seen_folders.add(folder)
        manifest = _read(root / folder / "Cargo.toml" if folder else root / "Cargo.toml")
        if not manifest:
            return
        crate = _crate_from_manifest(folder, manifest, known)
        if crate is not None:
            crates.append(crate)
        members = manifest.get("workspace", {}).get("members")
        if isinstance(members, list):
            count = 0
            for pattern in members:
                for match in sorted((root / folder).glob(str(pattern))):
                    count += 1
                    if count > _MAX_MEMBERS:
                        return
                    if match.is_dir() and (match / "Cargo.toml").is_file():
                        add(match.relative_to(root).as_posix())

    add("")
    if crates:
        return crates
    # No manifest: the usual roots, wherever they are.
    for path in sorted(known):
        base = posixpath.basename(path)
        if base in ("lib.rs", "main.rs") and posixpath.basename(posixpath.dirname(path)) == "src":
            folder = posixpath.dirname(posixpath.dirname(path))
            name = (posixpath.basename(folder) or "crate").replace("-", "_")
            existing = next((c for c in crates if c.folder == folder), None)
            if existing is None:
                existing = Crate(name=name, folder=folder)
                crates.append(existing)
            (existing.roots.insert(0, path) if base == "lib.rs" else existing.roots.append(path))
    return crates


def child_file(declaring_file: str, inline_path: List[str], name: str, explicit: Optional[str], is_root: bool, known: Set[str]) -> Optional[str]:
    """The file that `mod name;` means when it is written in `declaring_file`, inside the inline modules `inline_path`."""
    folder = posixpath.dirname(declaring_file)
    stem = posixpath.splitext(posixpath.basename(declaring_file))[0]
    if explicit:
        candidate = posixpath.normpath(posixpath.join(folder, *inline_path, explicit))
        return candidate if candidate in known else None
    if not (is_root or stem in ("mod", "lib", "main")):
        folder = posixpath.join(folder, stem)
    base = posixpath.join(folder, *inline_path, name)
    for candidate in (base + ".rs", posixpath.join(base, "mod.rs")):
        if candidate in known:
            return candidate
    return None
