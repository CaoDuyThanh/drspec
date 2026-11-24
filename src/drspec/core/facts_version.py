"""Re-reading files when the parser starts to record something new about them.

A file's stored snapshot holds what the parser found when the file was last read. When a release makes a
parser record more (the module facts that JavaScript and TypeScript calls are resolved through), files that
have not changed since would keep the old, poorer snapshot, so their calls would stay unresolved until each was
edited. This queues them once, at the next worker start.
"""

from __future__ import annotations

import duckdb

from drspec.core.languages import suffixes_of
from drspec.db.queries import clear_all_rescan_backoff, enqueue_staleness_many, get_config, list_scanned_file_paths, set_config

FACTS_VERSION = {"javascript": "2", "cpp": "9"}  # cpp 9: the type aliases of a file (`using X = Base<Arg>;`, `typedef Base<Arg> X;`) are recorded, so a base or a variable that is an alias resolves; cpp 8: the classes and members of a struct named by a scope or template arguments (`template<> struct Node<X>::Impl`), and the functions of the files that lost them (a destructor named `A<X>::~A<X>`, an argument alone on its line); cpp 7: a member inherited from a template base is typed by the base's arguments (`Base<Context>`), and an unbound one is unknown, not a library type; cpp 6: the base of a chain whose last member is written carries the members it is written through (`written_through`); cpp 5: a receiver that is `std::make_shared<T>()` or `std::make_unique<T>()` is kept, so an `auto` local it declares has T's type; cpp 4: the member accesses inside a function whose condition is a bare field (`if (flag)` was taken for a declaration and dropped them); cpp 3: the non-const reference parameters of every function; cpp 1: the classes, the return types and the declared types of `auto`, loops and lambdas; cpp 2: every data member of a class
"""Language family -> version of the facts its parser records. Raise it when the parser records more."""

_MARKER = "scan:facts:"
#: The yaml config files (`core/config_keys.py`) are not a language family, so they have their own version: 2 = an OpenCV `%YAML:1.0`
#: first line is read (such files were listed as problems), so every yaml file is read once more at the next worker start.
YAML_FACTS_VERSION = "2"


def requeue_for_new_facts(conn: duckdb.DuckDBPyConnection) -> int:
    """Queue the files whose snapshot was made by an older parser. Returns how many were queued."""
    queued = 0
    changed = False
    for family, version in FACTS_VERSION.items():
        if get_config(conn, _MARKER + family) == version:
            continue
        changed = True
        suffixes = suffixes_of(family)
        files = sorted(path for path in list_scanned_file_paths(conn) if path.endswith(suffixes))
        if files:
            enqueue_staleness_many(conn, [(path, "drift") for path in files], overwrite=False)
            queued += len(files)
        set_config(conn, _MARKER + family, version)
    if get_config(conn, _MARKER + "yaml") != YAML_FACTS_VERSION:
        changed = True
        from drspec.core.config_keys import CONFIG_SUFFIXES

        files = sorted(path for path in list_scanned_file_paths(conn) if path.lower().endswith(CONFIG_SUFFIXES))
        if files:
            enqueue_staleness_many(conn, [(path, "drift") for path in files], overwrite=False)
            queued += len(files)
        set_config(conn, _MARKER + "yaml", YAML_FACTS_VERSION)
    if changed:  # the files that could not be read may parse with the new facts: they are read again
        clear_all_rescan_backoff(conn)
    return queued
