"""Call resolution for C++.

C++ names are scoped (`math::Calc::mul`), a call may name its scope (`math::helper(r)`)
or not (`helper(a)` inside `math::Calc::mul` means `math::helper` or a member), and a
function is only visible where its header is included. So C++ gets its own resolver,
working from qualified names and the include graph rather than from per-file maps:

1. a bare call is looked up in the caller's class, then each enclosing namespace, then the
   global scope; the first scope that has a visible function of that name decides;
2. a qualified call is looked up the same way with its written scope in front;
3. a member call on a variable uses the type the variable was declared with in the same
   function (`math::Calc c(3);`, `Calc* p`, `const Calc& r`); `auto` is not guessed;
4. a function is visible from the caller's file, from the project files it includes and from
   those they include (one level further), by where it is defined or where it is declared.

5. when none of that finds the method, the receiver is given a type from what is declared: the class's base
   classes (a method of a base, called inside the derived class or on one), the declared type of a field (`ctx.stop()`),
   the declared return type of the function a call result comes from (`getSteering()->apply()`), the initialiser of
   `auto`, the element type of `std::vector<T>`, `std::array<T, N>` and arrays (`for (auto& b : bodies)`, `bodies[0]`),
   the declared parameter types of a lambda, and the template arguments of a class (`Holder<Body>::get()` returns
   `Body*`). No flow is followed: only what is written is used, and nothing is looked at that step 1-4 resolved.

6. last, with `infer_by_name` (the setting `resolve.infer_by_unique_name`): a member call on a receiver whose type step 5
   could not work out gets an `INFERRED` edge to the one method of that name the project defines, if there is exactly
   one; two or more methods, an overload set, a free function or an operator get nothing.

One match is `EXTRACTED`; several definitions of one qualified name (overloads) are one
`AMBIGUOUS` edge each. A call on a base-class type resolves to the method that class declares, not to the
overrides. Not attempted: overload resolution by argument types, argument-dependent lookup, calls through function
pointers, virtual dispatch, typedefs and `using` aliases, and templates instantiated elsewhere.
"""

from __future__ import annotations

import posixpath
from dataclasses import dataclass, field
from typing import Callable, Dict, Iterable, List, Optional, Set, Tuple

import duckdb

from drspec.core.call_resolver import CallSite, ResolvedCall
from drspec.core.identity import qualified_name
from drspec.parsers.cpp_types import CONTAINERS, MAKE_POINTER, POINTER_WRAPPERS, parse_type, pointee, split_chain

_RANK = {"AMBIGUOUS": 0, "INFERRED": 1, "EXTRACTED": 2}


