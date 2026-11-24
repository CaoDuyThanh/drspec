"""DB-backed project-wide facts for the call resolver.

Resolves already-parsed facts (imports, same-file names) against the
project's already-scanned artifacts via pure DB aggregation — no parsing
happens here.
"""

from __future__ import annotations

import re
from typing import Optional

import duckdb

from drspec.db.queries import get_artifacts_by_file
from drspec.parsers.models import RawModuleImport, RawNameImport


#: Functions already recorded as removed are not there for the purpose of resolving a call.
_LIVE = "function_id NOT IN (SELECT function_id FROM pending_removals)"


def class_identity(file_path: str, qualified_name: str) -> str:
    return f"{file_path}::{qualified_name}"


def _file_bindings(conn, file_path: str, imports, cache: Optional[dict]) -> tuple[dict, dict]:
    """What a file's imports name: `(class bindings, module bindings)`. They depend
    on the file and the scanned artifacts only, not on any one function, so with a
    `cache` they are worked out once per file for a whole sync."""
    key = ("bindings", file_path)
    if cache is not None and key in cache:
        return cache[key]
    bindings: dict = {}
    for raw in imports.name_imports if imports else []:
        target = _cached(cache, ("module", raw.module_path), lambda: _guess_module_file_path(conn, raw.module_path, cache))
        if target is None:
            # Class may be imported via a package's __init__.py re-export
            # rather than defined in a directly-matched file.
            target = _cached(cache, ("reexport", raw.module_path, raw.name), lambda: _guess_package_reexport_class_file(conn, raw.module_path, raw.name, cache))
        name = raw.alias or raw.name
        identity = class_identity(target, raw.name) if target else None
        bindings.setdefault(name, set()).add(identity)
    modules: dict = {}
    for raw in imports.module_imports if imports else []:
        if raw.alias:
            modules.setdefault(raw.alias, set()).add(_cached(cache, ("module", raw.module_path), lambda: _guess_module_file_path(conn, raw.module_path, cache)))
    if cache is not None:
        cache[key] = (bindings, modules)
    return bindings, modules


def _cached(cache: Optional[dict], key: tuple, compute):
    if cache is None:
        return compute()
    if key not in cache:
        cache[key] = compute()
    return cache[key]


def bind_declared_types(
    conn, file_path: str, imports, declared_types: dict[str, str], classes: dict, cache: Optional[dict] = None
) -> dict[str, str]:
    """Bind annotations to local or explicitly imported class identities.

    Never select a project-wide class merely because its short name is unique.
    Conflicting imports and unknown annotations remain unresolved.

    `cache` (one dict per sync, used only while the scanned artifacts do not
    change) keeps a file's import bindings from being looked up again for every
    function in it.
    """
    if not declared_types:
        return {}  # nothing is annotated, so there is nothing to bind
    bindings, modules = _file_bindings(conn, file_path, imports, cache)
    resolved = {}
    for variable, annotation in declared_types.items():
        # An unresolved annotated parameter still shadows a module import.
        resolved[variable] = "<unresolved>"
        identity = None
        if annotation in bindings:
            candidates = bindings[annotation]
            if len(candidates) == 1:
                identity = next(iter(candidates))
        elif '.' in annotation:
            module, name = annotation.rsplit('.', 1)
            imported_class, nested = annotation.split('.', 1)
            imported_candidates = bindings.get(imported_class, set())
            if len(imported_candidates) == 1:
                base = next(iter(imported_candidates))
                identity = base + '.' + nested if base else None
                if identity in classes:
                    resolved[variable] = identity
                continue
            candidates = modules.get(module, set())
            if len(candidates) == 1:
                target = next(iter(candidates))
                identity = class_identity(target, name) if target else None
            elif not candidates:
                identity = class_identity(file_path, annotation)  # nested local class
        else:
            identity = class_identity(file_path, annotation)
        if identity in classes:
            resolved[variable] = identity
    return resolved


def build_classes_by_name(conn: duckdb.DuckDBPyConnection) -> dict[str, dict[str, list[str]]]:
    """file::qualified_class -> {method_name: [function_id, ...]}, project-wide (every
    class this resolver might need to reach, same-file or imported). A method with
    overloads has several ids.
    Built once per scan invocation, not per caller — see `graph_sync.py`.
    """
    classes: dict[str, dict[str, list[str]]] = {}
    rows = conn.execute(
        f"SELECT function_id, function_name, parent, file_path FROM artifacts WHERE parent IS NOT NULL AND {_LIVE} ORDER BY function_id"
    ).fetchall()
    for function_id, function_name, parent, file_path in rows:
        classes.setdefault(class_identity(file_path, parent), {}).setdefault(function_name, []).append(function_id)
    return classes


