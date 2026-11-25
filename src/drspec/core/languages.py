"""The languages drspec reads, and what is needed to support one.

One place says which file suffixes belong to a language, which stored `language` values make up its
family (TypeScript and TSX are read with JavaScript), and where its health figures come from, so that
adding a language means filling in one entry here, a parser, a normaliser in `hasher.py`, a resolver and
the packaging, rather than finding every place a language name was written by hand.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, Tuple


@dataclass(frozen=True)
class LanguageSupport:
    key: str
    """The name of the family in `status` and in the language filters."""
    languages: Tuple[str, ...]
    """The `language` values stored for the family's functions."""
    resolver: str
    """How calls are tied to functions: `names` (per-file imports), `scoped` (C++) or `modules` (JavaScript and Rust)."""
    reasons_key: str = ""
    """Config key prefix under which a resolver stores why a call was left unresolved; empty when it does not."""
    ignored_folders: Tuple[str, ...] = ()
    """Folders the family's tooling generates; added to the scanner's ignore list."""


LANGUAGES: Dict[str, LanguageSupport] = {
    "python": LanguageSupport("python", ("python",), "names"),
    "javascript": LanguageSupport("javascript", ("javascript", "typescript", "tsx"), "modules", "js:unresolved:", ("node_modules", "dist", "build")),
    "cpp": LanguageSupport("cpp", ("cpp", "c"), "scoped"),
    "rust": LanguageSupport("rust", ("rust",), "modules", "rust:unresolved:", ("target",)),
}


#: C and C++ are read by one parser and one resolver; a function stored as `c` is a C function.
C_FAMILY = ("c", "cpp")


def is_c_family(language: str) -> bool:
    return language in C_FAMILY


def languages_matching(name: str) -> Tuple[str, ...]:
    """The stored `language` values a language filter (`--include`) stands for: `cpp` is C++ and C together, `c` is only C."""
    return C_FAMILY if name == "cpp" else (name,)


def family_of(language: str) -> str:
    """The family a stored `language` value belongs to (`tsx` is `javascript`)."""
    for support in LANGUAGES.values():
        if language in support.languages:
            return support.key
    return language


def suffixes_of(key: str) -> Tuple[str, ...]:
    """The file suffixes the scanner reads as the family `key` (taken from the scanner's own map, not repeated here)."""
    from drspec.core.scanner import LANGUAGE_MAP

    languages = LANGUAGES[key].languages
    return tuple(suffix for suffix, language in LANGUAGE_MAP.items() if language in languages)