@dataclass
class CppIndex:
    by_qualified: Dict[str, List[str]] = field(default_factory=dict)
    file_of: Dict[str, str] = field(default_factory=dict)
    declared_in: Dict[str, Set[str]] = field(default_factory=dict)
    anonymous: Dict[str, List[str]] = field(default_factory=dict)
    """Functions in an anonymous namespace, by the qualified name without it: visible from their own file only."""
    classes: Set[str] = field(default_factory=set)
    class_members: Dict[str, Set[str]] = field(default_factory=dict)
    """Qualified class name -> the names of all its data members, of any type (`class_facts` keeps only the ones whose type is of
    use to the typing); the union of its definitions, a class left out of `class_facts` for being defined differently included."""
    class_facts: Dict[str, dict] = field(default_factory=dict)
    """Qualified class name -> `{bases, fields, template}` as written (`cpp_types`), from the files' parse; a class that two
    files define differently is left out, since nothing can say which one a name means."""
    class_defs: Dict[str, List[Tuple[str, dict]]] = field(default_factory=dict)
    """Every definition of a class, by qualified name: (the file, its facts). A name with one definition is in `class_facts` too."""
    alias_defs: Dict[str, List[Tuple[str, Tuple[str, str]]]] = field(default_factory=dict)
    """Every definition of an alias, by qualified name: (the file, (target as written, scope)). A name with one definition is in `aliases`."""
    dropped_classes: Set[str] = field(default_factory=set)
    """The classes left out of `class_facts` because two files define them differently: still classes, so their methods still count as
    methods of that name (the by-name rule must not take another class's method as the only one)."""
    globals_of: Dict[str, Set[str]] = field(default_factory=dict)
    aliases: Dict[str, Tuple[str, str]] = field(default_factory=dict)
    """Qualified name of a type alias (`using X = Base<Arg>;`, `typedef Base<Arg> X;`) -> (the target as written, the scope it is written
    in: the alias's namespace or class, `""` at file level). An alias that two files define differently is left out."""
    """File -> the variables it declares at file or namespace level (what a receiver may name without declaring it)."""
    methods_by_name: Dict[str, List[str]] = field(default_factory=dict)
    """Method name -> the ids of the methods of that name (a function whose parent is a class the project defines)."""
    ref_params: Dict[str, str] = field(default_factory=dict)
    """Qualified function name -> a flag per parameter, `1` for a non-const reference (`T&`): what a member passed there may be changed by;
    a name whose overloads or declarations say different things is left out."""
    returns: Dict[str, str] = field(default_factory=dict)
    """Qualified function name -> its declared return type; a name whose overloads return different types is left out."""
    known_files: Set[str] = field(default_factory=set)
    includes_of: Callable[[str], List[str]] = lambda file: []
    _resolved: Dict[str, List[str]] = field(default_factory=dict)

    def includes(self, file_path: str) -> List[str]:
        """The project files `file_path` includes directly."""
        cached = self._resolved.get(file_path)
        if cached is None:
            cached = [found for target in self.includes_of(file_path) if (found := resolve_include(file_path, target, self.known_files))]
            self._resolved[file_path] = cached
        return cached

    def visible_files(self, file_path: str) -> Set[str]:
        """The file itself, what it includes, and what those include."""
        seen = {file_path}
        frontier = [file_path]
        for _ in range(2):
            frontier = [other for current in frontier for other in self.includes(current) if other not in seen]
            seen.update(frontier)
        return seen


def resolve_include(from_file: str, target: str, known_files: Iterable[str]) -> Optional[str]:
    """Which project file `#include "target"` in `from_file` means: relative to the including file's folder, or the
    one project file whose path ends with the target. `None` when there is none or the choice is not clear."""
    known = known_files if isinstance(known_files, (set, frozenset)) else set(known_files)
    relative = posixpath.normpath(posixpath.join(posixpath.dirname(from_file), target))
    if relative in known:
        return relative
    ending = [path for path in known if path == target or path.endswith("/" + target)]
    if len(ending) == 1:
        return ending[0]
    if ending:
        folder = posixpath.dirname(from_file)
        nearest = sorted(ending, key=lambda path: -len(posixpath.commonprefix([posixpath.dirname(path) + "/", folder + "/"])))
        best = len(posixpath.commonprefix([posixpath.dirname(nearest[0]) + "/", folder + "/"]))
        second = len(posixpath.commonprefix([posixpath.dirname(nearest[1]) + "/", folder + "/"]))
        if best > second:
            return nearest[0]
    return None


def build_cpp_index(
    conn: duckdb.DuckDBPyConnection,
    includes_of: Callable[[str], List[str]],
    known_files: Iterable[str],
    facts_of: Optional[Callable[[str], dict]] = None,
) -> CppIndex:
    index = CppIndex(known_files=set(known_files), includes_of=includes_of)
    if facts_of is not None:
        _read_type_facts(index, facts_of)
    rows = conn.execute(
        "SELECT function_id, file_path, parent, function_name FROM artifacts WHERE language IN ('cpp', 'c') "
        "AND function_id NOT IN (SELECT function_id FROM pending_removals) ORDER BY function_id"
    ).fetchall()
    for function_id, file_path, parent, function_name in rows:
        qualified = qualified_name(function_id)
        index.by_qualified.setdefault(qualified, []).append(function_id)
        if parent and (parent in index.class_facts or parent in index.dropped_classes):  # a method: its parent is a class (not a namespace)
            index.methods_by_name.setdefault(function_name, []).append(function_id)
        if "<anonymous>" in qualified:
            visible_name = "::".join(part for part in qualified.split("::") if part != "<anonymous>")
            index.anonymous.setdefault(visible_name, []).append(function_id)
        index.file_of[function_id] = file_path
        index.known_files.add(file_path)
        if parent:
            index.classes.add(parent)
    for definition_id, file_path in conn.execute("SELECT definition_id, file_path FROM function_declarations WHERE definition_id IS NOT NULL").fetchall():
        index.declared_in.setdefault(definition_id, set()).add(file_path)
    return index


