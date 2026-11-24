"""Which project file a JavaScript or TypeScript import names.

An import path is resolved against the files the project has indexed, never against the disk
once the configuration is read:

* a relative path (`./math`, `../lib/util.js`) is tried as written, with each source extension,
  and as a folder with an `index` file; an ESM-style `./x.js` that is really `x.ts` is accepted;
* a path matched by `tsconfig.json` / `jsconfig.json` `compilerOptions.paths`, or reachable from
  its `baseUrl`;
* the name of a package in the project (the root `package.json` and the workspaces it lists),
  with its `main`, `module` or `exports` entry, and a sub-path inside it;
* anything else is a package from `node_modules`: not indexed, so it is left unresolved rather
  than guessed.
"""

from __future__ import annotations

import json
import posixpath
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional, Set, Tuple

SOURCE_SUFFIXES = (".ts", ".tsx", ".js", ".jsx", ".mjs", ".cjs")
_RUNTIME_SUFFIXES = {".js": (".ts", ".tsx", ".jsx"), ".mjs": (".mts",), ".cjs": (".cts",), ".jsx": (".tsx",)}
_MAX_WORKSPACES = 200


@dataclass
class JsConfig:
    base_url: str = ""
    """Project-relative folder `baseUrl` points at ('' for the root); `None`-like when unset is `unset`."""
    has_base_url: bool = False
    paths: List[Tuple[str, List[str]]] = field(default_factory=list)
    """`(pattern, [targets])` with `*` as the only wildcard, targets relative to `base_url`."""
    packages: Dict[str, str] = field(default_factory=dict)
    """Package name -> project-relative folder."""
    entries: Dict[str, str] = field(default_factory=dict)
    """Package folder -> project-relative entry file as written (`main`, `module` or `exports["."]`)."""
    exports: Dict[str, Dict[str, str]] = field(default_factory=dict)
    """Package folder -> {sub-path: project-relative file as written}."""


def _read_json(path: Path) -> dict:
    """A JSON file that may carry comments and trailing commas (tsconfig does); `{}` when it cannot be read."""
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return {}
    text = re.sub(r'("(?:\\.|[^"\\])*")|//[^\n]*|/\*.*?\*/', lambda m: m.group(1) or "", text, flags=re.S)
    text = re.sub(r",(\s*[}\]])", r"\1", text)
    try:
        value = json.loads(text)
    except ValueError:
        return {}
    return value if isinstance(value, dict) else {}


def _exports_targets(value: object) -> Dict[str, str]:
    """`package.json` `exports` as {sub-path: file}; conditions are tried in the order types-last, import, require, default."""
    def pick(entry: object) -> Optional[str]:
        if isinstance(entry, str):
            return entry
        if isinstance(entry, dict):
            for condition in ("import", "module", "require", "node", "default"):
                if condition in entry:
                    found = pick(entry[condition])
                    if found:
                        return found
        if isinstance(entry, list):
            for item in entry:
                found = pick(item)
                if found:
                    return found
        return None

    if isinstance(value, str):
        return {".": value}
    if isinstance(value, dict):
        if any(key.startswith(".") for key in value):
            return {key: found for key, entry in value.items() if key.startswith(".") and (found := pick(entry))}
        found = pick(value)
        return {".": found} if found else {}
    return {}


