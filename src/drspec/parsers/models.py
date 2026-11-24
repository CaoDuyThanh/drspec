"""Data models for code parsing results."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional


@dataclass
class RawMemberAccess:
    """One read or write of a member or field (C++ first), found in the function's body.

    Attributes:
        receiver: The expression the member is taken from, in the form of `RawCall.receiver` (`r`, `this`, `p->inner`,
            `bodies[]`), `None` for a bare name inside a member function (`data`, the field of the enclosing class, or a
            local; the resolver tells which), `""` when the receiver is too complex to write down (never guessed).
        member: The member's name.
        kind: `read`, `write` (`=`, an element or a mutating method of the member: `r.data[0] = 1`, `r.data.push_back(1)`)
            or `readwrite` (`+=`, `++`, `--`).
        line: The line in the file (1-based).
        text: The expression's source text.
    """

    receiver: Optional[str]
    member: str
    kind: str
    line: int
    text: str
    call_receiver: Optional[str] = None
    call_name: Optional[str] = None
    arg_index: int = -1
    """When the member is an argument of a call (`f(r.data)`): the call's receiver and name and the argument's position, so the resolver
    can tell from the callee's declared parameter whether it is passed as a non-const reference (then it is read and written)."""
    written_through: str = ""
    """For the base of a chain whose last member is written (`r.inner.z = 3`: `inner` has `z` written through it): the members from the
    one after this one down to the written one, dot-joined (`z`; `c.d` for `a.b` in `a.b.c.d = x`). Empty for any other access."""


@dataclass
class RawCall:
    """One call expression, extracted directly from a `call`/`call_expression`
    AST node — never from text scanning, so a docstring, comment, or
    function signature can never be mistaken for a call.

    Attributes:
        receiver: The call's receiver, if any — `None` for a bare call
            (`foo()`), the receiver's plain name for an attribute/pointer
            call (`order.process()` -> `"order"`), or the sentinel
            `"super"` for `super().method()` (Python) / `super.method()`
            (JS/TS), with no resolution of which ancestor class it refers
            to. Also `None` when the receiver is too complex to represent
            (e.g. the result of another call or expression) — such calls
            are dropped at extraction time rather than guessed.
        name: The called function/method name.
        text: The call expression's source text (for `call_site`/embedding).
    """

    receiver: Optional[str]
    name: str
    text: str
    kind: str = "call"
    """`call`, or for JavaScript and TypeScript `new` (`new X()`), `jsx` (`<X />`) or `super` (`super(...)`)."""


@dataclass
class RawPropertyRead:
    """One non-call attribute-access expression (`obj.attr`, no trailing
    `()`), extracted directly from an `attribute`/`member_expression` AST
    node. Python-only: JS/C++ parsers never populate this (their
    `ExtractedFunction.property_reads` is always empty), since this
    project doesn't detect a JS/C++ equivalent of `@property` yet.

    Unlike `RawCall`, a property read always has a receiver — there's no
    "bare" form the way `foo()` is a bare call — so `receiver` isn't
    `Optional` here. A receiver too complex to represent (anything but a
    plain identifier, or a bare `super()`) is dropped at extraction time.

    Attributes:
        receiver: The accessed object's plain name, or the sentinel
            `"super"` for `super().attr` — never resolved to a specific
            ancestor class.
        name: The accessed attribute's name.
        text: The access expression's source text.
    """

    receiver: str
    name: str
    text: str


@dataclass
class RawNameImport:
    """A name bound directly to a specific import target — Python's
    `from X import Y [as Z]`, JS's `import { Y as Z } from "X"`. Resolved
    against already-scanned artifacts later (needs DB access the parser
    doesn't have) — see `drspec.core.project_facts`.
    """

    module_path: str
    name: str
    alias: Optional[str] = None


@dataclass
class RawSymbolRead:
    """One bare-identifier read inside a function body, extracted
    directly from an `identifier` AST node — never from text scanning. A
    *candidate* list, not a final answer: unlike `RawCall`/
    `RawPropertyRead`, whose AST node shape alone identifies the relation,
    a bare identifier's shape can't tell a module-level constant read
    apart from a local variable or parameter read, so resolution later
    filters this down to names that match a known module-level constant.

    No `receiver` field — a symbol read is always a plain name, never
    `obj.attr`.
    """

    name: str
    text: str


@dataclass
class ExtractedConstant:
    """One module-level `NAME = <expr>` (or `NAME: Type = <expr>`)
    assignment, extracted from the module's own top-level statements —
    deliberately not the full `ExtractedFunction` treatment: no
    signature/calls/decorators, just enough to give the symbol a node
    and a hashable value.

    Only single-identifier-target, module-level assignments. Tuple/
    multi-target assignment (`a, b = ...`) and augmented assignment
    (`x += 1`) are both excluded — neither has a single plain-identifier
    target.
    """

    name: str
    value_text: str
    start_line: int
    end_line: int


@dataclass
class RawModuleImport:
    """A name bound to an entire module/namespace, not a specific
    function — Python's `import X [as Y]`, JS's `import * as Y from "X"`.
    Resolving `alias.attr(...)` to a specific function needs the target
    module's own function list, looked up later against the DB.
    """

    module_path: str
    alias: Optional[str] = None


