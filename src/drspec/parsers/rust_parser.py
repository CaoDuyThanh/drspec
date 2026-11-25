"""Rust source code parser using Tree-sitter.

Functions are the `fn` items: free functions, methods and associated functions of an `impl` block
(inherent or for a trait), default methods of a trait, functions nested in another function, and
`macro_rules!` definitions (named `name!`). Closures are not functions of their own: their calls belong to
the function around them.

The qualified name is the path inside the file: inline `mod` blocks, then the type, then the function
(`net::Server::start`); a method of a trait implementation carries the trait so that an inherent method
and a trait method of one name never collide (`Server::<Display>::fmt`). The file's own place in the
crate comes from the file path and `mod name;` declarations, which the resolver follows.

The calls inside `println!(...)`, `assert_eq!(...)`, `vec![...]` and other macros are read by parsing the
macro's arguments again as expressions, since the grammar leaves them as plain tokens.
"""

from __future__ import annotations

from typing import Dict, List, Optional

import tree_sitter_rust as tsrust
from tree_sitter import Language, Node, Parser

from drspec.parsers.models import ExtractedFunction, ParseError, ParseResult, RawCall
from drspec.parsers.parameters import normalise_type, rust_parameter_tokens

# Macros of the standard library and of the common ecosystem crates: no edge to them, but their arguments are code.
_MACRO_CACHE_SIZE = 20000  # macro texts kept (their calls) so that a text is parsed once; cleared when full

STANDARD_MACROS = frozenset({
    "println", "print", "eprintln", "eprint", "format", "write", "writeln", "panic", "assert", "assert_eq", "assert_ne",
    "debug_assert", "debug_assert_eq", "debug_assert_ne", "vec", "matches", "todo", "unimplemented", "unreachable",
    "dbg", "format_args", "include_str", "include_bytes", "concat", "stringify", "env", "option_env", "cfg", "line",
    "file", "column", "module_path", "compile_error", "thread_local", "try", "json", "bail", "ensure", "anyhow",
    "info", "warn", "error", "debug", "trace", "lazy_static", "select",
})
# Wrapper types a receiver is seen through: `Box<T>`, `Rc<T>`, `Arc<T>`, `Option<T>`, `RefCell<T>`.
_WRAPPERS = frozenset({"Box", "Rc", "Arc", "Option", "RefCell", "Cell", "Mutex", "RwLock", "Cow"})
# Traits of the standard library that say nothing about which project method is called.
_STD_BOUNDS = frozenset({
    "Clone", "Copy", "Debug", "Display", "Send", "Sync", "Sized", "Default", "PartialEq", "Eq", "Hash", "Ord", "PartialOrd",
    "Into", "From", "AsRef", "Iterator", "IntoIterator", "Fn", "FnMut", "FnOnce", "ToString", "Drop", "Deref", "Unpin",
})
_MAX_MACRO_DEPTH = 3

# What a macro's tokens may hold for the calls in them to be read straight from the tokens (see `_fast_macro_calls`).
_PATH_PARTS = frozenset({"identifier", "self", "crate", "super"})
_OPERANDS = _PATH_PARTS | {"integer_literal", "float_literal", "string_literal", "raw_string_literal", "char_literal", "boolean_literal", "primitive_type"}
_PLAIN_PUNCTUATION = frozenset({
    ".", "::", ",", "+", "-", "*", "/", "%", "&", "&&", "|", "||", "^", "!", "<", ">", "<=", ">=", "==", "!=", "=", "+=", "-=",
    "*=", "/=", "%=", "^=", "&=", "|=", "<<", ">>", "..", "..=", "?",
})


class _NotPlain(Exception):
    """The tokens of a macro are not a plain expression: its text is parsed instead."""