def _read_type_facts(index: CppIndex, facts_of: Callable[[str], dict]) -> None:
    """The classes and return types the parse of each C or C++ file recorded (`facts` of its imports)."""
    from drspec.core.languages import suffixes_of

    suffixes = suffixes_of("cpp")
    clashing: Set[str] = set()
    clashing_returns: Set[str] = set()
    clashing_refs: Set[str] = set()
    for file_path in sorted(index.known_files):
        if not file_path.endswith(suffixes):
            continue
        facts = facts_of(file_path) or {}
        if facts.get("globals"):
            index.globals_of[file_path] = set(facts["globals"])
        for fact in facts.get("classes", []):
            name = fact.get("name")
            if not name:
                continue
            kept = {"bases": list(fact.get("bases", [])), "fields": dict(fact.get("fields", {})), "template": list(fact.get("template", []))}
            if fact.get("members"):
                index.class_members.setdefault(name, set()).update(fact["members"])
            definitions = index.class_defs.setdefault(name, [])
            if all(file != file_path for file, _facts in definitions):
                definitions.append((file_path, kept))
        for alias in facts.get("aliases", []):
            name, target = alias.get("name"), alias.get("target")
            if not name or not target:
                continue
            scope = name.rsplit("::", 1)[0] if "::" in name else ""
            definitions = index.alias_defs.setdefault(name, [])
            if all(file != file_path for file, _payload in definitions):
                definitions.append((file_path, (target, scope)))
        for qualified, flags in (facts.get("refparams") or {}).items():
            if index.ref_params.get(qualified, flags) != flags:
                clashing_refs.add(qualified)
            index.ref_params[qualified] = flags
        for qualified, written in (facts.get("returns") or {}).items():
            if index.returns.get(qualified, written) != written:
                clashing_returns.add(qualified)
            index.returns[qualified] = written
    for name, definitions in index.class_defs.items():
        if all(facts == definitions[0][1] for _file, facts in definitions):
            index.class_facts[name] = definitions[0][1]  # every definition says the same: one class
        else:
            clashing.add(name)
            index.dropped_classes.add(name)  # the lookup picks the definition the file can see (`_pick`)
    for name, definitions in index.alias_defs.items():
        if all(payload == definitions[0][1] for _file, payload in definitions):
            index.aliases[name] = definitions[0][1]
    for name in clashing:
        index.class_facts.pop(name, None)
    for qualified in clashing_returns:
        index.returns.pop(qualified, None)
    for qualified in clashing_refs:
        index.ref_params.pop(qualified, None)
    # (an alias that two files define differently is found by `_pick`, not by its name alone)


def _is_template_parameter(name: str) -> bool:
    """A bare one-word name that is a template parameter in use (`T`): no way to tell what it is, so not an external type."""
    return len(name) == 1 and name.isupper()


@dataclass(frozen=True)
class TypeRef:
    """What a receiver is: a class (qualified) with its template arguments, or an array of it."""

    name: str
    args: Tuple["TypeRef", ...] = ()
    array: bool = False
    external: bool = False
    """A declared type that is not a class of the project (`std::string`, a library's): the receiver's type is known, so a
    method of that name in the project is not what is called."""


_MAX_DEPTH = 8


def _scopes(caller_id: str) -> List[str]:
    """The caller's class and enclosing namespaces, innermost first, then the global scope (empty)."""
    parts = qualified_name(caller_id).split("::")[:-1]
    return ["::".join(parts[:count]) for count in range(len(parts), 0, -1)] + [""]


def _join(scope: str, name: str) -> str:
    return f"{scope}::{name}" if scope else name


def _folders_in_common(first: str, second: str) -> int:
    """How many leading folders two file paths share."""
    a, b = posixpath.dirname(first).split("/"), posixpath.dirname(second).split("/")
    count = 0
    for x, y in zip(a, b):
        if x != y:
            break
        count += 1
    return count