@dataclass
class ExtractedFunction:
    """Represents a function extracted from source code.

    Attributes:
        name: Function name (without parent prefix).
        qualified_name: Full qualified name (e.g., "ClassName.method_name").
        signature: Full function signature line.
        body: Complete function body text.
        start_line: 1-indexed starting line number.
        end_line: 1-indexed ending line number.
        parent: Parent class or function name, if any.
        decorators: List of decorator strings (without @).
        is_method: True if this is a class method.
        is_async: True if this is an async function.
        is_property: True if decorated with exactly `@property` — Python
            only; always False for JS/C++, which this project doesn't
            detect properties for yet. Checked for the bare decorator
            name only, not `@x.setter`/`@functools.cached_property`/etc.
            — see `project_facts.build_properties_by_name`.
        calls: Every call expression found in this function's body,
            extracted directly from the AST.
        property_reads: Every non-call attribute-access expression found
            in this function's body that could plausibly be a `@property`
            read — Python only, always empty for JS/C++.
        symbol_reads: Every bare-identifier read in this function's body
            that could plausibly be a module-level constant read —
            Python only, always empty for JS/C++. A candidate list;
            resolution filters it against known constants, never guessed
            here.
        first_param_name: The method's own first parameter name — Python
            only (JS/C++ have no explicit `self`/`this` parameter to name;
            self-detection there uses the fixed `this` keyword instead).
        declared_param_types: Parameter name -> declared type name, from
            a static annotation on that parameter (Python type hints,
            C++ parameter types). Empty for JS/TS — `tree-sitter-javascript`
            doesn't parse TypeScript type annotations at all, a hard
            dependency constraint, not a scope choice.
    """

    name: str
    qualified_name: str
    signature: str
    body: str
    start_line: int
    end_line: int
    parent: Optional[str] = None
    decorators: list[str] = field(default_factory=list)
    is_method: bool = False
    is_async: bool = False
    is_property: bool = False
    calls: list[RawCall] = field(default_factory=list)
    property_reads: list[RawPropertyRead] = field(default_factory=list)
    symbol_reads: list[RawSymbolRead] = field(default_factory=list)
    member_accesses: list[RawMemberAccess] = field(default_factory=list)
    first_param_name: Optional[str] = None
    declared_param_types: dict[str, str] = field(default_factory=dict)
    has_error: bool = False
    """The function's own syntax subtree contains an error, so what was extracted from it
    cannot be trusted; the scanner leaves such a function out."""
    parameter_tokens: list[str] = field(default_factory=list)
    """One token per parameter (its declared type, or its name when untyped); the part
    of the function's identifier that tells same-named definitions apart."""
    discriminator: str = ""
    """What else tells this definition from another of the same name and parameters, when the language has one: the
    header of the Rust `impl` block it is in (`IntoIterator for &mut A`). The scanner uses it, only when two
    definitions of a file would otherwise share an identifier."""


@dataclass
class ParseError:
    """Represents a syntax error found during parsing.

    Attributes:
        line: 1-indexed line number of the error.
        column: 0-indexed column number.
        message: Human-readable error message.
    """

    line: int
    column: int
    message: str


@dataclass
class ParseResult:
    """Result of parsing a source file.

    Attributes:
        functions: List of extracted functions.
        errors: List of syntax errors encountered.
        has_errors: True if any syntax errors were found.
        file_path: Optional path to the parsed file.
        name_imports: File-level `from X import Y` (or JS/C++ equivalent)
            statements — imports are a file-level concept, not a
            per-function one, so these live on `ParseResult`, not on each
            `ExtractedFunction`.
        module_imports: File-level `import X [as Y]` (or JS/C++
            equivalent) statements.
        constants: Module-level `NAME = <expr>` assignments — also a
            file-level concept, like imports. Python only; always empty
            for JS/C++.
    """

    functions: list[ExtractedFunction] = field(default_factory=list)
    errors: list[ParseError] = field(default_factory=list)
    has_errors: bool = False
    suspected_losses: list[int] = field(default_factory=list)
    """Lines in an error region that look like a function definition nothing was extracted from."""
    suspected_names: dict = field(default_factory=dict)
    """`{line: name}` for the suspected losses whose head gives a name (the identifier before the first parenthesis)."""
    includes: list[str] = field(default_factory=list)
    """C++: the targets of `#include "..."` (quoted includes only)."""
    skipped_constructs: int = 0
    """Valid constructs the grammar cannot parse (an explicit specialisation of a defaulted
    member, say) that were not counted as errors."""
    file_path: Optional[str] = None
    facts: dict = field(default_factory=dict)
    """JavaScript, TypeScript and Rust: what the file imports, exports and declares, for resolving calls across files
    (JavaScript: `bindings`, `exports`, `reexports`, `classes`, `namespaces`, `objects`; Rust: `uses`, `mods`, `impls`,
    `traits`, `types`); empty for other languages."""
    name_imports: list[RawNameImport] = field(default_factory=list)
    module_imports: list[RawModuleImport] = field(default_factory=list)
    constants: list[ExtractedConstant] = field(default_factory=list)