def build_properties_by_name(conn: duckdb.DuckDBPyConnection) -> dict[str, dict[str, str]]:
    """file::qualified_class -> {property_name: function_id}, for Python
    `@property` reads. Only `is_property = TRUE` methods count — matches
    the `calls` resolver's narrow, never-guess scope.
    """
    properties: dict[str, dict[str, str]] = {}
    rows = conn.execute(
        f"SELECT function_id, function_name, parent, file_path FROM artifacts "
        f"WHERE parent IS NOT NULL AND is_property = TRUE AND {_LIVE}"
    ).fetchall()
    for function_id, function_name, parent, file_path in rows:
        properties.setdefault(class_identity(file_path, parent), {})[function_name] = function_id
    return properties


def build_project_function_index(conn: duckdb.DuckDBPyConnection) -> dict[str, list[str]]:
    """function_name -> [function_id, ...], project-wide — only used by
    the opt-in `deep_scan=True` fallback."""
    index: dict[str, list[str]] = {}
    rows = conn.execute(f"SELECT function_id, function_name FROM artifacts WHERE {_LIVE}").fetchall()
    for function_id, function_name in rows:
        index.setdefault(function_name, []).append(function_id)
    return index


def resolve_name_imports(conn: duckdb.DuckDBPyConnection, raw_imports: list[RawNameImport], cache: Optional[dict] = None) -> dict[str, list[str]]:
    """Resolve real `from X import Y [as Z]`-shaped facts (a file's
    `ParseResult.name_imports` / `ScanResult.file_imports[...].name_imports`,
    each a `RawNameImport`) against already-scanned artifacts.

    Returns:
        local name (alias, or the imported name itself) -> [function_id, ...]
        (several for overloads). An import that can't be matched to exactly one known file is
        simply absent — never guessed.

    `cache` (one dict per publish, used only while the scanned artifacts stay as they are) keeps the same module
    name from being looked up again for every file that imports it.
    """
    resolved: dict[str, list[str]] = {}

    for raw in raw_imports:
        target_file = _cached(cache, ("module", raw.module_path), lambda: _guess_module_file_path(conn, raw.module_path, cache))
        if target_file is not None:
            module_functions = _cached(cache, ("byname", target_file), lambda: _functions_by_name(conn, target_file, _cached(cache, ("gone",), lambda: {row[0] for row in conn.execute("SELECT function_id FROM pending_removals").fetchall()})))
            if raw.name in module_functions:
                resolved[raw.alias or raw.name] = module_functions[raw.name]
            continue

        # module_path's last component may be a package dir, not a file:
        # this project's imports go through __init__.py re-exports rather
        # than the defining submodule, so the stem match above finds nothing.
        reexport_ids = _guess_package_reexport_function_id(conn, raw.module_path, raw.name, cache)
        if reexport_ids:
            resolved[raw.alias or raw.name] = reexport_ids

    return resolved


def resolve_module_imports(
    conn: duckdb.DuckDBPyConnection, raw_imports: list[RawModuleImport], cache: Optional[dict] = None
) -> dict[str, dict[str, list[str]]]:
    """Resolve real `import X [as Y]`-shaped facts (a file's
    `ParseResult.module_imports`, each a `RawModuleImport`) against
    already-scanned artifacts.

    Returns:
        module alias -> {function_name: [function_id, ...]} for that module's
        own functions. A module that can't be matched to exactly one
        known file is simply absent — never guessed.
    """
    resolved: dict[str, dict[str, list[str]]] = {}

    for raw in raw_imports:
        if raw.alias is None:
            continue
        target_file = _cached(cache, ("module", raw.module_path), lambda: _guess_module_file_path(conn, raw.module_path, cache))
        if target_file is not None:
            resolved[raw.alias] = _cached(cache, ("byname", target_file), lambda: _functions_by_name(conn, target_file, _cached(cache, ("gone",), lambda: {row[0] for row in conn.execute("SELECT function_id FROM pending_removals").fetchall()})))
            continue

        # Same package-re-export fallback as above, for a whole-module
        # alias import instead of a single name.
        package_dir = _module_path_to_package_dir(raw.module_path)
        if package_dir is not None:
            package_functions = _cached(cache, ("package", package_dir), lambda: _functions_by_name_in_package(conn, package_dir, cache))
            if package_functions:
                resolved[raw.alias] = package_functions

    return resolved


def _functions_by_name(conn: duckdb.DuckDBPyConnection, file_path: str, gone: Optional[set] = None) -> dict[str, list[str]]:
    """A file's own importable (top-level, non-method) functions by name; a name defined
    more than once (overloads) lists every definition.

    Methods are excluded: import syntax can't target a class method
    directly, and including them would let a same-named free function
    and method collide on the bare name, picking one arbitrarily.
    """
    by_name: dict[str, list[str]] = {}
    if gone is None:
        gone = {row[0] for row in conn.execute("SELECT function_id FROM pending_removals").fetchall()}
    for a in sorted(get_artifacts_by_file(conn, file_path), key=lambda artifact: artifact.function_id):
        if a.parent is None and a.function_id not in gone:
            by_name.setdefault(a.function_name, []).append(a.function_id)
    return by_name