def _pick(candidates: List[Tuple[str, object]], visible: Set[str], view_file: str) -> Optional[object]:
    """The one definition of a name that the file `view_file` can see: the only one, else the one in a file it includes (`visible`),
    else the one that shares the most folders with it (its own folder, then the package root). `None` when none or several are
    the one: the name is ambiguous for that file."""
    if len(candidates) == 1:
        return candidates[0][1]
    hits = [payload for file, payload in candidates if file in visible]
    if len(hits) == 1:
        return hits[0]
    if hits:
        return None
    best, winners = -1, []
    for file, payload in candidates:
        shared = _folders_in_common(file, view_file)
        if shared > best:
            best, winners = shared, [payload]
        elif shared == best:
            winners.append(payload)
    return winners[0] if len(winners) == 1 else None


def _targets(function_ids: List[str]) -> List[Tuple[str, str]]:
    if len(function_ids) == 1:
        return [(function_ids[0], "EXTRACTED")]
    return [(function_id, "AMBIGUOUS") for function_id in function_ids]


@dataclass(frozen=True)
class MemberAccess:
    """One read or write of a member as the parser found it (`parsers.models.RawMemberAccess`)."""

    receiver: Optional[str]
    member: str
    kind: str
    line: int
    text: str
    call_receiver: Optional[str] = None
    call_name: Optional[str] = None
    arg_index: int = -1
    written_through: str = ""


@dataclass(frozen=True)
class ResolvedMember:
    """A member access with its class: `owner_class` is the class that declares the member (a base's when it is inherited), or
    `""` when the receiver's type is not known (the access is unresolved; `receiver` is what it was taken from)."""

    owner_class: str
    member: str
    kind: str
    line: int
    site: str
    receiver: str = ""
    written_through: str = ""


