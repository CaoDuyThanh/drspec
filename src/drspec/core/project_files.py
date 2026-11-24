"""Which files of a project drspec reads: code and documents, found the way git sees the project.

Inside a git work tree the list is what git knows about (`git ls-files --cached --others --exclude-standard`, so
nested and global ignore files count), narrowed by the scanner's own rules (supported suffix, the built-in
ignore names such as `node_modules`) and by two settings: `scan.ignore` (folders to skip although git tracks
them) and `scan.include` (folders to read although git ignores them). Outside git, the built-in rules alone apply,
as they always did. The number of files skipped because of `.gitignore` is reported, so a missing file is not silent.
"""

from __future__ import annotations

import os
import subprocess
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import Any, Dict, Iterable, Iterator, List, Optional, Sequence, Set, Tuple

from drspec.core.scanner import Scanner

STATE_DIR = "_drspec"
_GIT_TIMEOUT_SECONDS = 60


@dataclass
class ProjectFiles:
    files: List[str] = field(default_factory=list)
    """Project-relative paths (forward slashes) of code and document files, sorted."""
    ignored: Set[str] = field(default_factory=set)
    """Readable files left out because git ignores them or `scan.ignore` names their folder."""
    skipped_by_gitignore: int = 0
    generated: Dict[str, str] = field(default_factory=dict)
    """Generated folders left out because they carry a marker of a build's output (folder -> kind, for example
    `devel` -> `catkin`). Not read unless `scan.include` names them."""
    used_git: bool = False
    rules: Any = None
    """The scanner's rules the listing was made with: `rules.wanted(path)` says whether a path is something drspec reads."""

    def left_the_project(self, root: Path, path: str) -> bool:
        """A path that was indexed and is not in the list: whether that is because it is gone or now ignored (it
        is), and not because the listing missed it."""
        if not (root / path).exists() or path in self.ignored or _under(path, self.generated):
            return True  # a generated folder is not walked, so its files are judged by their folder
        return self.rules is not None and not self.rules.wanted(path)


def folders_of(setting: Any) -> Tuple[str, ...]:
    """The folders a comma-separated setting names, as clean relative prefixes."""
    parts = []
    for raw in str(setting or "").split(","):
        clean = raw.strip().strip("/")
        if clean.startswith("./"):
            clean = clean[2:]
        if clean:
            parts.append(clean)
    return tuple(parts)


def _under(path: str, folders: Iterable[str]) -> bool:
    return any(path == folder or path.startswith(folder + "/") for folder in folders)