class RustParser:
    """Parser for Rust source code using Tree-sitter."""

    def __init__(self) -> None:
        self._macro_cache: Dict[tuple, List[RawCall]] = {}
        self._source = b""  # the text of the tree being read (a call's text is cut from it)
        grammar = tsrust.language()
        try:
            self._language = Language(grammar, "rust")
            self._parser = Parser()
            self._parser.set_language(self._language)
        except TypeError:
            self._language = Language(grammar)
            self._parser = Parser()
            self._parser.language = self._language

    def parse(self, source_code: str, file_path: Optional[str] = None) -> ParseResult:
        self._source = bytes(source_code, "utf8")
        tree = self._parser.parse(self._source)
        result = ParseResult(file_path=file_path)
        if tree.root_node.has_error:
            result.has_errors = True
            self._collect_errors(tree.root_node, result.errors)
        self._facts: dict = {"uses": [], "mods": [], "types": [], "impls": [], "traits": []}
        raw: List[ExtractedFunction] = []
        self._walk_items(tree.root_node, "", False, raw, {})
        seen: Dict[tuple, ExtractedFunction] = {}
        for func in raw:  # `#[cfg]` variants of one function are one entry (the last wins); the same method in two `impl` headers are two
            seen[(func.qualified_name, tuple(func.parameter_tokens), func.discriminator)] = func
        result.functions = list(seen.values())
        result.facts = self._facts
        return result

    def parse_file(self, file_path: str) -> ParseResult:
        with open(file_path, "r", encoding="utf-8") as handle:
            return self.parse(handle.read(), file_path=file_path)

    # ---- items

    def _walk_items(self, node: Node, mod_path: str, in_test: bool, out: List[ExtractedFunction], bounds: Dict[str, str]) -> None:
        attributes: List[str] = []
        for child in node.children:
            kind = child.type
            if kind == "attribute_item":
                attributes.append(self._attribute_text(child))
                continue
            if kind == "function_item":
                self._function(child, mod_path, None, None, in_test or self._is_test(attributes), attributes, out, bounds)
            elif kind == "mod_item":
                self._module(child, mod_path, in_test, attributes, out)
            elif kind == "impl_item":
                self._impl(child, mod_path, in_test or self._is_cfg_test(attributes), out)
            elif kind == "trait_item":
                self._trait(child, mod_path, in_test, out)
            elif kind in ("struct_item", "enum_item", "union_item", "type_item"):
                name = child.child_by_field_name("name")
                if name is not None:
                    self._facts["types"].append({"name": name.text.decode("utf8"), "mod": mod_path, "kind": kind.split("_")[0]})
            elif kind == "macro_definition":
                self._macro_definition(child, mod_path, in_test, out)
            elif kind == "use_declaration":
                self._use(child, mod_path)
            if kind not in ("line_comment", "block_comment", "attribute_item"):
                attributes = []

    @staticmethod
    def _attribute_text(node: Node) -> str:
        inner = next((c for c in node.children if c.type == "attribute"), None)
        return " ".join((inner or node).text.decode("utf8").split())

    @staticmethod
    def _is_test(attributes: List[str]) -> bool:
        return any(a == "test" or a.endswith("::test") or a.startswith("tokio::test") for a in attributes)

    @staticmethod
    def _is_cfg_test(attributes: List[str]) -> bool:
        return any(a.replace(" ", "") == "cfg(test)" for a in attributes)

    def _module(self, node: Node, parent: str, in_test: bool, attributes: List[str], out: List[ExtractedFunction]) -> None:
        name_node = node.child_by_field_name("name")
        if name_node is None:
            return
        name = name_node.text.decode("utf8")
        body = node.child_by_field_name("body")
        path_attr = next((a for a in attributes if a.startswith("path")), None)
        explicit = None
        if path_attr and '"' in path_attr:
            explicit = path_attr.split('"')[1]
        self._facts["mods"].append({"name": name, "mod": parent, "inline": body is not None, "path": explicit})
        if body is not None:
            self._walk_items(body, f"{parent}::{name}" if parent else name, in_test or self._is_cfg_test(attributes), out, {})

    def _impl(self, node: Node, mod_path: str, in_test: bool, out: List[ExtractedFunction]) -> None:
        type_node = node.child_by_field_name("type")
        body = node.child_by_field_name("body")
        if type_node is None or body is None:
            return
        type_name = self._type_name(type_node) or normalise_type(type_node.text.decode("utf8"))
        trait_node = node.child_by_field_name("trait")
        trait_name = self._type_name(trait_node, last_segment=True) if trait_node is not None else None
        bounds = self._bounds(node)
        owner = f"{type_name}::<{trait_name}>" if trait_name else type_name
        header = " ".join(type_node.text.decode("utf8").split())
        if trait_node is not None:
            header = " ".join(trait_node.text.decode("utf8").split()) + " for " + header
        methods: List[str] = []
        attributes: List[str] = []
        for child in body.children:
            if child.type == "attribute_item":
                attributes.append(self._attribute_text(child))
                continue
            if child.type == "function_item":
                func = self._function(child, mod_path, owner, type_name, in_test or self._is_test(attributes), attributes, out, bounds)
                if func is not None:
                    func.discriminator = header
                    methods.append(func.qualified_name)
            attributes = []
        self._facts["impls"].append({"mod": mod_path, "type": type_name, "type_path": self._type_path(type_node), "trait": trait_name, "methods": methods})

    def _trait(self, node: Node, mod_path: str, in_test: bool, out: List[ExtractedFunction]) -> None:
        name_node = node.child_by_field_name("name")
        body = node.child_by_field_name("body")
        if name_node is None or body is None:
            return
        name = name_node.text.decode("utf8")
        bounds = self._bounds(node)
        methods: List[str] = []
        attributes: List[str] = []
        for child in body.children:
            if child.type == "attribute_item":
                attributes.append(self._attribute_text(child))
                continue
            if child.type in ("function_item", "function_signature_item"):
                fn_name = child.child_by_field_name("name")
                if fn_name is not None:
                    methods.append(fn_name.text.decode("utf8"))
                if child.type == "function_item":
                    self._function(child, mod_path, name, name, in_test, attributes, out, bounds)
            attributes = []
        self._facts["traits"].append({"mod": mod_path, "name": name, "methods": methods})
        self._facts["types"].append({"name": name, "mod": mod_path, "kind": "trait"})

    def _macro_definition(self, node: Node, mod_path: str, in_test: bool, out: List[ExtractedFunction]) -> None:
        name_node = node.child_by_field_name("name") or next((c for c in node.children if c.type == "identifier"), None)
        if name_node is None:
            return
        name = name_node.text.decode("utf8") + "!"
        qualified = f"{mod_path}::{name}" if mod_path else name
        out.append(ExtractedFunction(
            name=name, qualified_name=qualified, signature=f"macro_rules! {name[:-1]}", body=node.text.decode("utf8"),
            start_line=node.start_point[0] + 1, end_line=node.end_point[0] + 1, parent=mod_path or None,
            decorators=["macro"] + (["test"] if in_test else []), has_error=node.has_error, parameter_tokens=[],
        ))

    # ---- use declarations

    def _use(self, node: Node, mod_path: str) -> None:
        is_pub = any(c.type == "visibility_modifier" for c in node.children)
        argument = node.child_by_field_name("argument") or next((c for c in node.children if c.is_named and c.type != "visibility_modifier"), None)
        if argument is not None:
            self._use_tree(argument, [], mod_path, is_pub)

    @staticmethod
    def _path_parts(node: Node) -> List[str]:
        if node.type == "scoped_identifier":
            path = node.child_by_field_name("path")
            name = node.child_by_field_name("name")
            return (RustParser._path_parts(path) if path is not None else []) + ([name.text.decode("utf8")] if name is not None else [])
        return [node.text.decode("utf8")]

    def _use_tree(self, node: Node, prefix: List[str], mod_path: str, is_pub: bool) -> None:
        uses = self._facts["uses"]
        kind = node.type
        if kind in ("identifier", "scoped_identifier", "crate", "self", "super"):
            parts = prefix + self._path_parts(node)
            if parts and parts[-1] == "self":
                parts = parts[:-1]
            if parts:
                uses.append({"mod": mod_path, "path": "::".join(parts), "alias": parts[-1], "pub": is_pub})
        elif kind == "use_as_clause":
            path = node.child_by_field_name("path")
            alias = node.child_by_field_name("alias")
            if path is not None and alias is not None:
                uses.append({"mod": mod_path, "path": "::".join(prefix + self._path_parts(path)), "alias": alias.text.decode("utf8"), "pub": is_pub})
        elif kind == "use_wildcard":
            path = next((c for c in node.children if c.is_named), None)
            parts = prefix + (self._path_parts(path) if path is not None else [])
            uses.append({"mod": mod_path, "path": "::".join(parts), "alias": "*", "pub": is_pub})
        elif kind == "scoped_use_list":
            path = node.child_by_field_name("path")
            lst = node.child_by_field_name("list")
            base = prefix + (self._path_parts(path) if path is not None else [])
            if lst is not None:
                for item in lst.children:
                    if item.is_named:
                        self._use_tree(item, base, mod_path, is_pub)
        elif kind == "use_list":
            for item in node.children:
                if item.is_named:
                    self._use_tree(item, prefix, mod_path, is_pub)

    # ---- functions

    def _function(
        self, node: Node, mod_path: str, owner: Optional[str], owner_type: Optional[str], in_test: bool, attributes: List[str],
        out: List[ExtractedFunction], outer_bounds: Dict[str, str], enclosing: Optional[str] = None,
    ) -> Optional[ExtractedFunction]:
        name_node = node.child_by_field_name("name")
        body = node.child_by_field_name("body")
        if name_node is None or body is None:
            return None
        name = name_node.text.decode("utf8")
        if enclosing:
            qualified = f"{enclosing}::{name}"
            parent: Optional[str] = enclosing
        else:
            prefix = "::".join(part for part in (mod_path, owner) if part)
            qualified = f"{prefix}::{name}" if prefix else name
            parent = prefix or None
        bounds = {**outer_bounds, **self._bounds(node)}
        modifiers = next((c for c in node.children if c.type == "function_modifiers"), None)
        is_async = modifiers is not None and any(c.type == "async" for c in modifiers.children)
        decorators = [a for a in attributes if a]
        if in_test and "test" not in decorators:
            decorators.append("test")
        if any(c.type == "visibility_modifier" for c in node.children):
            decorators.append("pub")
        signature = " ".join(node.text[: body.start_byte - node.start_byte].decode("utf8").split())
        calls: List[RawCall] = []
        self._collect_calls(body, calls, 0)
        func = ExtractedFunction(
            name=name, qualified_name=qualified, signature=signature, body=node.text.decode("utf8"),
            start_line=node.start_point[0] + 1, end_line=node.end_point[0] + 1, parent=parent,
            decorators=decorators, is_method=owner is not None and enclosing is None, is_async=is_async, calls=calls,
            parameter_tokens=rust_parameter_tokens(node), declared_param_types=self._declared_types(node, bounds),
            has_error=node.has_error,
        )
        out.append(func)
        self._nested(body, mod_path, in_test, out, bounds, qualified)
        return func

    def _nested(self, node: Node, mod_path: str, in_test: bool, out: List[ExtractedFunction], bounds: Dict[str, str], enclosing: str) -> None:
        for child in node.children:
            if child.type == "function_item":
                self._function(child, mod_path, None, None, in_test, [], out, bounds, enclosing=enclosing)
            elif child.type in ("impl_item", "trait_item"):
                # An `impl` or `trait` written inside a function (examples and tests do): its methods are indexed too. The
                # type it names is local to that function, so two functions may each define a `S` with the same
                # methods: the function is part of what tells them apart.
                before = len(out)
                (self._impl if child.type == "impl_item" else self._trait)(child, mod_path, in_test, out)
                for func in out[before:]:
                    func.discriminator = f"{func.discriminator} @ {enclosing}".strip()
            else:
                self._nested(child, mod_path, in_test, out, bounds, enclosing)

    # ---- types

    @staticmethod
    def _type_name(node: Optional[Node], last_segment: bool = True) -> Optional[str]:
        """The name of a type: `Foo` for `Foo`, `Foo<T>`, `&Foo`, `fmt::Display`; `None` for tuples, arrays and primitives."""
        if node is None:
            return None
        kind = node.type
        if kind in ("type_identifier", "identifier"):
            return node.text.decode("utf8")
        if kind == "generic_type":
            return RustParser._type_name(node.child_by_field_name("type"), last_segment)
        if kind == "scoped_type_identifier":
            name = node.child_by_field_name("name")
            return name.text.decode("utf8") if name is not None else None
        if kind in ("reference_type", "pointer_type"):
            return RustParser._type_name(node.child_by_field_name("type"), last_segment)
        return None

    @staticmethod
    def _type_path(node: Optional[Node]) -> str:
        """The type as a path (`net::Server`) with generics and references taken off."""
        if node is None:
            return ""
        if node.type == "generic_type":
            return RustParser._type_path(node.child_by_field_name("type"))
        if node.type in ("reference_type", "pointer_type"):
            return RustParser._type_path(node.child_by_field_name("type"))
        if node.type == "scoped_type_identifier":
            path = node.child_by_field_name("path")
            name = node.child_by_field_name("name")
            head = "::".join(RustParser._path_parts(path)) if path is not None and path.type in ("scoped_identifier", "identifier", "crate", "self", "super") else (RustParser._type_path(path) if path is not None else "")
            return f"{head}::{name.text.decode('utf8')}" if head and name is not None else (name.text.decode("utf8") if name is not None else "")
        if node.type in ("type_identifier", "identifier"):
            return node.text.decode("utf8")
        return ""

    def _receiver_type(self, node: Optional[Node], bounds: Dict[str, str]) -> Optional[str]:
        """The type whose methods a call on a value of this type reaches: references and smart pointers are
        looked through, `impl Trait` and `dyn Trait` are the trait, a generic parameter is its first project bound."""
        if node is None:
            return None
        kind = node.type
        if kind == "reference_type":
            return self._receiver_type(node.child_by_field_name("type"), bounds)
        if kind == "generic_type":
            base = self._type_name(node.child_by_field_name("type"))
            if base in _WRAPPERS:
                arguments = node.child_by_field_name("type_arguments")
                inner = next((c for c in (arguments.children if arguments is not None else []) if c.is_named and c.type != "lifetime"), None)
                return self._receiver_type(inner, bounds)
            return self._type_path(node) or None
        if kind in ("abstract_type", "dynamic_type"):
            trait = node.child_by_field_name("trait") or next((c for c in node.children if c.is_named and c.type not in ("lifetime",)), None)
            return self._type_path(trait.child_by_field_name("type") if trait is not None and trait.type == "generic_type" and False else trait) or self._type_name(trait)
        if kind == "type_identifier":
            name = node.text.decode("utf8")
            return bounds.get(name, name)
        if kind == "scoped_type_identifier":
            return self._type_path(node) or None
        return None

    def _bounds(self, node: Node) -> Dict[str, str]:
        """Generic parameter -> its first bound that is not a standard-library trait (`T: Shape + Clone` -> `Shape`)."""
        bounds: Dict[str, str] = {}

        def first_bound(trait_bounds: Node) -> Optional[str]:
            for item in trait_bounds.children:
                name = self._type_path(item) if item.type in ("type_identifier", "scoped_type_identifier", "generic_type") else None
                if name and name.split("::")[-1] not in _STD_BOUNDS:
                    return name
            return None

        parameters = node.child_by_field_name("type_parameters")
        for item in (parameters.children if parameters is not None else []):
            if item.type == "constrained_type_parameter":
                left = item.child_by_field_name("left")
                trait_bounds = item.child_by_field_name("bounds")
                found = first_bound(trait_bounds) if trait_bounds is not None else None
                if left is not None and found:
                    bounds[left.text.decode("utf8")] = found
        where = next((c for c in node.children if c.type == "where_clause"), None)
        for item in (where.children if where is not None else []):
            if item.type == "where_predicate":
                left = item.child_by_field_name("left")
                trait_bounds = item.child_by_field_name("bounds")
                found = first_bound(trait_bounds) if trait_bounds is not None else None
                if left is not None and found and left.type == "type_identifier":
                    bounds[left.text.decode("utf8")] = found
        return bounds

    def _declared_types(self, function_node: Node, bounds: Dict[str, str]) -> Dict[str, str]:
        types: Dict[str, str] = {}
        parameters = function_node.child_by_field_name("parameters")
        for param in (parameters.children if parameters is not None else []):
            if param.type != "parameter":
                continue
            pattern = param.child_by_field_name("pattern")
            if pattern is not None and pattern.type == "mut_pattern":
                pattern = next((c for c in pattern.children if c.type == "identifier"), None)
            type_name = self._receiver_type(param.child_by_field_name("type"), bounds)
            if pattern is not None and pattern.type == "identifier" and type_name:
                types[pattern.text.decode("utf8")] = type_name
        body = function_node.child_by_field_name("body")
        if body is not None:
            self._local_types(body, types, bounds)
        return types

    def _local_types(self, node: Node, types: Dict[str, str], bounds: Dict[str, str]) -> None:
        for child in node.children:
            if child.type == "function_item":
                continue
            if child.type == "let_declaration":
                pattern = child.child_by_field_name("pattern")
                if pattern is not None and pattern.type == "mut_pattern":
                    pattern = next((c for c in pattern.children if c.type == "identifier"), None)
                if pattern is not None and pattern.type == "identifier":
                    type_name = self._receiver_type(child.child_by_field_name("type"), bounds)
                    if type_name is None:
                        type_name = self._constructed_type(child.child_by_field_name("value"))
                    if type_name:
                        types[pattern.text.decode("utf8")] = type_name
            self._local_types(child, types, bounds)

    def _constructed_type(self, value: Optional[Node]) -> Optional[str]:
        """`Foo::new(..)`, `Foo::default()`, `Foo { .. }` and either with a trailing `?`/`.unwrap()` give `Foo`."""
        while value is not None and value.type in ("try_expression", "await_expression"):
            value = next((c for c in value.children if c.is_named), None)
        if value is None:
            return None
        if value.type == "struct_expression":
            return self._type_path(value.child_by_field_name("name")) or None
        if value.type == "call_expression":
            function = value.child_by_field_name("function")
            if function is not None and function.type == "scoped_identifier":
                path = function.child_by_field_name("path")
                if path is not None:
                    parts = self._path_parts(path)
                    if parts and parts[-1][:1].isupper() and parts[-1] != "Self":
                        return "::".join(parts)
        return None

    # ---- calls

    def _collect_calls(self, node: Node, calls: List[RawCall], depth: int) -> None:
        for child in node.children:
            kind = child.type
            if kind == "function_item":
                continue
            if kind == "call_expression":
                call = self._call(child)
                if call is not None:
                    calls.append(call)
            elif kind == "macro_invocation":
                self._macro_calls(child, calls, depth)
                continue
            self._collect_calls(child, calls, depth)

    def _call(self, node: Node) -> Optional[RawCall]:
        function = node.child_by_field_name("function")
        if function is None:
            return None
        if function.type == "generic_function":
            function = function.child_by_field_name("function")
            if function is None:
                return None
        text = node.text.decode("utf8")
        if function.type == "identifier":
            return RawCall(receiver=None, name=function.text.decode("utf8"), text=text)
        if function.type == "scoped_identifier":
            parts = self._path_parts(function)
            if len(parts) < 2:
                return None
            return RawCall(receiver="::".join(parts[:-1]), name=parts[-1], text=text, kind="path")
        if function.type == "field_expression":
            value = function.child_by_field_name("value")
            field = function.child_by_field_name("field")
            if value is not None and field is not None and value.type in ("identifier", "self") and field.type == "field_identifier":
                return RawCall(receiver=value.text.decode("utf8"), name=field.text.decode("utf8"), text=text, kind="method")
        return None

    def _macro_calls(self, node: Node, calls: List[RawCall], depth: int) -> None:
        name_node = node.child_by_field_name("macro")
        tokens = next((c for c in node.children if c.type == "token_tree"), None)
        if name_node is None:
            return
        name = name_node.text.decode("utf8")
        short = name.split("::")[-1]
        if short not in STANDARD_MACROS:
            calls.append(RawCall(receiver=None, name=f"{short}!", text=node.text.decode("utf8")[:200], kind="macro"))
        if tokens is None or depth >= _MAX_MACRO_DEPTH:
            return
        # What is written as a plain expression (`assert_eq!(a.len(), 1)`, `println!("{}", f(x))`, the most of them) is
        # read from the tokens the file's own parse made, without parsing the text again.
        plain = self._fast_macro_calls(tokens, depth)
        if plain is not None:
            calls.extend(plain)
            return
        inner = tokens.text.decode("utf8")[1:-1]
        # A call needs a parenthesis and a macro invocation a `!`: text with neither holds no call, and is not parsed.
        if "(" not in inner and "!" not in inner:
            return
        # The calls found do not depend on where the text is (a call carries no position), so the same text is
        # parsed once however often it is written (`assert_eq!(a.len(), 1)` is in every test).
        key = (depth, inner)
        cached = self._macro_cache.get(key)
        if cached is not None:
            calls.extend(cached)
            return
        found_calls: List[RawCall] = []
        self._parse_macro(inner, found_calls, depth)
        if len(self._macro_cache) >= _MACRO_CACHE_SIZE:
            self._macro_cache.clear()
        self._macro_cache[key] = found_calls
        calls.extend(found_calls)

    def _parse_macro(self, inner: str, calls: List[RawCall], depth: int) -> None:
        outer_source = self._source
        try:
            self._parse_macro_text(inner, calls, depth)
        finally:
            self._source = outer_source

    def _parse_macro_text(self, inner: str, calls: List[RawCall], depth: int) -> None:
        best: Optional[List[RawCall]] = None
        for template in ("fn __m() {{ __f({}); }}", "fn __m() {{ {} }}"):  # as arguments, then as statements
            self._source = template.format(inner).encode("utf8")
            tree = self._parser.parse(self._source)
            function = tree.root_node.children[0] if tree.root_node.children else None
            body = function.child_by_field_name("body") if function is not None and function.type == "function_item" else None
            if body is None:
                continue
            found: List[RawCall] = []
            self._collect_calls(body, found, depth + 1)
            found = [c for c in found if c.name != "__f"]
            if not tree.root_node.has_error:
                calls.extend(found)
                return
            if best is None:
                best = found
        calls.extend(best or [])

    def _fast_macro_calls(self, tokens: Node, depth: int) -> Optional[List[RawCall]]:
        """The calls in the tokens of a macro, read from the tokens, or `None` when they are not a plain expression.

        Plain is: names, literals, paths (`a::b`), fields and methods (`a.b(..)`), the usual operators, calls and indexes,
        nested macros, struct literals. Anything else (a keyword, `;` outside `[..; n]`, `=>`, a turbofish, a type
        written in the middle, two operands side by side as in a DSL) is left to parsing the text, which reads what a
        statement or a pattern means. The calls come in the order and with the text the parse gives."""
        found: List[RawCall] = []
        try:
            self._walk_tokens(tokens, depth, found)
        except _NotPlain:
            return None
        return found

    def _text_of(self, first: Node, last: Node) -> str:
        return self._source[first.start_byte : last.end_byte].decode("utf8")

    def _walk_tokens(self, tree: Node, depth: int, found: List[RawCall], named: bool = False) -> None:
        opener = tree.children[0].type if tree.children else ""
        kids = tree.children[1:-1]
        previous: Optional[str] = None  # the kind of the token before: what a token may follow
        before: Optional[str] = None  # and the one before that
        skip = False
        for index, kid in enumerate(kids):
            if skip:
                skip = False
                before, previous = previous, "token_tree"
                continue
            kind = kid.type
            if kind in _OPERANDS:
                if previous in _OPERANDS or previous == "token_tree":
                    raise _NotPlain  # two operands side by side: not an expression
                if kind == "float_literal" and kid.text.endswith(b"."):
                    raise _NotPlain  # `1.max(2)`: how it is split is the grammar's to say
                if kind == "primitive_type" and previous not in ("as", "::", "<", None, "(", ",", "&", "mutable_specifier"):
                    raise _NotPlain
            elif kind == "token_tree":
                head = kid.children[0].type if kid.children else ""
                if head == "(" and previous in _PATH_PARTS:
                    self._call_before(kids, index, kid, found)
                if head not in ("(", "[", "{"):
                    raise _NotPlain
                if previous == "token_tree" and head == "{":
                    raise _NotPlain
                self._walk_tokens(kid, depth, found, named=head == "{" and previous in _PATH_PARTS)
            elif kind == "!":
                following = kids[index + 1] if index + 1 < len(kids) else None
                if previous in _PATH_PARTS:
                    if following is None or following.type != "token_tree":
                        raise _NotPlain  # `a ! b`
                    self._macro_before(kids, index, following, depth, found)
                    skip = True
                    before, previous = previous, "!"
                    continue
            elif kind == "." :
                following = kids[index + 1] if index + 1 < len(kids) else None
                if following is None or following.type not in ("identifier", "await"):
                    raise _NotPlain
            elif kind == "::":
                following = kids[index + 1] if index + 1 < len(kids) else None
                if previous not in _PATH_PARTS or following is None or following.type not in _PATH_PARTS:
                    raise _NotPlain  # a turbofish, a path from the root, `<T as Trait>::f`
            elif kind == ";":
                if opener != "[":
                    raise _NotPlain
            elif kind == ":":
                # only a field of a struct literal (`Foo { a: f(1), b: 2 }`, the braces after a name); a bare block
                # `{ a: f(1) }`, a `"key": value` or a typed closure parameter is for the parse to read
                if not named or previous != "identifier" or before not in (None, ","):
                    raise _NotPlain
            elif kind == "as":
                if previous not in _OPERANDS and previous != "token_tree" and previous != "?":
                    raise _NotPlain
            elif kind == "await":
                if previous != ".":
                    raise _NotPlain
            elif kind == "mutable_specifier":
                if previous not in ("&", "*"):
                    raise _NotPlain
            elif kind not in _PLAIN_PUNCTUATION:
                raise _NotPlain  # a keyword, `=>`, `#`, `$`, a lifetime, `->`, `@`, ...
            before, previous = previous, kind

    def _call_before(self, kids: List[Node], index: int, arguments: Node, found: List[RawCall]) -> None:
        """The call whose arguments are `kids[index]`: a function, a path, or a method of a plain name."""
        callee = kids[index - 1]
        start = index - 1
        while start >= 2 and kids[start - 1].type == "::" and kids[start - 2].type in _PATH_PARTS:
            start -= 2
        parts = [kids[position].text.decode("utf8") for position in range(start, index, 2)]
        if len(parts) >= 2:
            found.append(RawCall(receiver="::".join(parts[:-1]), name=parts[-1], text=self._text_of(kids[start], arguments), kind="path"))
            return
        if start >= 1 and kids[start - 1].type == ".":
            value = kids[start - 2] if start >= 2 else None
            if value is None or value.type not in ("identifier", "self"):
                return
            before = kids[start - 3].type if start >= 3 else None
            if before in (".", "::"):
                return  # a field of a field, or the end of a path: not a plain name
            if callee.type == "identifier":
                found.append(RawCall(receiver=value.text.decode("utf8"), name=parts[0], text=self._text_of(value, arguments), kind="method"))
            return
        if callee.type == "identifier":
            found.append(RawCall(receiver=None, name=parts[0], text=self._text_of(callee, arguments)))

    def _macro_before(self, kids: List[Node], index: int, tokens: Node, depth: int, found: List[RawCall]) -> None:
        """A macro written inside a macro: `name!(..)` or `path::name!(..)` (`kids[index]` is the `!`)."""
        start = index - 1
        while start >= 2 and kids[start - 1].type == "::" and kids[start - 2].type in _PATH_PARTS:
            start -= 2
        short = kids[index - 1].text.decode("utf8")
        if short not in STANDARD_MACROS:
            found.append(RawCall(receiver=None, name=f"{short}!", text=self._text_of(kids[start], tokens)[:200], kind="macro"))
        if depth + 1 < _MAX_MACRO_DEPTH:
            self._walk_tokens(tokens, depth + 1, found)

    def _collect_errors(self, node: Node, errors: List[ParseError]) -> None:
        if node.type == "ERROR" or node.is_missing:
            errors.append(ParseError(line=node.start_point[0] + 1, column=node.start_point[1], message=f"Syntax error: {node.type}" if node.type == "ERROR" else "Missing token"))
        for child in node.children:
            self._collect_errors(child, errors)