def _guess_module_file_path(conn: duckdb.DuckDBPyConnection, module_path: str, cache: Optional[dict] = None) -> Optional[str]:
    """Guess which scanned file a dotted/relative/namespaced module path
    (`drspec.core.hasher`, `.hasher`, `./utils`, `std`) refers to, by
    matching its last non-empty `.`/`/`-separated component against
    known file stems. Returns `None` (never guesses) if zero or more
    than one file match.

    Can't match a package import (`drspec.db`, where "db" names a
    directory, not a file); see `_module_path_to_package_dir` for that case.
    """
    components = [c for c in module_path.replace("/", ".").split(".") if c]
    if not components:
        return None
    stem = components[-1]

    if cache is not None and not _WILDCARDS.search(stem):
        files = _index(conn, cache).files_with_stem(stem)
        return next(iter(files)) if len(files) == 1 else None
    rows = conn.execute(
        "SELECT DISTINCT file_path FROM artifacts WHERE file_path LIKE '%/' || ? || '.%' OR file_path LIKE ? || '.%'",
        [stem, stem],
    ).fetchall()
    if len(rows) == 1:
        return rows[0][0]
    return None


def _module_path_to_package_dir(module_path: str) -> Optional[str]:
    """`"drspec.db"` -> `"drspec/db"` — the directory a package import
    would correspond to, for the `_guess_module_file_path` fallbacks
    below. `None` for an empty/root path (nothing to scope a search to).
    """
    package_dir = module_path.replace(".", "/").strip("/")
    return package_dir or None


def _functions_by_name_in_package(conn: duckdb.DuckDBPyConnection, package_dir: str, cache: Optional[dict] = None) -> dict[str, list[str]]:
    """Union of a package directory's direct child files' top-level
    functions, for `from <package> import X` where `<package>`'s
    `__init__.py` re-exports `X` from a submodule. `__init__.py` itself
    has no function defs to scan, so there's no file for the stem-match
    above to find.

    Approximates the re-export without tracing it: a name unique among
    direct children (one level only, no nested sub-packages) resolves;
    a collision across sibling files is dropped rather than guessed (overloads
    inside one file are kept together) — this
    can't confirm the name is actually re-exported, just that it's
    unambiguous in the directory.

    `package_dir` is matched both root-anchored and mid-path since real
    `file_path`s are rooted under `src/`.
    """
    if cache is not None and not _WILDCARDS.search(package_dir):
        rows = _index(conn, cache).top_level_in(package_dir)
    else:
        rows = _functions_in_package_query(conn, package_dir)
    files_by_name: dict[str, set[str]] = {}
    ids_by_name: dict[str, list[str]] = {}
    for function_name, function_id, file_path in sorted(rows, key=lambda row: row[1]):
        files_by_name.setdefault(function_name, set()).add(file_path)
        ids_by_name.setdefault(function_name, []).append(function_id)
    return {name: ids for name, ids in ids_by_name.items() if len(files_by_name[name]) == 1}


def _functions_in_package_query(conn: duckdb.DuckDBPyConnection, package_dir: str) -> list:
    """`(function_name, function_id, file_path)` of the top-level functions of the files directly in `package_dir`."""
    return conn.execute(
        """
        SELECT function_name, function_id, file_path FROM artifacts
        WHERE parent IS NULL AND function_id NOT IN (SELECT function_id FROM pending_removals)
          AND (file_path LIKE ? || '/%' OR file_path LIKE '%/' || ? || '/%')
          AND NOT (file_path LIKE ? || '/%/%' OR file_path LIKE '%/' || ? || '/%/%')
        """,
        [package_dir, package_dir, package_dir, package_dir],
    ).fetchall()


def _guess_package_reexport_function_id(conn: duckdb.DuckDBPyConnection, module_path: str, name: str, cache: Optional[dict] = None) -> Optional[list[str]]:
    """`from <module_path> import <name>`'s package-re-export fallback —
    see `_functions_by_name_in_package`. `None` if `module_path` has no
    directory component, or `name` isn't uniquely defined among that
    package's direct children.
    """
    package_dir = _module_path_to_package_dir(module_path)
    if package_dir is None:
        return None
    return _cached(cache, ("package", package_dir), lambda: _functions_by_name_in_package(conn, package_dir, cache)).get(name)