def resolve_cpp_calls(
    caller_id: str,
    caller_file: str,
    call_sites: List[CallSite],
    declared_types: Dict[str, str],
    index: CppIndex,
    caller_class: Optional[str] = None,
    infer_by_name: bool = False,
    member_accesses: Optional[List[MemberAccess]] = None,
    members_out: Optional[List[ResolvedMember]] = None,
) -> List[ResolvedCall]:
    """Resolve every call of one C++ function. `caller_class` is the enclosing class when it is a method. With
    `infer_by_name`, a member call whose receiver has no type that can be worked out gets an `INFERRED` edge to the
    one method of that name when the project defines exactly one (never to several, never to a free function).

    With `member_accesses`, the member accesses of the same function are resolved by the same typing (declared types of
    variables, parameters, fields and return types) and appended to `members_out`: a receiver of a project class gives the
    class that declares the member (a base's when it is inherited); a receiver whose type cannot be worked out, or a class that
    has no such field, gives an unresolved access (no class, the member name kept); a receiver of a type the project does not
    define (`std::pair`, a library's), a name that is a local or a parameter, and a bare name that is no field are not stored."""
    visible = index.visible_files(caller_file)
    scopes = _scopes(caller_id)

    def class_facts_of(name: str) -> Optional[dict]:
        """The facts of a class as the caller's file sees it: its one definition, or the one that `_pick` finds for the file."""
        if name in index.class_facts:
            return index.class_facts[name]
        found = _pick(index.class_defs.get(name, []), visible, caller_file)
        return found if isinstance(found, dict) else None

    def alias_of_key(key: str) -> Optional[Tuple[str, str]]:
        """`(target as written, scope)` of an alias by its qualified name, as the caller's file sees it."""
        if key in index.aliases:
            return index.aliases[key]
        return _pick(index.alias_defs.get(key, []), visible, caller_file)  # type: ignore[return-value]

    def ambiguous_name(name: str, within: List[str]) -> bool:
        """A name that two files define differently, and the caller's file cannot tell which one it means."""
        for scope in within:
            key = _join(scope, name)
            if key in index.class_defs and class_facts_of(key) is None and key not in index.classes:
                return True
            if key in index.alias_defs and alias_of_key(key) is None:
                return True
        return False

    def seen(function_ids: List[str]) -> List[str]:
        return [
            function_id
            for function_id in function_ids
            if index.file_of.get(function_id) in visible or index.declared_in.get(function_id, set()) & visible
        ]

    def lookup(written: str) -> List[Tuple[str, str]]:
        for scope in scopes:
            found = seen(index.by_qualified.get(_join(scope, written), []))
            if not found:  # an anonymous namespace has internal linkage: only its own file can call into it
                found = [i for i in index.anonymous.get(_join(scope, written), []) if index.file_of.get(i) == caller_file]
            if found:
                return _targets(found)
        return []

    def class_of(type_name: str) -> Optional[str]:
        written = parse_type(type_name)[0]  # (a declared type carries its template arguments)
        rooted = written.lstrip(":") if written.startswith("::") else None
        bare = written
        for scope in ([""] if rooted else scopes):
            candidate = _join(scope, rooted or bare)
            if candidate in index.classes or class_facts_of(candidate) is not None:
                return candidate
        return None

    # --- the types of receivers, from what is declared (step 5) -------------------------------------------------------

    def class_scopes(class_name: str) -> List[str]:
        parts = class_name.split("::")
        return ["::".join(parts[:count]) for count in range(len(parts), 0, -1)] + [""]

    def known_class(name: str, within: List[str]) -> Optional[str]:
        rooted = name.startswith("::")
        for scope in ([""] if rooted else within):
            candidate = _join(scope, name.lstrip(":") if rooted else name)
            if class_facts_of(candidate) is not None or candidate in index.classes:
                return candidate
        return None

    def alias_of(name: str, within: List[str]) -> Optional[Tuple[str, List[str]]]:
        """The type a name declared as an alias (`using X = Base<Arg>;`, `typedef Base<Arg> X;`) stands for as written, and the scopes
        that text is written in (the alias's own namespace or class)."""
        for scope in within:
            found = alias_of_key(_join(scope, name))
            if found is not None:
                target, alias_scope = found
                return target, class_scopes(alias_scope) if alias_scope else [""]
        return None

    def type_ref(text: str, within: List[str], bindings: Dict[str, TypeRef], hops: int = 0) -> Optional[TypeRef]:
        name, args, array = parse_type(pointee(text))
        if name in bindings and not args:
            bound = bindings[name]
            return TypeRef(bound.name, bound.args, array or bound.array)
        found = known_class(name, within)
        if found is None:
            plain = name[5:] if name.startswith("std::") else name
            if plain in CONTAINERS:
                found = name
            else:
                aliased = alias_of(name, within) if hops < _MAX_DEPTH else None
                if aliased is None:
                    return None
                target, target_within = aliased  # an alias is the type it names: `AliasBase* p;` is `Base<Arg>`
                resolved = type_ref(target, target_within, {}, hops + 1)
                return TypeRef(resolved.name, resolved.args, array or resolved.array, resolved.external) if resolved is not None else None
        return TypeRef(found, tuple(type_ref(argument, within, bindings) or TypeRef(argument) for argument in args), array)

    def declared_type(written: str, within: List[str], bindings: Dict[str, TypeRef]) -> Optional[TypeRef]:
        """The type a declaration writes: a class of the project, or `external` when it names none (`std::string`, a library's);
        `None` for a template parameter nothing binds (it could be anything)."""
        written = pointee(written)
        if parse_type(written)[0] in POINTER_WRAPPERS:
            return None  # a smart pointer that says nothing of what it points to: its type is not known
        ref = type_ref(written, within, bindings)
        name = parse_type(written)[0]
        if ref is None and not _is_template_parameter(name) and name not in template_names(within):
            if ambiguous_name(name, within):
                return None  # two files define it differently and this one cannot tell which: unknown, not a library's type
            return TypeRef(name, external=True)  # (a template parameter of a class in scope that nothing binds is unknown, not a library type)
        return ref

    def element_of(ref: Optional[TypeRef]) -> Optional[TypeRef]:
        if ref is None or ref.external:
            return None
        if ref.array:
            return TypeRef(ref.name, ref.args, False)
        plain = ref.name[5:] if ref.name.startswith("std::") else ref.name
        if plain in CONTAINERS and ref.args:
            if class_facts_of(ref.args[0].name) is not None or ref.args[0].name in index.classes:
                return ref.args[0]
            return TypeRef(ref.args[0].name, external=True)  # the elements are of a type the project does not define
        return None

    def template_names(within: List[str]) -> Set[str]:
        """The template parameters of the classes in `within`, bound or not."""
        names: Set[str] = set()
        for scope in within:
            names.update((class_facts_of(scope) or {}).get("template", []))
        return names

    def base_target(written: str, within: List[str], hops: int = 0) -> Optional[Tuple[str, List[str], List[str]]]:
        """`(class, its template arguments as written, the scopes they are written in)` of a base written as `written`: a class of the
        project, or an alias of one (`using X = Base<Arg>;`), followed to what it names. A cycle, or a name that is not a project class
        (a library's), is `None`."""
        name, args, _array = parse_type(written)
        found = known_class(name, within)
        if found is not None:
            return found, args, within
        if hops >= _MAX_DEPTH:
            return None
        aliased = alias_of(name, within)
        if aliased is None:
            return None
        target, target_within = aliased
        return base_target(target, target_within, hops + 1)

    def base_bindings(derived: str, base: str, args: List[str], args_within: List[str], derived_bindings: Dict[str, TypeRef]) -> Dict[str, TypeRef]:
        """The template parameters of a base, bound to the arguments the derived class gives it (`class N : public Base<Context>`:
        `Base`'s `Ctx` is `Context`; `class N : public Alias` with `using Alias = Base<Context>;` the same). An argument that is a
        parameter of the derived class and is not bound here stays unbound: a member typed with it is unknown, not a library type."""
        params = (class_facts_of(base) or {}).get("template", [])
        within = args_within
        own = template_names(class_scopes(derived))
        bound: Dict[str, TypeRef] = {}
        for param, argument in zip(params, args):
            ref = type_ref(argument, within, derived_bindings)
            if ref is None:
                if _is_template_parameter(argument) or argument in own:
                    continue
                ref = TypeRef(argument, external=True)
            bound[param] = ref
        return bound

    def bases_of(class_name: str) -> List[str]:
        facts = class_facts_of(class_name)
        found = []
        for written in (facts["bases"] if facts else []):
            resolved = base_target(written, class_scopes(class_name))
            if resolved is not None and resolved[0] != class_name:
                found.append(resolved[0])
        return found

    def bindings_for(class_name: str, ref: Optional[TypeRef]) -> Dict[str, TypeRef]:
        facts = class_facts_of(class_name)
        if not facts or ref is None or ref.name != class_name:
            return {}
        return {parameter: argument for parameter, argument in zip(facts["template"], ref.args) if class_facts_of(argument.name) is not None or argument.name in index.classes}

    def find_methods(class_name: str, method: str, depth: int = 0, visited: Optional[Set[str]] = None) -> Optional[Tuple[str, List[str]]]:
        """The class (this one, else the first base that has it) that declares `method`, and its function ids."""
        visited = visited if visited is not None else set()
        if class_name in visited or depth > _MAX_DEPTH:
            return None
        visited.add(class_name)
        found = seen(index.by_qualified.get(_join(class_name, method), []))
        if found:
            return class_name, found
        for base in bases_of(class_name):
            result = find_methods(base, method, depth + 1, visited)
            if result is not None:
                return result
        return None

    def find_field(class_name: str, name: str, bindings: Optional[Dict[str, TypeRef]] = None, depth: int = 0, visited: Optional[Set[str]] = None) -> Optional[Tuple[str, str, Dict[str, TypeRef]]]:
        """The class (this one, else the first base that has it) that declares the field `name`, its written type, and the template
        parameters of that class bound to what the lookup started from (through every base on the way)."""
        visited = visited if visited is not None else set()
        if class_name in visited or depth > _MAX_DEPTH:
            return None
        visited.add(class_name)
        bound = bindings or {}
        facts = class_facts_of(class_name)
        if facts and name in facts["fields"]:
            return class_name, facts["fields"][name], bound
        for written in (facts["bases"] if facts else []):
            resolved = base_target(written, class_scopes(class_name))
            if resolved is None or resolved[0] == class_name:
                continue
            base, args, args_within = resolved
            result = find_field(base, name, base_bindings(class_name, base, args, args_within, bound), depth + 1, visited)
            if result is not None:
                return result
        return None

    def find_member(class_name: str, name: str, depth: int = 0, visited: Optional[Set[str]] = None) -> Optional[str]:
        """The class (this one, else the first base) that declares the data member `name`, of any type."""
        visited = visited if visited is not None else set()
        if class_name in visited or depth > _MAX_DEPTH:
            return None
        visited.add(class_name)
        if name in index.class_members.get(class_name, ()) or name in (class_facts_of(class_name) or {}).get("fields", {}):
            return class_name
        for base in bases_of(class_name):
            result = find_member(base, name, depth + 1, visited)
            if result is not None:
                return result
        return None

    def declared(name: str, depth: int) -> Optional[TypeRef]:
        written = declared_types.get(name)
        if written is not None:
            if written.startswith("="):
                return evaluate(written[1:], depth + 1)
            if written.startswith("@"):
                return element_of(evaluate(written[1:], depth + 1))
            return declared_type(written, scopes, {})
        if name == "this":
            return TypeRef(caller_class) if caller_class else None
        if caller_class:
            field_found = find_field(caller_class, name)
            if field_found is not None:
                owner, written_type, found_bindings = field_found
                return declared_type(written_type, class_scopes(owner), found_bindings)
        return None

    def result_of_function(name: str) -> Optional[TypeRef]:
        """The declared type of what a call by a bare (or scope-qualified) name returns; a class name called is that class."""
        found = lookup(name)
        if not found and caller_class and "::" not in name:
            inherited = find_methods(caller_class, name)
            found = _targets(inherited[1]) if inherited else []
        if found:
            qualified = {qualified_name(function_id) for function_id, _tier in found}
            if len(qualified) != 1:
                return None
            written = index.returns.get(next(iter(qualified)))
            if written is None:
                return None
            owner_scopes = _scopes(found[0][0])
            return declared_type(written, owner_scopes, {})
        constructed = known_class(name, scopes)
        return TypeRef(constructed) if constructed else None

    def result_of_method(owner: TypeRef, name: str) -> Optional[TypeRef]:
        found = find_methods(owner.name, name)
        if found is None:
            return None
        declaring, _ids = found
        written = index.returns.get(_join(declaring, name))
        if written is None:
            return None
        return declared_type(written, class_scopes(declaring), bindings_for(declaring, owner))

    def type_of_field(owner: TypeRef, name: str) -> Optional[TypeRef]:
        found = find_field(owner.name, name, bindings_for(owner.name, owner))
        if found is None:
            return None
        declaring, written, found_bindings = found
        return declared_type(written, class_scopes(declaring), found_bindings)

    def evaluate(expression: str, depth: int = 0) -> Optional[TypeRef]:
        """The type of a receiver expression (`cpp_types.split_chain`), from declared types only."""
        if depth > _MAX_DEPTH:
            return None
        made = MAKE_POINTER.fullmatch(expression)
        if made:  # `std::make_shared<T>()`: a `T` that the pointer holds
            return declared_type(made.group(1), scopes, {})
        steps = split_chain(expression)
        if not steps:
            return None
        current: Optional[TypeRef] = None
        for position, (name, operations) in enumerate(steps):
            rest = list(operations)
            if position == 0:
                if "()" in rest:
                    current = result_of_function(name)
                    rest = rest[rest.index("()") + 1 :]
                else:
                    current = declared(name, depth)
            elif current is None:
                return None
            elif "()" in rest:
                current = result_of_method(current, name)
                rest = rest[rest.index("()") + 1 :]
            else:
                current = type_of_field(current, name)
            for operation in rest:
                current = element_of(current) if operation == "[]" else None
            if current is None:
                return None
        return current

    def through_the_type(call: CallSite) -> List[Tuple[str, str]]:
        """The method a call names, on the class its receiver is declared to be (or one of that class's bases)."""
        owner = evaluate(call.receiver) if call.receiver else None
        if owner is None:
            return by_unique_name(call) if infer_by_name else []
        if owner.external:
            return []  # the receiver is of a declared type the project does not define: not a method of the project
        found = find_methods(owner.name, call.name)
        return _targets(found[1]) if found else []

    def by_unique_name(call: CallSite) -> List[Tuple[str, str]]:
        """The receiver's type is not known: the one method of that name in the project, as `INFERRED`, or nothing."""
        if call.name.startswith(("operator", "~")):
            return []
        steps = split_chain(call.receiver or "")
        if steps and any(steps[0][0] in index.globals_of.get(file, ()) for file in visible):
            return []  # it names a variable declared at file level, whose type is written there: not a type that cannot be found
        candidates = index.methods_by_name.get(call.name, [])
        return [(candidates[0], "INFERRED")] if len(candidates) == 1 else []

    by_callee: Dict[str, List[str]] = {}
    confidence: Dict[str, str] = {}
    for call in call_sites:
        if call.receiver is None:
            found = lookup(call.name)
            if not found and caller_class:  # a method of a base class, called by its bare name
                inherited = find_methods(caller_class, call.name)
                found = _targets(inherited[1]) if inherited else []
        elif call.text.startswith(call.receiver + "::"):
            found = lookup(f"{call.receiver}::{call.name}")
        elif call.receiver == "this":
            found = _targets(seen(index.by_qualified.get(_join(caller_class, call.name), []))) if caller_class else []
            if not found:
                found = through_the_type(call)
        else:
            type_name = declared_types.get(call.receiver)
            owner = class_of(type_name) if type_name and not type_name.startswith(("=", "@")) else None
            found = _targets(seen(index.by_qualified.get(_join(owner, call.name), []))) if owner else []
            if not found:
                found = through_the_type(call)
        for callee_id, tier in found:
            by_callee.setdefault(callee_id, []).append(call.text)
            if callee_id not in confidence or _RANK[tier] > _RANK[confidence[callee_id]]:
                confidence[callee_id] = tier
    def reference_flags(call_receiver: Optional[str], call_name: str) -> Optional[str]:
        """The flags of the non-const reference parameters of the function a call names (found as a call is), when every candidate says the same."""
        if call_receiver is None:
            found_ids = [function_id for function_id, _tier in lookup(call_name)]
            if not found_ids and caller_class:
                inherited = find_methods(caller_class, call_name)
                found_ids = inherited[1] if inherited else []
        elif call_receiver == "this":
            inherited = find_methods(caller_class, call_name) if caller_class else None
            found_ids = inherited[1] if inherited else []
        else:
            qualified_scope = lookup(f"{call_receiver}::{call_name}")
            if qualified_scope:
                found_ids = [function_id for function_id, _tier in qualified_scope]
            else:
                owner = evaluate(call_receiver)
                inherited = find_methods(owner.name, call_name) if owner is not None and not owner.external else None
                found_ids = inherited[1] if inherited else []
        flags = {index.ref_params.get(qualified_name(function_id)) for function_id in found_ids}
        return flags.pop() if len(flags) == 1 else None

    if member_accesses and members_out is not None:
        members_out.extend(
            _resolve_members(member_accesses, declared_types, caller_class, evaluate, find_member, lambda name: class_facts_of(name) is not None or name in index.class_members, reference_flags)
        )
    return [ResolvedCall(callee_id=callee, call_site="; ".join(sites), confidence=confidence[callee]) for callee, sites in by_callee.items()]