def _git_lines(root: Path, *arguments: str) -> Optional[List[str]]:
    try:
        done = subprocess.run(
            ["git", "-C", str(root), *arguments], stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
            timeout=_GIT_TIMEOUT_SECONDS, check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if done.returncode != 0:
        return None
    return [os.fsdecode(part) for part in done.stdout.split(b"\0") if part]


def inside_git(root: Path) -> bool:
    lines = _git_lines(root, "rev-parse", "--is-inside-work-tree")
    return bool(lines) and lines[0].strip() == "true"


def _raise(error: OSError) -> None:
    """A folder that cannot be read must stop the listing: skipped silently, its files would look removed."""
    raise error


def generated_kind(folder: Path) -> Optional[str]:
    """What build wrote this folder, or None when it holds no marker of a generated tree: a CMake build tree
    (`CMakeCache.txt`), or a catkin workspace space such as `devel/` and `install/` (a `.catkin` file, or
    `_setup_util.py` with `setup.sh`). The name of the folder does not count: a real `install/` is read."""
    if (folder / "CMakeCache.txt").is_file():
        return "CMake build tree"
    if (folder / ".catkin").is_file():
        return "catkin"
    if (folder / "_setup_util.py").is_file() and (folder / "setup.sh").is_file():
        return "catkin"
    return None


class _Generated:
    """Which folders of one listing are generated trees left out by default, with the answer for each folder
    remembered. A folder named in `scan.include` (or inside one) is always read."""

    def __init__(self, root: Path, include: Tuple[str, ...], found: Dict[str, str]) -> None:
        self.root = root
        self.include = include
        self.found = found  # the generated folders met so far: folder -> kind
        self.kinds: Dict[str, Optional[str]] = {}

    def excluded_folder(self, folder: str) -> Optional[str]:
        """The kind of a generated folder that is left out, or None."""
        if not folder or _under(folder, self.include):
            return None
        if folder not in self.kinds:
            self.kinds[folder] = generated_kind(self.root / folder)
        kind = self.kinds[folder]
        if kind:
            self.found[folder] = kind
        return kind

    def excludes_file(self, relative: str) -> bool:
        """Whether a file lies in a generated folder that is left out (`scan.include` wins over the rule)."""
        if _under(relative, self.include):
            return False
        parts = relative.split("/")[:-1]
        return any(self.excluded_folder("/".join(parts[:depth])) for depth in range(1, len(parts) + 1))


class _Rules:
    """The scanner's rules for one listing, with the answer for each folder remembered: a listing of a
    project git ignores a virtual environment in names tens of thousands of files in a few hundred folders."""

    def __init__(self, scanner: Scanner, document_suffixes: Tuple[str, ...], config_suffixes: Tuple[str, ...] = ()) -> None:
        from drspec.core.scanner import LANGUAGE_MAP

        self.scanner = scanner
        self.suffixes = tuple(LANGUAGE_MAP) + tuple(document_suffixes) + tuple(config_suffixes)
        self.folder_ignored: Dict[str, bool] = {}

    def wanted(self, relative: str, prefix: str = "") -> bool:
        """Whether the scanner would read this path at all: a supported suffix or a document, outside the
        built-in ignored names (judged on the part after `prefix`, the folder a setting asked for) and
        drspec's own folder."""
        if not relative.lower().endswith(self.suffixes):
            return False
        if relative == STATE_DIR or relative.startswith(STATE_DIR + "/"):
            return False
        judged = relative[len(prefix) :].lstrip("/") if prefix else relative
        folder, _, name = judged.rpartition("/")
        if folder:
            blocked = self.folder_ignored.get(folder)
            if blocked is None:
                blocked = self.folder_ignored[folder] = self.scanner.should_ignore(Path(folder))
            if blocked:
                return False
        return not self.scanner.should_ignore(Path(name))


def _walk(root: Path, folder: str, rules: _Rules) -> Iterator[str]:
    """Every wanted file under `root/folder`, by walking the disk (for the folders `scan.include` names)."""
    base = root / folder
    for directory, subfolders, names in os.walk(base, onerror=_raise):
        relative_dir = PurePosixPath(Path(directory).relative_to(root)).as_posix()
        subfolders[:] = sorted(
            sub for sub in subfolders
            if not rules.scanner.should_ignore(Path(PurePosixPath(relative_dir[len(folder) :].lstrip("/")) / sub))
        )
        for name in sorted(names):
            relative = f"{relative_dir}/{name}"
            if rules.wanted(relative, folder):
                yield relative


def _config_suffixes(settings: Dict[str, Any]) -> Tuple[str, ...]:
    """The YAML config files are read unless `strings.enabled` is off (then they are not listed, and the scan removes them)."""
    from drspec.core.config_keys import CONFIG_SUFFIXES

    return CONFIG_SUFFIXES if settings.get("strings.enabled", True) else ()


def list_project_files(
    root: Path,
    scanner: Optional[Scanner] = None,
    document_suffixes: Tuple[str, ...] = (),
    settings: Optional[Dict[str, Any]] = None,
    count_ignored: bool = True,
    only: Optional[Sequence[str]] = None,
) -> ProjectFiles:
    """The code and document files of the project at `root`, sorted, with what was left out (listing the ignored
    files costs about as much as listing the others, so a caller that does not report them can skip it).

    With `only`, the files and folders (relative paths) a scan was asked about: only what is under them is listed, so a
    scan of one file does not look at the other thousands."""
    scanner = scanner or Scanner()
    settings = settings or {}
    rules = _Rules(scanner, document_suffixes, _config_suffixes(settings))
    ignore = folders_of(settings.get("scan.ignore"))
    include = folders_of(settings.get("scan.include"))
    result = ProjectFiles()
    generated = _Generated(root, include, result.generated)

    specs = ["--", *(f":(literal){path}" for path in only)] if only else []
    tracked = _git_lines(root, "ls-files", "--cached", "--others", "--exclude-standard", "-z", *specs) if inside_git(root) else None
    found: Set[str] = set()
    if tracked is None:
        if only:
            for path in only:
                if (root / path).is_dir():
                    found.update(_walk_all(root, rules, path, generated))
                elif rules.wanted(path) and (root / path).is_file():
                    found.add(path)
        else:
            found.update(_walk_all(root, rules, "", generated))
    else:
        result.used_git = True
        for relative in tracked:
            relative = relative.replace(os.sep, "/")
            if rules.wanted(relative) and (root / relative).is_file():
                found.add(relative)
        ignored_by_git = (_git_lines(root, "ls-files", "--others", "--ignored", "--exclude-standard", "-z", *specs) or []) if count_ignored else []
        for relative in ignored_by_git:
            relative = relative.replace(os.sep, "/")
            if rules.wanted(relative) and not _under(relative, include):
                result.ignored.add(relative)
                generated.excludes_file(relative)  # git already ignores it, but a generated folder is named all the same
        result.skipped_by_gitignore = len(result.ignored)
    for folder in include:
        if only and not any(_under(path, [folder]) or _under(folder, [path]) for path in only):
            continue  # a folder the scan was not asked about
        for relative in _walk(root, folder, rules):
            found.add(relative)
            result.ignored.discard(relative)
    result.skipped_by_gitignore = len(result.ignored)
    for relative in list(found):
        # left out, so a copy indexed before the rule was there is removed with the other ignored files
        if _under(relative, ignore) or generated.excludes_file(relative):
            found.discard(relative)
            result.ignored.add(relative)
    result.rules = rules
    result.files = sorted(found, key=lambda path: path.split("/"))  # folder by folder, as the discovery cursor counts
    return result


def _walk_all(root: Path, rules: _Rules, start: str = "", generated: Optional[_Generated] = None) -> Iterator[str]:
    for directory, subfolders, names in os.walk(root / start if start else root, onerror=_raise):
        relative_dir = PurePosixPath(Path(directory).relative_to(root)).as_posix()
        relative_dir = "" if relative_dir == "." else relative_dir
        subfolders[:] = sorted(
            sub for sub in subfolders
            if not rules.scanner.should_ignore(Path(sub)) and not (not relative_dir and sub == STATE_DIR)
            # a generated tree is not walked at all: its files would only be found to be left out
            and not (generated and generated.excluded_folder(f"{relative_dir}/{sub}" if relative_dir else sub))
        )
        for name in sorted(names):
            relative = f"{relative_dir}/{name}" if relative_dir else name
            if rules.wanted(relative):
                yield relative