def _guess_package_reexport_class_file(conn: duckdb.DuckDBPyConnection, module_path: str, qualified_name: str, cache: Optional[dict] = None) -> Optional[str]:
    """Package-re-export fallback for a class name (see
    `_functions_by_name_in_package`). Matched against `artifacts.parent`
    since a class has no row of its own — only its methods do, each
    carrying the class name in `parent`. Returns the one file among
    `module_path`'s package directory's direct children that defines
    this class, or `None` if zero or more than one do.
    """
    package_dir = _module_path_to_package_dir(module_path)
    if package_dir is None:
        return None
    if cache is not None and not _WILDCARDS.search(package_dir):
        files = _index(conn, cache).files_defining_class_in(qualified_name, package_dir)
        return next(iter(files)) if len(files) == 1 else None
    rows = conn.execute(
        """
        SELECT DISTINCT file_path FROM artifacts
        WHERE parent = ? AND function_id NOT IN (SELECT function_id FROM pending_removals)
          AND (file_path LIKE ? || '/%' OR file_path LIKE '%/' || ? || '/%')
          AND NOT (file_path LIKE ? || '/%/%' OR file_path LIKE '%/' || ? || '/%/%')
        """,
        [qualified_name, package_dir, package_dir, package_dir, package_dir],
    ).fetchall()
    if len(rows) == 1:
        return rows[0][0]
    return None


# ---------------------------------------------------------------------------
# The same questions answered from memory
# ---------------------------------------------------------------------------


#: What makes a `LIKE` pattern more than the text it holds (and an empty directory name, which no component of a path
#: stands for). A name with one of them is asked of the database, as it always was: `_` there matches any character, `/`
#: and `.` too, and the in-memory index does not copy that.
_WILDCARDS = re.compile(r"[%_\\]|//")


class _ProjectIndex:
    """What `_guess_module_file_path`, `_functions_by_name_in_package` and `_guess_package_reexport_class_file` ask the
    `artifacts` table, read once: each of those questions was a scan of the whole table, asked once for every module and
    package a publish meets (19,000 times for the first scan of Django, 25% of it). Valid while the scanned artifacts stay as
    they are, as the cache that holds it is. For a name without a `LIKE` wildcard each answer is the one the query gives."""

    def __init__(self, conn: duckdb.DuckDBPyConnection):
        files = [row[0] for row in conn.execute("SELECT DISTINCT file_path FROM artifacts").fetchall()]
        # `/stem.` in a LIKE: a component of the path that holds a dot, by what comes before the first one
        self._by_stem: dict[str, set[str]] = {}
        # `package_dir` as the directory of a file: the query takes a file whose path holds `/package_dir/` (or starts with
        # `package_dir/`) and holds no `/` after the *first* such place; so a directory name that appears twice in a path
        # (`x/utils/y/utils/f.py`) is not the directory of that file. Found here as: the tail of the directory that is the
        # only place in it where those components stand together.
        self._by_directory: dict[str, set[str]] = {}
        for file_path in files:
            parts = file_path.split("/")
            for part in parts:
                if "." in part:
                    self._by_stem.setdefault(part.split(".", 1)[0], set()).add(file_path)
            directory = parts[:-1]
            seen: dict[str, int] = {}
            for start in range(len(directory)):
                for end in range(start + 1, len(directory) + 1):
                    key = "/".join(directory[start:end])
                    seen[key] = seen.get(key, 0) + 1
            for start in range(len(directory)):
                tail = "/".join(directory[start:])
                if seen[tail] == 1:
                    self._by_directory.setdefault(tail, set()).add(file_path)
        live = "function_id NOT IN (SELECT function_id FROM pending_removals)"
        self._top_level: dict[str, list] = {}
        for function_name, function_id, file_path in conn.execute(f"SELECT function_name, function_id, file_path FROM artifacts WHERE parent IS NULL AND {live}").fetchall():
            self._top_level.setdefault(file_path, []).append((function_name, function_id, file_path))
        self._class_files: dict[str, set[str]] = {}
        for parent, file_path in conn.execute(f"SELECT DISTINCT parent, file_path FROM artifacts WHERE parent IS NOT NULL AND {live}").fetchall():
            self._class_files.setdefault(parent, set()).add(file_path)

    def files_with_stem(self, stem: str) -> set[str]:
        return self._by_stem.get(stem, set())

    def top_level_in(self, package_dir: str) -> list:
        return [row for file_path in self._by_directory.get(package_dir, ()) for row in self._top_level.get(file_path, ())]

    def files_defining_class_in(self, qualified_name: str, package_dir: str) -> set[str]:
        return self._class_files.get(qualified_name, set()) & self._by_directory.get(package_dir, set())


def _index(conn: duckdb.DuckDBPyConnection, cache: dict) -> _ProjectIndex:
    index = cache.get(("project index",))
    if index is None:
        index = cache[("project index",)] = _ProjectIndex(conn)
    return index
