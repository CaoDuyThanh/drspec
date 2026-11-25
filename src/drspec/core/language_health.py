"""How well each language is covered in this project, for `status`: progress that can be measured, not anecdotal."""

from __future__ import annotations

import json
import re
import zlib
from typing import Any, Dict

import duckdb

from drspec.core.file_records import packed_functions
from drspec.core.languages import LANGUAGES, suffixes_of
from drspec.core.partial import KEY_PREFIX as PARTIAL_PREFIX
from drspec.core.partial import SKIPPED_PREFIX, lost_something
from drspec.core.unresolved import definition_counts, refine_not_found


#: C and C++ library functions: a call to one is not a call to a function of the project.
_LIBRARY_CALLS = frozenset(
    """abs acos asin atan atan2 atof atoi atol bsearch calloc ceil cos cosh exit exp fabs fclose feof ferror fflush fgetc fgets
    floor fmod fopen fprintf fputc fputs fread free frexp fscanf fseek ftell fwrite getenv isalnum isalpha isdigit islower isspace
    isupper ldexp log log10 longjmp malloc memchr memcmp memcpy memmove memset modf pow printf putchar puts qsort rand realloc
    remove rename scanf setjmp sin sinh snprintf sprintf sqrt srand sscanf strcat strchr strcmp strcpy strerror strlen strncat
    strncmp strncpy strrchr strstr strtod strtol strtoul tan tanh time tolower toupper vfprintf vprintf vsnprintf vsprintf
    sqrtf sinf cosf tanf powf fabsf floorf ceilf fmodf expf logf atan2f acosf asinf roundf round trunc lround va_start va_end
    va_copy assert static_assert sizeof alignof offsetof new delete move forward swap min max""".split()
)
_MACRO_NAME = re.compile(r"^[A-Z][A-Z0-9_]{2,}$|^_*[A-Z][A-Z0-9]*_[A-Z0-9_]+$")


def classify_cpp_call(call: Dict[str, Any]) -> str:
    """Why a C++ call is not tied to a function of the project, by what the call is: `macro` (an all-capitals name, the
    invocation of a macro), `external` (the C or C++ library) or `not found`."""
    name = str(call.get("name", ""))
    receiver = str(call.get("receiver") or "")
    if _MACRO_NAME.match(name):
        return "macro"
    if receiver.split("::")[0] == "std" or (not receiver and name in _LIBRARY_CALLS) or name.startswith("std::"):
        return "external"
    return "not found"


_PYTHON_BUILTINS = frozenset(name for name in dir(__import__("builtins")) if not name.startswith("_"))


def classify_python_call(call: Dict[str, Any], project_methods: frozenset = frozenset()) -> str:
    """Why a Python call is not tied to a function of the project, by what the call is: `builtin` (a bare name of the
    language), `external` (a call on `self` or `cls` to a name that no class of the project defines: a method the class
    inherits from a library), `unknown receiver` (a method of an object whose type is not known here) or `not found` (a
    bare name that no scanned module defines: a library, or a name made at run time)."""
    receiver = call.get("receiver")
    if receiver:
        if receiver in ("self", "cls") and str(call.get("name", "")) not in project_methods:
            return "external"
        return "unknown receiver"
    if str(call.get("name", "")) in _PYTHON_BUILTINS:
        return "builtin"
    return "not found"