def _resolve_members(
    accesses: List[MemberAccess],
    declared_types: Dict[str, str],
    caller_class: Optional[str],
    evaluate: Callable[..., Optional[TypeRef]],
    find_member: Callable[..., Optional[str]],
    is_project_class: Callable[[str], bool],
    reference_flags: Callable[[Optional[str], str], Optional[str]],
) -> List[ResolvedMember]:
    found: List[ResolvedMember] = []

    def kind_of(access: MemberAccess) -> str:
        """A read passed to a parameter the callee takes as a non-const reference (`void fill(int& out)`) is read and written."""
        if access.kind == "read" and access.call_name is not None and access.arg_index >= 0:
            flags = reference_flags(access.call_receiver, access.call_name)
            if flags and access.arg_index < len(flags) and flags[access.arg_index] == "1":
                return "readwrite"
        return access.kind

    for access in accesses:
        if access.receiver is None:  # a bare name: the data member of the enclosing class, if it is one (a local is not)
            if access.member in declared_types or not caller_class:
                continue
            declaring = find_member(caller_class, access.member)
            if declaring is not None:
                found.append(ResolvedMember(declaring, access.member, kind_of(access), access.line, access.text, "", access.written_through))
            continue
        owner = evaluate(access.receiver) if access.receiver else None
        if owner is None:
            found.append(ResolvedMember("", access.member, access.kind, access.line, access.text, access.receiver, access.written_through))
            continue
        if owner.external or not is_project_class(owner.name):
            continue  # a member of a type the project does not define (a library's, a standard container)
        declaring = find_member(owner.name, access.member)
        if declaring is None:
            found.append(ResolvedMember("", access.member, access.kind, access.line, access.text, access.receiver, access.written_through))
        else:
            found.append(ResolvedMember(declaring, access.member, kind_of(access), access.line, access.text, "", access.written_through))
    return found