def load_config(root: Path) -> JsConfig:
    """Read the project's module configuration. A missing or unreadable file means 'no setting', never an error."""
    config = JsConfig()
    for name in ("tsconfig.json", "jsconfig.json"):
        options = _read_json(root / name).get("compilerOptions")
        if not isinstance(options, dict):
            continue
        if isinstance(options.get("baseUrl"), str):
            config.has_base_url = True
            config.base_url = posixpath.normpath(options["baseUrl"]).lstrip("./") if options["baseUrl"] not in (".", "./") else ""
        if isinstance(options.get("paths"), dict):
            for pattern, targets in options["paths"].items():
                if isinstance(targets, list):
                    config.paths.append((pattern, [t for t in targets if isinstance(t, str)]))
        break

    def add_package(folder: str) -> None:
        manifest = _read_json(root / folder / "package.json") if folder else _read_json(root / "package.json")
        name = manifest.get("name")
        if not isinstance(name, str):
            return
        config.packages[name] = folder
        entry = manifest.get("module") if isinstance(manifest.get("module"), str) else manifest.get("main")
        if isinstance(entry, str):
            config.entries[folder] = posixpath.normpath(posixpath.join(folder, entry))
        exported = _exports_targets(manifest.get("exports"))
        if exported:
            config.exports[folder] = {key: posixpath.normpath(posixpath.join(folder, target)) for key, target in exported.items() if "*" not in key}

    add_package("")
    workspaces = _read_json(root / "package.json").get("workspaces")
    if isinstance(workspaces, dict):
        workspaces = workspaces.get("packages")
    if isinstance(workspaces, list):
        count = 0
        for pattern in workspaces:
            if not isinstance(pattern, str) or pattern.startswith("!"):
                continue
            for match in sorted(root.glob(pattern)):
                if match.is_dir() and (match / "package.json").is_file():
                    count += 1
                    if count > _MAX_WORKSPACES:
                        return config
                    add_package(match.relative_to(root).as_posix())
    return config


def _candidates(base: str) -> List[str]:
    """The files a path without certainty could be: as written, with each suffix, or a folder index."""
    out = [base]
    suffix = posixpath.splitext(base)[1]
    stem = base[: -len(suffix)] if suffix else base
    if suffix in _RUNTIME_SUFFIXES:
        out.extend(stem + other for other in _RUNTIME_SUFFIXES[suffix])
    out.extend(base + extension for extension in SOURCE_SUFFIXES)
    out.extend(posixpath.join(base, "index" + extension) for extension in SOURCE_SUFFIXES)
    return out


def _first_known(base: str, known: Set[str]) -> Optional[str]:
    for candidate in _candidates(posixpath.normpath(base)):
        if candidate in known:
            return candidate
    return None


def _match_pattern(pattern: str, spec: str) -> Optional[str]:
    """The text a `*` in `pattern` stands for, or `''` for an exact pattern; `None` when it does not match."""
    if "*" not in pattern:
        return "" if pattern == spec else None
    head, _, tail = pattern.partition("*")
    if spec.startswith(head) and spec.endswith(tail) and len(spec) >= len(head) + len(tail):
        return spec[len(head): len(spec) - len(tail)] if tail else spec[len(head):]
    return None


def resolve_module(from_file: str, spec: str, known: Set[str], config: Optional[JsConfig] = None) -> Optional[str]:
    """The project file that `import ... from "spec"` (or `require("spec")`) in `from_file` means, or `None`."""
    if not spec:
        return None
    config = config or JsConfig()
    if spec.startswith("."):
        return _first_known(posixpath.join(posixpath.dirname(from_file), spec), known)
    if spec.startswith("/"):
        return _first_known(spec.lstrip("/"), known)
    for pattern, targets in config.paths:
        star = _match_pattern(pattern, spec)
        if star is None:
            continue
        for target in targets:
            found = _first_known(posixpath.join(config.base_url, target.replace("*", star)), known)
            if found:
                return found
    if config.has_base_url:
        found = _first_known(posixpath.join(config.base_url, spec), known)
        if found:
            return found
    # A package of the project, by name, optionally with a sub-path.
    for name in sorted(config.packages, key=len, reverse=True):
        if spec == name or spec.startswith(name + "/"):
            folder = config.packages[name]
            subpath = "." + spec[len(name):]
            exported = config.exports.get(folder, {})
            if subpath in exported:
                found = _first_known(exported[subpath], known)
                if found:
                    return found
            if subpath == ".":
                entry = config.entries.get(folder)
                for candidate in ([entry] if entry else []) + [posixpath.join(folder, "src", "index"), posixpath.join(folder, "index")]:
                    found = _first_known(candidate, known)
                    if found:
                        return found
            else:
                for base in (posixpath.join(folder, subpath[2:]), posixpath.join(folder, "src", subpath[2:])):
                    found = _first_known(base, known)
                    if found:
                        return found
            return None
    return None


def is_external(spec: str, resolved: Optional[str], config: Optional[JsConfig] = None) -> bool:
    """Whether an unresolved import names a package outside the project, as opposed to a project file that is missing."""
    if resolved is not None or not spec:
        return False
    return not spec.startswith((".", "/"))