def language_health(conn: duckdb.DuckDBPyConnection, key: str) -> Dict[str, Any]:
    """`{}` when the project has no file of the family `key`; otherwise the number of files, files read only in part,
    constructs skipped without being counted as errors, the share of calls that could not be tied to a function and,
    for a resolver that records them, how many calls were left unresolved for each reason."""
    support = LANGUAGES[key]
    suffixes = suffixes_of(key)
    files = [row[0] for row in conn.execute("SELECT file_path FROM scanned_files").fetchall() if row[0].endswith(suffixes)]
    if not files:
        return {}
    family = set(files)
    partial = warnings = objective_skipped = 0
    for name, value in conn.execute("SELECT key, value FROM config WHERE starts_with(key, ?)", [PARTIAL_PREFIX]).fetchall():
        if name[len(PARTIAL_PREFIX):] not in family:
            continue
        try:
            info = json.loads(value)
            lost = lost_something(info)
            if name.endswith(".mm"):  # Objective-C++ is read with the C++ grammar: its `@interface` and `- (void)m:` are skipped
                objective_skipped += len(info.get("skipped", []))
        except ValueError:
            lost = True
        partial, warnings = partial + lost, warnings + (not lost)
    skipped = sum(
        int(value) for name, value in conn.execute("SELECT key, value FROM config WHERE starts_with(key, ?)", [SKIPPED_PREFIX]).fetchall()
        if name[len(SKIPPED_PREFIX):] in family and str(value).isdigit()
    )
    marks = ", ".join("?" for _ in support.languages)
    resolved_sites = {
        row[0]: set(str(row[1] or "").split("; "))
        for row in conn.execute(
            f"SELECT caller_id, string_agg(call_site, '; ') FROM dependencies d JOIN artifacts a ON a.function_id = d.caller_id "
            f"WHERE a.language IN ({marks}) GROUP BY caller_id",
            list(support.languages),
        ).fetchall()
    }
    total = unresolved = 0
    definitions = definition_counts(conn)
    cpp_reasons: Dict[str, int] = {}
    project_methods: frozenset = frozenset()
    if key == "python":
        project_methods = frozenset(row[0] for row in conn.execute("SELECT DISTINCT function_name FROM artifacts WHERE language = 'python' AND parent IS NOT NULL").fetchall())
    for name, value in conn.execute("SELECT key, value FROM config WHERE starts_with(key, 'scan:file:')").fetchall():
        if name[len("scan:file:"):] not in family or not value:
            continue
        try:
            functions = packed_functions(value, name[len("scan:file:"):])
        except (ValueError, zlib.error):
            continue
        for function in functions:
            sites = resolved_sites.get(function.get("function_id"), set())
            for call in function.get("calls", []):
                if call.get("text") not in sites:
                    if key in ("cpp", "python"):
                        reason = classify_cpp_call(call) if key == "cpp" else classify_python_call(call, project_methods)
                        if reason == "not found":  # by what the project defines under that name: library, our own function, or several
                            reason = refine_not_found(call.get("name", ""), definitions)
                        cpp_reasons[reason] = cpp_reasons.get(reason, 0) + 1
                        if reason in ("macro", "external", "builtin"):
                            continue  # not a call to a function of the project: counted on its own, not as unresolved
                    unresolved += 1
                total += 1
    reasons: Dict[str, int] = {}
    if key in ("cpp", "python"):
        reasons = {reason: count for reason, count in cpp_reasons.items() if reason not in ("macro", "external", "builtin")}
    if support.reasons_key:
        existing = {row[0] for row in conn.execute("SELECT function_id FROM artifacts").fetchall()}
        for name, value in conn.execute("SELECT key, value FROM config WHERE starts_with(key, ?)", [support.reasons_key]).fetchall():
            if name[len(support.reasons_key):] not in existing:
                continue
            try:
                counts = json.loads(value)
            except ValueError:
                continue
            names = counts.pop("names", None)  # the names of the calls that were "not found" (written by a scan since the split)
            for reason, count in counts.items():
                if reason == "not found" and isinstance(names, list) and len(names) == int(count):
                    for call_name in names:
                        refined = refine_not_found(call_name, definitions)
                        reasons[refined] = reasons.get(refined, 0) + 1
                else:
                    reasons[reason] = reasons.get(reason, 0) + int(count)
    # `new X()` of a class with no constructor of its own is not a failure to resolve: leave it out of both figures.
    implicit = reasons.pop("implicit constructor", 0)
    total, unresolved = max(total - implicit, 0), max(unresolved - implicit, 0)
    health: Dict[str, Any] = {"files": len(files), "files_partially_parsed": partial, "skipped_constructs": skipped, "calls": total, "unresolved_calls": unresolved}
    if warnings:
        health["files_read_with_warnings"] = warnings
    if total:
        health["unresolved_share"] = round(100.0 * unresolved / total, 1)
    if reasons:
        health["unresolved_reasons"] = dict(sorted(reasons.items()))
    if key == "cpp":
        health["macro_invocations"] = cpp_reasons.get("macro", 0)
        health["library_calls"] = cpp_reasons.get("external", 0)
    if key == "cpp":  # the C inside the family: files and functions stored as C
        c_files, c_functions = conn.execute("SELECT COUNT(DISTINCT file_path), COUNT(*) FROM artifacts WHERE language = 'c'").fetchone()
        if c_functions:
            health["c_files"], health["c_functions"] = int(c_files), int(c_functions)
    if objective_skipped:
        health["objective_c_functions_skipped"] = objective_skipped
    if key == "python":
        health["library_calls"] = cpp_reasons.get("builtin", 0) + cpp_reasons.get("external", 0)
    return health


def cpp_health(conn: duckdb.DuckDBPyConnection) -> Dict[str, Any]:
    return language_health(conn, "cpp")


def language_blocks(conn: duckdb.DuckDBPyConnection) -> Dict[str, Dict[str, Any]]:
    """The health block of every language the project has files of, by family key."""
    blocks = {}
    for key in LANGUAGES:
        health = language_health(conn, key)
        if health:
            blocks[key] = health
    return blocks
