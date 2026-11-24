"""C++ source code parser using Tree-sitter."""

from __future__ import annotations

import bisect
import re
from pathlib import Path
from typing import Optional

import tree_sitter_cpp as tscpp
from tree_sitter import Language, Parser, Node

from drspec.parsers.cpp_defined_macros import (
    blank_decorations, blank_definers, blank_names, decoration_macros, definer_calls, defining_macros, identity_macros, replace_tags, tag_macros,
)
from drspec.parsers.cpp_macros import macro_test_case
from drspec.parsers.cpp_types import normalize_type
from drspec.parsers.parameters import cpp_parameter_tokens, innermost_function_declarator
from drspec.parsers.models import (
    ExtractedFunction,
    ParseError,
    ParseResult,
    RawCall,
    RawMemberAccess,
    RawNameImport,
)


_CPP_BUILTIN_TYPES = frozenset("int long short char bool float double void unsigned signed size_t ptrdiff_t wchar_t".split())


def _blank(line: str) -> str:
    """Spaces for `line`, as many as it has bytes in UTF-8: a blanked line keeps the offsets of everything after it (the
    tree is made from bytes, and a character of text above ASCII is more than one)."""
    return " " * len(line.encode("utf-8"))


class CppParser:
    """Parser for C++ source code using Tree-sitter.

    Extracts function definitions including:
    - Free functions
    - Class methods (in-class and out-of-class definitions)
    - Template functions and methods
    - Namespaced functions
    - Function declarations (from headers)
    """

    def __init__(self) -> None:
        """Initialize the C++ parser with tree-sitter version compatibility."""
        self._c_mode = False
        try:
            # Some tree-sitter 0.21.x builds use the new Language/set_language API...
            self._language = Language(tscpp.language(), "cpp")
            self._parser = Parser()
            self._parser.set_language(self._language)
        except TypeError:
            # ...others still expect the old Language/.language-attribute API.
            self._language = Language(tscpp.language())
            self._parser = Parser()
            self._parser.language = self._language

    def parse(self, source_code: str, file_path: Optional[str] = None, language: Optional[str] = None) -> ParseResult:
        """Parse C++ source code and extract functions.

        Args:
            source_code: C++ source code as string.
            file_path: Optional file path for context.

        Returns:
            ParseResult containing extracted functions and any errors.
        """
        # What the grammar cannot read is blanked out before parsing, not hidden after: an error leaves the
        # tree with the code that follows it folded into the error region.
        original = source_code
        c_mode = language == "c" or (language is None and bool(file_path) and file_path.endswith(".c"))
        self._c_mode = c_mode
        prepared, skipped_constructs = self._prepare(original)
        # The macros the project defines to decorate declarations (`class API Name`, `F() FMT(2);`) are blanked in the text
        # the tree is made from too.
        parse_text = blank_decorations(prepared, decoration_macros(original, file_path))
        parse_text = self._without_directives_in_enums(parse_text)
        mangled = False
        defined_by_macro: list = []
        self._definer_lines = set()
        if c_mode:
            # What only C needs, in the text the tree is made from (the same length, so no position moves): a K&R
            # definition without its parameter declarations, and the C++ keywords that C uses as names under other spelling.
            parse_text = replace_tags(parse_text, tag_macros(original, file_path))
            parse_text = blank_names(parse_text, identity_macros(original, file_path))
            definers = defining_macros(original, file_path)
            defined_by_macro = definer_calls(parse_text, definers)
            self._definer_lines = {line for first, _use, last in defined_by_macro for line in range(first, last + 1)}
            parse_text = blank_definers(parse_text, definers)
            parse_text = self._without_old_style_parameters(parse_text)
            parse_text = self._without_c_extensions(parse_text)
            parse_text, mangled = self._mangle_keywords(parse_text)
        tree = self._parser.parse(bytes(parse_text, "utf8"))
        # The stored text of every function and call is the source as the user wrote it, whatever the tree was made from
        # (every blanking keeps the length and the positions, so the tree's offsets are the original's).
        source_code = original
        self._text_bytes = bytes(source_code, "utf8")
        self._parse_bytes = bytes(parse_text, "utf8")
        read_text = parse_text  # the text the tree was made from (same positions as `source_code`)
        only_first: Optional[str] = None  # the text the first-branch tree was made from, when that is the tree read
        if tree.root_node.has_error:
            tree, read_text, only_first = self._better_tree(parse_text, tree)
        result = ParseResult(file_path=file_path)
        result.skipped_constructs = skipped_constructs

        if tree.root_node.has_error:
            result.has_errors = True
            self._collect_errors(tree.root_node, result.errors)

        is_header = self._is_header_file(file_path) if file_path else False

        raw_functions: list[ExtractedFunction] = []
        self._extract_functions(
            tree.root_node,
            source_code,
            raw_functions,
            namespace=None,
            class_name=None,
            is_header=is_header,
        )

        if only_first is not None:
            raw_functions.extend(self._functions_of_other_branches(only_first, read_text, source_code, raw_functions, is_header, c_mode))

        if mangled:
            self._restore_names(raw_functions)

        # A declaration is dropped only when the same function (name and parameters) is also defined in this file;
        # every definition is kept, so overloads stay separate, and so do two definitions of one function (the two
        # branches of an `#if` / `#else`), which the scanner gives identifiers of their own.
        seen: dict[tuple[str, tuple[str, ...]], ExtractedFunction] = {}
        again: list[ExtractedFunction] = []
        for func in raw_functions:
            key = (func.qualified_name, tuple(func.parameter_tokens))
            existing = seen.get(key)
            if existing is None or ("declaration" in existing.decorators and "declaration" not in func.decorators):
                seen[key] = func
            elif "declaration" not in existing.decorators and "declaration" not in func.decorators:
                again.append(func)
        result.functions = sorted([*seen.values(), *again], key=lambda f: (f.start_line, f.qualified_name))
        if tree.root_node.has_error:
            # (comments and strings are blanked: code shown in a comment is not a definition)
            code_only = self._NOT_CODE.sub(lambda match: re.sub(r"[^\n]", " ", match.group(0)), parse_text)
            result.suspected_losses = self._suspected_losses(tree.root_node, code_only.split("\n"), result.functions)
        # A definition the grammar dropped without any error is found by the braces alone and reported, so that a file
        # is not `successful` while a function is missing.
        missed = [line for line in self._missed_definitions(read_text, result.functions) if line not in result.suspected_losses]
        if missed:
            result.suspected_losses = sorted(set(result.suspected_losses) | set(missed))
            if not result.has_errors:
                result.has_errors = True
                result.errors.append(ParseError(line=missed[0], column=0, message="Possibly missed definition"))

        if c_mode and result.suspected_losses:
            # what a C compiler never sees (`#ifdef __cplusplus`, the C++ wrapper of a C header) is not lost to a C project
            hidden = self._cplusplus_only_lines(original)
            result.suspected_losses = [line for line in result.suspected_losses if line not in hidden]
        for line, use, _last in defined_by_macro:
            # a function a macro defines: nothing in the text names it, so the use is reported as it is written
            if line not in result.suspected_losses:
                result.suspected_losses.append(line)
            result.suspected_names[line] = f"{use} (a function defined by a macro)"
        result.suspected_losses.sort()
        if result.suspected_losses:
            result.suspected_names = {
                **self._names_of_losses(
                    original,
                    [line for line in result.suspected_losses if line not in result.suspected_names],
                ),
                **result.suspected_names,
            }
            if c_mode:
                # A struct, union or enum of C holds no function: its head in an error region loses nothing a user could
                # search for (the error itself is still reported).
                kept = [line for line in result.suspected_losses if not self._CLASS_HEAD.match(result.suspected_names.get(line, ""))]
                result.suspected_losses = kept
                result.suspected_names = {line: name for line, name in result.suspected_names.items() if line in kept}

        # A separate pass over the whole tree, not scoped to any one function.
        self._extract_imports(tree.root_node, result.name_imports)
        result.includes = self._extract_includes(tree.root_node)
        try:
            result.facts = self._type_facts(tree.root_node)  # (what the resolver types a receiver by; never fails a parse)
        except RecursionError:
            result.facts = {}

        return result

    def parse_file(self, file_path: str, language: Optional[str] = None) -> ParseResult:
        """Parse a C++ file and extract functions.

        Args:
            file_path: Path to the C++ file.
            language: `c` when the file is read as C (what only C needs is switched on).

        Returns:
            ParseResult containing extracted functions and any errors.

        Raises:
            FileNotFoundError: If the file doesn't exist.
            IOError: If the file can't be read.
        """
        with open(file_path, "r", encoding="utf-8") as f:
            source_code = f.read()
        return self.parse(source_code, file_path=file_path, language=language)

    def _is_header_file(self, file_path: str) -> bool:
        """Check if file is a header file."""
        return file_path.endswith((".h", ".hpp", ".hxx", ".H", ".hh"))

    def _extract_functions(
        self,
        node: Node,
        source_code: str,
        functions: list[ExtractedFunction],
        namespace: Optional[str] = None,
        class_name: Optional[str] = None,
        is_header: bool = False,
    ) -> None:
        """Recursively extract functions from an AST node.

        Args:
            node: Current AST node.
            source_code: Original source code.
            functions: List to append extracted functions to.
            namespace: Current namespace context.
            class_name: Current class context.
            is_header: True if parsing a header file.
        """
        for child in node.children:
            if child.type == "namespace_definition":
                ns_name = self._get_namespace_name(child)
                full_ns = f"{namespace}::{ns_name}" if namespace else ns_name
                body = self._get_child_by_type(child, "declaration_list")
                if body:
                    self._extract_functions(
                        body, source_code, functions, namespace=full_ns, class_name=None, is_header=is_header
                    )

            elif child.type in ("class_specifier", "struct_specifier"):
                cls_name = self._get_class_name(child)
                if cls_name:
                    full_class = self._build_qualified_name(namespace, cls_name)
                    body = self._get_child_by_type(child, "field_declaration_list")
                    if body:
                        self._extract_class_methods(
                            body, source_code, functions, namespace, full_class, is_header
                        )

            elif child.type == "function_definition":
                func = self._extract_function_definition(
                    child, source_code, namespace, class_name, is_header
                )
                if func:
                    functions.append(func)

            elif child.type == "template_declaration":
                self._extract_template(
                    child, source_code, functions, namespace, class_name, is_header
                )

            elif child.type == "declaration" and is_header:
                func = self._extract_function_declaration(child, source_code, namespace)
                if func:
                    functions.append(func)

            else:
                self._extract_functions(
                    child, source_code, functions, namespace, class_name, is_header
                )

    #: A K&R definition: `int kr(a, b)` then the declarations of `a` and `b`, then the body.
    _OLD_STYLE = re.compile(
        r"(?m)^([ \t]*(?:[A-Za-z_][\w \t\*]*[ \t\*]))([A-Za-z_]\w*)([ \t]*\([ \t\r\n]*[A-Za-z_]\w*(?:[ \t\r\n]*,[ \t\r\n]*[A-Za-z_]\w*)*[ \t\r\n]*\))"
        r"((?:[ \t]*\r?\n)*(?:[ \t]*(?:register[ \t]+)?[A-Za-z_][\w \t\*\[\],]*;[ \t]*\r?\n)+)(?=[ \t]*\{)"
    )

    def _without_old_style_parameters(self, text: str) -> str:
        """The text with the parameter declarations of K&R definitions blanked, so `int kr(a, b) int a; int b; {...}` is
        read as a function of two parameters the grammar knows (it knows their names, which is what the identifier needs)."""
        def blank(match: "re.Match[str]") -> str:
            declarations = match.group(4)
            return match.group(1) + match.group(2) + match.group(3) + re.sub(r"[^\n]", " ", declarations)

        return self._OLD_STYLE.sub(blank, text)

    _ENUM_OPENING = re.compile(r"\benum\b(?:\s+(?:class|struct))?[^;{}()]*\{")

    def _without_directives_in_enums(self, text: str) -> str:
        """Directive lines (`#define`, `#undef`, `#if`) inside the braces of an `enum`, blanked, the same length: the
        grammar cannot read them there, and an enum written with an X-macro or a conditional enumerator is common."""
        matches = list(self._ENUM_OPENING.finditer(text))
        if not matches:
            return text
        pieces, position = [], 0
        for match in matches:
            if match.start() < position:
                continue
            closing = self._closing(text, match.end() - 1, "{", "}")
            if closing == -1:
                continue
            body = text[match.end() : closing]
            if "#" not in body:
                continue
            lines = body.split("\n")
            continued = False
            for index, line in enumerate(lines):
                if continued or line.lstrip().startswith("#"):
                    continued = line.rstrip().endswith("\\")
                    lines[index] = _blank(line)
            pieces.append(text[position : match.end()])
            pieces.append("\n".join(lines))
            position = closing
        pieces.append(text[position:])
        return "".join(pieces)

    _ASM = re.compile(r"\b__?asm(?:__)?\b(?:\s*(?:__?volatile(?:__)?|volatile|goto|inline)\b)*\s*\(")
    _ARRAY_QUALIFIER = re.compile(r"\[(\s*)(?:static|restrict|const|volatile)\b")

    @staticmethod
    def _closing(text: str, opening: int, open_char: str, close_char: str) -> int:
        """The index of the character that closes the one at `opening`, skipping strings and characters; -1 if none."""
        depth, index, length = 0, opening, len(text)
        while index < length:
            char = text[index]
            if char in "\"'":
                quote, index = char, index + 1
                while index < length and text[index] != quote:
                    index += 2 if text[index] == "\\" else 1
            elif char == open_char:
                depth += 1
            elif char == close_char:
                depth -= 1
                if depth == 0:
                    return index
            index += 1
        return -1

    def _without_c_extensions(self, text: str) -> str:
        """The GNU and C99 forms the C++ grammar cannot read, each replaced by something it can, the same length: a
        statement expression `({ ... })` and an `__asm__` statement become a `0` (what is inside them is not read), and
        the qualifiers in an array parameter (`int a[static 1]`) are blanked."""
        def blank(start: int, end: int, keep: str = "") -> None:
            nonlocal text
            replacement = re.sub(r"[^\n]", " ", text[start:end])
            if keep:
                replacement = keep + replacement[len(keep):]
            text = text[:start] + replacement + text[end:]

        position = 0
        while True:
            found = text.find("({", position)
            if found == -1:
                break
            before = text[:found].rstrip()
            word = re.search(r"([A-Za-z_]\w*)$", before)
            if before and (before[-1] in ")]" or (word is not None and word.group(1) not in ("return", "case", "else", "do", "goto"))):
                # `f({`: a call, not a statement expression
                position = found + 2
                continue
            end = self._closing(text, found, "(", ")")
            if end == -1:
                break
            blank(found, end + 1, keep="(0")
            text = text[:end] + ")" + text[end + 1 :]
            position = end + 1
        for match in list(self._ASM.finditer(text)):
            end = self._closing(text, match.end() - 1, "(", ")")
            if end != -1 and match.start() < len(text) and text[match.start() : match.start() + 1] != " ":
                blank(match.start(), end + 1, keep="0")
        text = self._ARRAY_QUALIFIER.sub(lambda match: "[" + match.group(1) + " " * (len(match.group(0)) - len(match.group(1)) - 1), text)
        return text

    #: Words that are keywords in C++ and ordinary names in C, and the same-length spelling the grammar reads as a name.
    _C_NAMES = {word: word[0] + "_" + word[2:] for word in (
        "new", "delete", "class", "this", "template", "namespace", "typename", "operator", "private", "protected", "public",
        "virtual", "friend", "explicit", "export", "mutable", "using", "try", "catch", "throw", "typeid",
    )}
    _C_NAMES_PATTERN = re.compile(r"\b(" + "|".join(sorted(_C_NAMES, key=len, reverse=True)) + r")\b")
    _C_NAMES_BACK = {mangled: word for word, mangled in _C_NAMES.items()}
    _C_NAMES_BACK_PATTERN = re.compile(r"\b(" + "|".join(sorted(_C_NAMES_BACK)) + r")\b")

    def _mangle_keywords(self, text: str) -> "tuple[str, bool]":
        """`text` with C++ keywords that C uses as names respelled (same length), outside strings, comments and directives;
        and whether any was."""
        pieces: list[str] = []
        position = 0
        changed = False

        def code(segment: str) -> str:
            nonlocal changed
            lines = segment.split("\n")
            for index, line in enumerate(lines):
                if line.lstrip().startswith("#") or not self._C_NAMES_PATTERN.search(line):
                    continue
                lines[index] = self._C_NAMES_PATTERN.sub(lambda match: self._C_NAMES[match.group(1)], line)
                changed = True
            return "\n".join(lines)

        for match in self._NOT_CODE.finditer(text):
            pieces.append(code(text[position : match.start()]))
            pieces.append(match.group(0))
            position = match.end()
        pieces.append(code(text[position:]))
        return "".join(pieces), changed

    def _restore_names(self, functions: list) -> None:
        """Put the original spelling back in what was read from the respelled text: names, parameter facts and calls."""
        def back(value: Optional[str]) -> Optional[str]:
            return self._C_NAMES_BACK_PATTERN.sub(lambda match: self._C_NAMES_BACK[match.group(1)], value) if value else value

        for function in functions:
            function.name = back(function.name) or function.name
            function.qualified_name = back(function.qualified_name) or function.qualified_name
            function.parent = back(function.parent)
            function.first_param_name = back(function.first_param_name)
            function.parameter_tokens = [back(token) or token for token in function.parameter_tokens]
            function.declared_param_types = {back(key) or key: back(value) or value for key, value in function.declared_param_types.items()}
            for call in function.calls:
                call.name = back(call.name) or call.name
                call.receiver = back(call.receiver)

    _PREPROCESSOR_BLOCKS = frozenset({"preproc_if", "preproc_ifdef", "preproc_else", "preproc_elif", "preproc_elifdef"})

    def _extract_class_methods(
        self,
        class_body: Node,
        source_code: str,
        functions: list[ExtractedFunction],
        namespace: Optional[str],
        class_name: str,
        is_header: bool,
        current_access: str = "private",  # C++ class members default to private
    ) -> None:
        """Extract methods from a class body.

        Args:
            class_body: The field_declaration_list node (or a preprocessor block inside it).
            source_code: Original source code.
            functions: List to append extracted functions to.
            namespace: Current namespace.
            class_name: Full qualified class name.
            is_header: True if parsing a header file.
            current_access: The access the members start with.
        """
        for child in class_body.children:
            if child.type in self._PREPROCESSOR_BLOCKS:
                # members under `#if`, `#ifdef`, `#else` and `#elif` are members, whichever branch holds them
                self._extract_class_methods(child, source_code, functions, namespace, class_name, is_header, current_access)

            elif child.type == "access_specifier":
                access_text = child.text.decode("utf8").strip().rstrip(":")
                current_access = access_text

            elif child.type == "function_definition":
                func = self._extract_method(
                    child, source_code, namespace, class_name, current_access, is_header
                )
                if func:
                    functions.append(func)

            elif child.type in ("declaration", "field_declaration"):
                # A class declared inside this one is a field_declaration whose type is the nested class: its members
                # (methods and constructors, with or without an initializer list) are read as members of that class.
                nested = self._nested_class_body(child)
                if nested is not None:
                    nested_name, nested_body = nested
                    self._extract_class_methods(
                        nested_body, source_code, functions, namespace, f"{class_name}::{nested_name}", is_header
                    )
                func = self._extract_method_declaration(
                    child, source_code, namespace, class_name, current_access, is_header
                )
                if func:
                    functions.append(func)

            elif child.type == "template_declaration":
                self._extract_template(
                    child, source_code, functions, namespace, class_name, is_header
                )

            elif child.type in ("class_specifier", "struct_specifier"):
                nested_name = self._get_class_name(child)
                if nested_name:
                    full_nested = f"{class_name}::{nested_name}"
                    nested_body = self._get_child_by_type(child, "field_declaration_list")
                    if nested_body:
                        self._extract_class_methods(
                            nested_body, source_code, functions, namespace, full_nested, is_header
                        )

    def _nested_class_body(self, declaration: Node) -> Optional[tuple[str, Node]]:
        """The name and member list of a named class or struct that a member declaration defines, or None.

        In a class body the tree wraps a nested class as `field_declaration` with the class as its `type`; the class's
        own body is a `field_declaration_list` like any other."""
        type_node = declaration.child_by_field_name("type")
        if type_node is None or type_node.type not in ("class_specifier", "struct_specifier"):
            return None
        name = self._get_class_name(type_node)
        body = self._get_child_by_type(type_node, "field_declaration_list")
        return (name, body) if name and body else None

    def _extract_function_definition(
        self,
        node: Node,
        source_code: str,
        namespace: Optional[str],
        class_name: Optional[str],
        is_header: bool,
    ) -> Optional[ExtractedFunction]:
        """Extract a function definition.

        Args:
            node: The function_definition node.
            source_code: Original source code.
            namespace: Current namespace context.
            class_name: Current class context (for out-of-class definitions).
            is_header: True if parsing a header file.

        Returns:
            ExtractedFunction or None if extraction fails.
        """
        declarator = self._get_child_by_type(node, "function_declarator")
        if declarator:
            declarator = innermost_function_declarator(declarator)
        else:
            # May be nested inside a reference_declarator or pointer_declarator.
            declarator = self._find_function_declarator(node)
        conversion = None if declarator else self._conversion_operator(node)
        if not declarator and conversion is None:
            return None

        name_info = self._get_function_name(declarator) if declarator else (conversion[0], None)
        if not name_info:
            return None

        name, method_class = name_info

        if method_class:
            # Out-of-class definition, e.g. `ClassName::method`.
            qualified_name = self._build_qualified_name(namespace, method_class, name)
            parent = self._build_qualified_name(namespace, method_class)
            is_method = True
        elif class_name:
            qualified_name = f"{class_name}::{name}"
            parent = class_name
            is_method = True
        else:
            qualified_name = self._build_qualified_name(namespace, name)
            parent = namespace
            is_method = False

        signature = self._get_signature(node, source_code)
        body = self._get_node_text(node, source_code)

        start_line = node.start_point[0] + 1
        end_line = node.end_point[0] + 1

        decorators = self._get_function_specifiers(node)
        if is_header and not self._has_body(node):
            decorators.append("declaration")

        tokens = conversion[1] if conversion else cpp_parameter_tokens(node)
        named = macro_test_case(name, tokens) if not is_method else None
        if named is not None:
            # A test macro: `TEST(Suite, Case) { ... }` is the function `Suite.Case`, not one more `TEST`.
            suite, case = named
            name, qualified_name, parent, tokens = case, f"{suite}.{case}" if suite else case, suite, []
            decorators.append("test")

        declared = self._declared_types(node)
        return ExtractedFunction(
            name=name,
            qualified_name=qualified_name,
            signature=signature,
            body=body,
            start_line=start_line,
            end_line=end_line,
            parent=parent,
            decorators=decorators,
            is_method=is_method,
            is_async=False,  # C++ doesn't have async keyword like Python/JS
            calls=self._extract_calls_for_function(node, source_code),
            member_accesses=self._extract_member_accesses(node, parent if is_method else None, declared),
            parameter_tokens=tokens,
            declared_param_types=declared,
            has_error=node.has_error,
        )

    def _extract_method(
        self,
        node: Node,
        source_code: str,
        namespace: Optional[str],
        class_name: str,
        access: str,
        is_header: bool,
    ) -> Optional[ExtractedFunction]:
        """Extract a method from inside a class body.

        Args:
            node: The function_definition node.
            source_code: Original source code.
            namespace: Current namespace.
            class_name: Full qualified class name.
            access: Access specifier (public/private/protected).
            is_header: True if parsing a header file.

        Returns:
            ExtractedFunction or None if extraction fails.
        """
        declarator = self._get_child_by_type(node, "function_declarator")
        if not declarator:
            declarator = self._find_function_declarator(node)
        conversion = None if declarator else self._conversion_operator(node)
        if not declarator and conversion is None:
            return None

        name_info = self._get_function_name(declarator) if declarator else (conversion[0], None)
        if not name_info:
            return None

        name, _ = name_info
        qualified_name = f"{class_name}::{name}"

        signature = self._get_signature(node, source_code)
        body = self._get_node_text(node, source_code)

        start_line = node.start_point[0] + 1
        end_line = node.end_point[0] + 1

        decorators = self._get_function_specifiers(node)
        decorators.append(access)

        declared = self._declared_types(node)
        return ExtractedFunction(
            name=name,
            qualified_name=qualified_name,
            signature=signature,
            body=body,
            start_line=start_line,
            end_line=end_line,
            parent=class_name,
            decorators=decorators,
            is_method=True,
            is_async=False,
            calls=self._extract_calls_for_function(node, source_code),
            member_accesses=self._extract_member_accesses(node, class_name, declared),
            parameter_tokens=conversion[1] if conversion else cpp_parameter_tokens(node),
            declared_param_types=declared,
            has_error=node.has_error,
        )

    def _extract_method_declaration(
        self,
        node: Node,
        source_code: str,
        namespace: Optional[str],
        class_name: str,
        access: str,
        is_header: bool,
    ) -> Optional[ExtractedFunction]:
        """Extract a method declaration (prototype) from inside a class.

        Args:
            node: The declaration node.
            source_code: Original source code.
            namespace: Current namespace.
            class_name: Full qualified class name.
            access: Access specifier.
            is_header: True if parsing a header file.

        Returns:
            ExtractedFunction or None if not a function declaration.
        """
        declarator = self._find_function_declarator(node)
        if not declarator:
            return None

        name_info = self._get_function_name(declarator)
        if not name_info:
            return None

        name, _ = name_info
        qualified_name = f"{class_name}::{name}"

        signature = self._get_signature(node, source_code)
        body = self._get_node_text(node, source_code)

        start_line = node.start_point[0] + 1
        end_line = node.end_point[0] + 1

        decorators = self._get_function_specifiers(node)
        decorators.append(access)
        decorators.append("declaration")

        return ExtractedFunction(
            name=name,
            qualified_name=qualified_name,
            signature=signature,
            body=body,
            start_line=start_line,
            end_line=end_line,
            parent=class_name,
            decorators=decorators,
            is_method=True,
            is_async=False,
            parameter_tokens=cpp_parameter_tokens(node),
            has_error=node.has_error,
        )

    def _extract_function_declaration(
        self,
        node: Node,
        source_code: str,
        namespace: Optional[str],
    ) -> Optional[ExtractedFunction]:
        """Extract a function declaration (prototype) from a header file.

        Args:
            node: The declaration node.
            source_code: Original source code.
            namespace: Current namespace.

        Returns:
            ExtractedFunction or None if not a function declaration.
        """
        declarator = self._find_function_declarator(node)
        if not declarator:
            return None

        name_info = self._get_function_name(declarator)
        if not name_info:
            return None

        name, method_class = name_info

        if method_class:
            qualified_name = self._build_qualified_name(namespace, method_class, name)
            parent = self._build_qualified_name(namespace, method_class)
            is_method = True
        else:
            qualified_name = self._build_qualified_name(namespace, name)
            parent = namespace
            is_method = False

        signature = self._get_signature(node, source_code)
        body = self._get_node_text(node, source_code)

        start_line = node.start_point[0] + 1
        end_line = node.end_point[0] + 1

        decorators = self._get_function_specifiers(node)
        decorators.append("declaration")

        return ExtractedFunction(
            name=name,
            qualified_name=qualified_name,
            signature=signature,
            body=body,
            start_line=start_line,
            end_line=end_line,
            parent=parent,
            decorators=decorators,
            is_method=is_method,
            is_async=False,
            parameter_tokens=cpp_parameter_tokens(node),
            has_error=node.has_error,
        )

    def _extract_template(
        self,
        node: Node,
        source_code: str,
        functions: list[ExtractedFunction],
        namespace: Optional[str],
        class_name: Optional[str],
        is_header: bool,
    ) -> None:
        """Extract template function or class.

        Args:
            node: The template_declaration node.
            source_code: Original source code.
            functions: List to append extracted functions to.
            namespace: Current namespace.
            class_name: Current class context.
            is_header: True if parsing a header file.
        """
        for child in node.children:
            if child.type == "function_definition":
                func = self._extract_function_definition(
                    child, source_code, namespace, class_name, is_header
                )
                if func:
                    func.decorators.append("template")
                    functions.append(func)

            elif child.type == "declaration":
                func = self._extract_function_declaration(child, source_code, namespace)
                if func:
                    func.decorators.append("template")
                    functions.append(func)

            elif child.type in ("class_specifier", "struct_specifier"):
                cls_name = self._get_class_name(child)
                if cls_name:
                    full_class = self._build_qualified_name(namespace, cls_name)
                    body = self._get_child_by_type(child, "field_declaration_list")
                    if body:
                        self._extract_class_methods(
                            body, source_code, functions, namespace, full_class, is_header
                        )

    def _get_namespace_name(self, node: Node) -> str:
        """Get namespace name from namespace_definition node."""
        for child in node.children:
            if child.type in ("identifier", "namespace_identifier"):
                return child.text.decode("utf8")
        return "<anonymous>"

    def _get_class_name(self, node: Node) -> Optional[str]:
        """Get class name from class_specifier or struct_specifier node: the name as written, without template arguments
        (`Impl<X>` is `Impl`; `Node<X>::Impl`, the nested struct an explicit specialization defines, is `Node::Impl`)."""
        name_node = self._get_child_by_type(node, "type_identifier")
        if name_node:
            return name_node.text.decode("utf8")
        written = node.child_by_field_name("name")
        return self._scoped_class_name(written) if written is not None else None

    def _scoped_class_name(self, node: Node) -> Optional[str]:
        """The names of a class written `A::B`, `A<X>::B` or `B<X>`, joined by `::` and without template arguments."""
        if node.type in ("type_identifier", "namespace_identifier", "identifier"):
            return node.text.decode("utf8")
        if node.type == "template_type":
            name = node.child_by_field_name("name")
            return name.text.decode("utf8") if name is not None else None
        if node.type in ("qualified_identifier", "scoped_type_identifier", "scoped_namespace_identifier"):
            scope, name = node.child_by_field_name("scope"), node.child_by_field_name("name")
            if name is None:
                return None
            tail = self._scoped_class_name(name)
            head = self._scoped_class_name(scope) if scope is not None else None
            return f"{head}::{tail}" if head and tail else tail
        return None

    def _conversion_operator(self, node: Node) -> Optional[tuple[str, list[str]]]:
        """`operator int() const { … }`, `operator float*() { … }`: a conversion operator is a function definition whose
        head is an `operator_cast`, not a function declarator. Returns its name (`operator int`, `operator float*`) and
        the tokens that tell overloads apart (`const`), or `None` when the node is not one."""
        cast = self._get_child_by_type(node, "operator_cast")
        if cast is None:
            return None
        text = cast.text.decode("utf8")
        match = re.match(r"operator\s+(.*?)\s*\(", text, re.DOTALL)
        if not match:
            return None
        name = "operator " + " ".join(match.group(1).split())
        qualifiers = [part for part in re.findall(r"\)\s*((?:const|volatile|noexcept|&&|&|\s)+)", text) for part in part.split()]
        return name, qualifiers

    def _get_function_name(self, declarator: Node) -> Optional[tuple[str, Optional[str]]]:
        """Get function name and class qualifier from declarator.

        Args:
            declarator: The function_declarator node.

        Returns:
            Tuple of (name, class_name) where class_name is None for free functions.
        """
        for child in declarator.children:
            if child.type == "qualified_identifier":
                parts = self._parse_qualified_identifier(child)
                if len(parts) >= 2:
                    return parts[-1], "::".join(parts[:-1])
                elif len(parts) == 1:
                    return parts[0], None

            elif child.type in ("identifier", "field_identifier"):
                return child.text.decode("utf8"), None

            elif child.type == "destructor_name":
                return child.text.decode("utf8"), None

            elif child.type in ("operator_name", "template_function"):
                return "".join(child.text.decode("utf8").split()), None

            elif child.type == "operator_cast":
                return " ".join(child.text.decode("utf8").split()), None

        return None

    def _parse_qualified_identifier(self, node: Node) -> list[str]:
        """Parse a qualified identifier into its parts.

        Args:
            node: A qualified_identifier or scoped_identifier node.

        Returns:
            List of identifier parts, e.g., ["namespace", "ClassName", "method"].
        """
        parts = []
        for child in node.children:
            if child.type in ("identifier", "type_identifier", "namespace_identifier"):
                parts.append(child.text.decode("utf8"))
            elif child.type == "qualified_identifier":
                parts.extend(self._parse_qualified_identifier(child))
            elif child.type in ("destructor_name", "operator_name", "operator_cast", "template_function"):
                parts.append("".join(child.text.decode("utf8").split()) if child.type != "operator_cast" else " ".join(child.text.decode("utf8").split()))
            elif child.type == "template_type":
                type_node = self._get_child_by_type(child, "type_identifier")
                if type_node:
                    parts.append(type_node.text.decode("utf8"))
        return parts

    def _find_function_declarator(self, node: Node) -> Optional[Node]:
        """Recursively find a function_declarator node."""
        for child in node.children:
            if child.type == "function_declarator":
                return self._innermost_function_declarator(child)
            if child.type in ("init_declarator", "reference_declarator", "pointer_declarator", "declarator"):
                result = self._find_function_declarator(child)
                if result:
                    return result
        return None

    def _innermost_function_declarator(self, declarator: Node) -> Node:
        return innermost_function_declarator(declarator)

    def _has_body(self, node: Node) -> bool:
        """Check if function has a body (compound_statement)."""
        return self._get_child_by_type(node, "compound_statement") is not None

    def _get_function_specifiers(self, node: Node) -> list[str]:
        """Extract function specifiers like virtual, static, const, etc."""
        specifiers = []

        for child in node.children:
            if child.type == "virtual":
                specifiers.append("virtual")
            elif child.type == "static":
                specifiers.append("static")
            elif child.type == "storage_class_specifier":
                text = child.text.decode("utf8")
                if text in ("static", "extern", "inline"):
                    specifiers.append(text)
            elif child.type == "type_qualifier":
                text = child.text.decode("utf8")
                if text in ("const", "volatile", "constexpr"):
                    specifiers.append(text)

        # Check for const after parameters (e.g., void foo() const)
        for child in node.children:
            if child.type == "function_declarator":
                for subchild in child.children:
                    if subchild.type == "type_qualifier":
                        text = subchild.text.decode("utf8")
                        if text not in specifiers:
                            specifiers.append(text)

        return specifiers

    def _build_qualified_name(self, *parts: Optional[str]) -> str:
        """Build a qualified name from parts, filtering out None values."""
        valid_parts = [p for p in parts if p]
        return "::".join(valid_parts) if valid_parts else ""

    def _get_signature(self, node: Node, source_code: str) -> str:
        """Extract the function signature (first line or up to the body)."""
        if self._lines_of is not source_code:  # the lines of a file are cut once, not once per function
            self._lines = source_code.split("\n")
            self._lines_of = source_code
        lines = self._lines
        start_line = node.start_point[0]

        body = self._get_child_by_type(node, "compound_statement")
        if body:
            body_line = body.start_point[0]
            if body_line > start_line:
                sig_lines = lines[start_line:body_line]
                return " ".join(line.strip() for line in sig_lines).strip()

        if start_line < len(lines):
            return lines[start_line].strip()
        return ""

    def _get_child_by_type(self, node: Node, type_name: str) -> Optional[Node]:
        """Find a child node by type."""
        for child in node.children:
            if child.type == type_name:
                return child
        return None

    _text_bytes: Optional[bytes] = None
    _parse_bytes: Optional[bytes] = None
    _definer_lines: frozenset = frozenset()  # the lines of the uses of function-defining macros (blanked, and reported)
    _lines: list = []
    _lines_of: Optional[str] = None

    def _get_node_text(self, node: Node, source_code: str) -> str:
        """Get the full text of a node. It comes from the source as the user wrote it, not from the tree's own text: the
        tree was made from a copy in which some macros are blanked (same length, same positions). A function's text
        starts at the first token the user wrote, which is a macro in front of it (`API int f() {`) that the tree does
        not include."""
        if self._text_bytes is not None and node.end_byte <= len(self._text_bytes):
            start = node.start_byte
            if node.type == "function_definition":
                start = self._with_leading_macros(start)
            return self._text_bytes[start:node.end_byte].decode("utf-8", errors="replace")
        return node.text.decode("utf-8")

    def _with_leading_macros(self, start: int) -> int:
        """`start`, moved back over the macros blanked right in front of it (and the whitespace between them)."""
        original, parsed = self._text_bytes, self._parse_bytes
        if original is None or parsed is None or len(parsed) != len(original):
            return start
        position = start
        while True:
            before = position
            while before > 0 and original[before - 1 : before] in (b" ", b"\t", b"\r", b"\n"):
                before -= 1
            run = before
            while run > 0 and parsed[run - 1 : run] == b" " and original[run - 1 : run] not in (b" ", b"\t", b"\r", b"\n"):
                run -= 1
            if run == before or self._inside_a_directive(original, run) or (original.count(b"\n", 0, run) + 1) in self._definer_lines:
                return position
            position = run

    @staticmethod
    def _inside_a_directive(text: bytes, position: int) -> bool:
        """Whether `position` is in a preprocessor directive: its line starts with `#`, or the line before it ends in a
        backslash (a macro that goes on). What is blanked there is not a macro in front of a function."""
        line_start = text.rfind(b"\n", 0, position) + 1
        if text[line_start:position + 200].split(b"\n", 1)[0].lstrip().startswith(b"#"):
            return True
        while line_start > 0:
            previous_end = line_start - 1
            previous_start = text.rfind(b"\n", 0, previous_end) + 1
            previous = text[previous_start:previous_end].rstrip(b"\r")
            if previous.endswith(b"\\"):
                if previous.lstrip().startswith(b"#"):
                    return True
                line_start = previous_start
                continue
            return False
        return False

    _MEMBER_LESS = re.compile(r"(?:\.|->)\s*[A-Za-z_]\w*\s*(<)(?![=<])")
    #: `va_arg(list, const char*)`: a type as the second argument of what looks like a call.
    _VA_ARG = re.compile(r"\bva_arg\s*\(\s*\w+\s*,\s*([^()]*?)\s*\)")
    #: `"%" PRId64 "..."`: a macro between string literals.
    _STRING_MACRO = re.compile(r'(")[ \t]+\b(?!(?:and|or|xor|not|bitand|bitor|compl|and_eq|or_eq|xor_eq|not_eq)\b)([A-Za-z_][A-Za-z0-9_]{2,})\b(?=[ \t]*(?:"|,|\)))')

    @staticmethod
    def _is_preprocessor_line(text: str, position: int) -> bool:
        start = text.rfind("\n", 0, position) + 1
        return text[start:position].lstrip().startswith("#") or text[start:start + 200].lstrip().startswith("#")

    @staticmethod
    def _closes_as_template(text: str, less: int) -> bool:
        """Whether the `<` at `less` opens a template argument list: a matching `>` is found before the end of the
        statement, and what follows it is a call or a scope (`x.get<int>()`, `T::template f<int>::type`)."""
        depth, parens = 1, 0
        position = less + 1
        limit = min(len(text), less + 400)
        while position < limit:
            char = text[position]
            if char == "<":
                depth += 1
            elif char == ">" and text[position - 1] != "-":
                depth -= 1
                if depth == 0:
                    after = position + 1
                    while after < len(text) and text[after] in " \t":
                        after += 1
                    return text.startswith("(", after) or text.startswith("::", after)
            elif char == "(":
                parens += 1
            elif char == ")":
                if parens == 0:
                    return False
                parens -= 1
            elif char in ";{}":
                return False
            position += 1
        return False

    def _rewrite_for_parsing(self, text: str) -> tuple[str, int]:
        """A copy of `text` with the constructs this grammar cannot read replaced by something it can, the same length
        and in the same place, so no line or column moves. Used only for a file whose first parse has an error, and only
        for parsing: the stored text is the code."""
        characters = list(text)
        changed = 0
        for match in self._MEMBER_LESS.finditer(text):
            less = match.start(1)
            if self._is_preprocessor_line(text, less) or self._closes_as_template(text, less):
                continue
            characters[less] = "-"  # a comparison reads as an arithmetic operator
            changed += 1
        for match in self._VA_ARG.finditer(text):
            start, end = match.span(1)
            if end > start and not self._is_preprocessor_line(text, start):
                characters[start:end] = list("0" + " " * (end - start - 1))
                changed += 1
        for match in self._STRING_MACRO.finditer(text):
            start, end = match.span(2)
            if not self._is_preprocessor_line(text, start):
                characters[start:end] = list(" " * (end - start))
                changed += 1
        return "".join(characters), changed

    #: What is not code to the brace scan: comments, raw string literals (`R"(...)"`, with any delimiter: shader and SQL
    #: text is often written that way), strings and characters.
    _NOT_CODE = re.compile(r"//[^\n]*|/\*.*?\*/|(?:u8|u|U|L)?R\"([^\s()\\\"]{0,16})\(.*?\)\1\"|\"(?:\\.|[^\"\\\n])*\"|'(?:\\.|[^'\\\n])*'", re.DOTALL)
    _FUNCTION_TAIL = re.compile(r"\)\s*(?:(?:const|noexcept(?:\s*\([^()]*\))?|override|final|volatile|mutable|&&?|->\s*[\w:<>\s*&,]+?)\s*)*$")

    def _definitions_by_scan(self, text: str) -> list[int]:
        """Lines of the definitions a plain look at the braces finds, whatever the grammar made of them: a `{` that follows
        the parameter list of something named (and is not inside another function), the name's line. A cross-check
        that does not depend on the parse tree, so a function the grammar dropped without an error is still seen."""
        blanked = self._NOT_CODE.sub(lambda match: re.sub(r"[^\n]", " ", match.group(0)), text)
        blanked = "\n".join(" " * len(line) if line.lstrip().startswith("#") else line for line in blanked.split("\n"))
        stack: list[bool] = []  # True: the braces of a function body
        found: list[int] = []
        newlines = [index for index, char in enumerate(blanked) if char == "\n"]  # to turn a position into a line number
        for match in re.finditer(r"[{}]", blanked):
            if blanked[match.start()] == "}":
                if stack:
                    stack.pop()
                continue
            if stack and stack[-1]:
                stack.append(True)  # inside a function everything is part of it
                continue
            head = blanked[max(0, match.start() - 700):match.start()]
            tail = self._FUNCTION_TAIL.search(head)
            name = None
            if tail:
                close = max(0, match.start() - 700) + tail.start()
                name = self._function_name_before(blanked, close)
            if name:
                found.append(bisect.bisect_left(newlines, blanked.rfind(name, 0, match.start())) + 1)
                stack.append(True)
            else:
                stack.append(False)
        return found

    def _missed_definitions(self, text: str, functions: list[ExtractedFunction]) -> list[int]:
        """Lines of definitions the scan of the braces sees and no extracted function covers."""
        spans = [(function.start_line, function.end_line) for function in functions]
        return sorted({line for line in self._definitions_by_scan(text) if not any(a <= line <= b for a, b in spans)})

    @staticmethod
    def _error_count(root: Node) -> int:
        count, stack = 0, [root]
        while stack:
            node = stack.pop()
            if node.type == "ERROR" or node.is_missing:
                count += 1
            if node.has_error:
                stack.extend(node.children)
        return count

    def _better_tree(self, prepared: str, tree):
        """The tree with the fewest errors among the first parse, the rewritten copy and the copy with only the first
        branch of each `#if`, and the text it was made from."""
        best, best_errors, best_text = tree, self._error_count(tree.root_node), prepared
        rewritten, changed = self._rewrite_for_parsing(prepared)
        if changed:
            second = self._parser.parse(bytes(rewritten, "utf8"))
            errors = self._error_count(second.root_node)
            if errors < best_errors:
                best, best_errors, best_text = second, errors, rewritten
        if best_errors:
            # Braces that open in one branch of an `#if` and close in the other cannot be read as written: read only the
            # first branch of each (the positions of everything left do not move).
            base = rewritten if changed else prepared
            single, dropped = self._first_branches_only(base)
            if dropped:
                third = self._parser.parse(bytes(single, "utf8"))
                if self._error_count(third.root_node) < best_errors:
                    return third, single, base
        return best, best_text, None

    def _functions_of_other_branches(self, base: str, first_only: str, source_code: str, found: list[ExtractedFunction], is_header: bool, c_mode: bool = False) -> list[ExtractedFunction]:
        """The functions that only the `#else` and `#elif` branches define, when the tree read had only the first branch of
        each `#if` (`first_only`, made from `base`). A text that keeps another branch is parsed only when the lines it adds
        can hold a definition, and a function read from it is kept only when it is a function: its head names it, it does
        not start inside a macro, and no other function starts inside it (an unbalanced brace in a text that mixes
        branches makes one function swallow the rest of the file)."""
        known = {(function.start_line, function.qualified_name) for function in found}
        covered = [(function.start_line, function.end_line) for function in found]
        candidates: list[ExtractedFunction] = []
        for variant in self._other_branch_texts(base):
            fresh = self._lines_only_in(variant, first_only)
            if not self._may_define(fresh, covered, declarations=c_mode):
                continue
            if c_mode and self._balanced(fresh):
                # C has no namespace or class for a function to be in: the added lines alone are read, in their places
                # (the rest blanked), which is most of a file less to parse
                shown = dict(fresh)
                variant = "\n".join(shown.get(number, "") if number in shown else _blank(line) for number, line in enumerate(variant.split("\n"), 1))
            extra: list[ExtractedFunction] = []
            self._extract_functions(self._parser.parse(bytes(variant, "utf8")).root_node, source_code, extra, namespace=None, class_name=None, is_header=is_header)
            for function in extra:
                if (function.start_line, function.qualified_name) not in known:
                    known.add((function.start_line, function.qualified_name))
                    candidates.append(function)
        starts = sorted({function.start_line for function in found} | {function.start_line for function in candidates})
        lines = source_code.split("\n")
        kept = []
        for function in candidates:
            if self._is_a_function_of_its_own(function, starts, lines):
                kept.append(function)
        return kept

    @staticmethod
    def _lines_only_in(variant: str, first_only: str) -> list[tuple[int, str]]:
        """`(line number, text)` of the lines of `variant` that `first_only` does not have: what the other branch adds."""
        return [
            (number, line)
            for number, (line, other) in enumerate(zip(variant.split("\n"), first_only.split("\n")), 1)
            if line.strip() and not other.strip()
        ]

    #: A brace that follows a parameter list: the shape of the start of a function body.
    _BODY_START = re.compile(r"\)\s*(?:(?:const|noexcept|override|final|volatile|mutable)\s*)*\{")

    #: A declaration without a body: `int name(int a, char *b);`.
    _DECLARATION = re.compile(r"[A-Za-z_]\w*\s*\([^;{}()]*(?:\([^;{}()]*\)[^;{}()]*)*\)\s*;")

    def _may_define(self, added: list[tuple[int, str]], covered: list[tuple[int, int]], declarations: bool = False) -> bool:
        """Whether the lines a branch adds can hold a function definition that no function found covers: a brace after a
        parameter list, and either the brace scan sees a definition there or their braces do not balance (a function that
        continues in the lines both branches share, so that the scan cannot tell). Only the added lines are looked at."""
        if not added:
            return False
        compact = "\n".join(text for _number, text in added)
        code = self._NOT_CODE.sub(lambda match: re.sub(r"[^\n]", " ", match.group(0)), compact)
        code = "\n".join(" " * len(line) if line.lstrip().startswith("#") else line for line in code.split("\n"))
        flat = code.replace("\n", " ")
        if not self._BODY_START.search(flat):
            # (a C file's added lines are read alone, which costs little: its declarations are read too, as the first
            # reading reads those of its own branch)
            return declarations and bool(self._DECLARATION.search(flat))
        if code.count("{") != code.count("}"):
            return True
        return any(not any(a <= added[line - 1][0] <= b for a, b in covered) for line in self._definitions_by_scan(compact))

    def _balanced(self, added: list[tuple[int, str]]) -> bool:
        """Whether the braces of the added lines close (code only, comments and strings left out)."""
        code = self._NOT_CODE.sub(lambda match: re.sub(r"[^\n]", " ", match.group(0)), "\n".join(text for _number, text in added))
        return code.count("{") == code.count("}")

    @staticmethod
    def _is_a_function_of_its_own(function: ExtractedFunction, starts: list[int], lines: list[str]) -> bool:
        """What was read is one function and starts where it is written: the name is in its first lines, the line before
        does not continue a macro (a line that ends in a backslash), and no other function starts inside it."""
        if function.start_line > 1 and lines[function.start_line - 2].rstrip().endswith("\\"):
            return False
        name = function.name.lstrip("~")
        if name and name not in " ".join(lines[function.start_line - 1 : function.start_line + 3]):
            return False
        inside = bisect.bisect_right(starts, function.end_line) - bisect.bisect_right(starts, function.start_line)
        return inside == 0

    _CPLUSPLUS_ONLY = re.compile(r"^\s*#\s*(?:ifdef\s+__cplusplus\b|if\s+defined\s*\(?\s*__cplusplus\s*\)?\s*$|if\s+__cplusplus\b)")
    _NOT_CPLUSPLUS = re.compile(r"^\s*#\s*(?:ifndef\s+__cplusplus\b|if\s+!\s*defined\s*\(?\s*__cplusplus\s*\)?\s*$)")

    def _cplusplus_only_lines(self, text: str) -> set[int]:
        """The (1-based) lines that only a C++ compiler reads: the first branch of `#ifdef __cplusplus`, and the `#else`
        branch of `#ifndef __cplusplus` (a nested conditional inside either is part of it)."""
        hidden: set[int] = set()
        stack: list[list] = []  # [branch hidden now (this frame's own), branch that is hidden when the conditional's first branch is the C one]
        for number, line in enumerate(text.split("\n"), 1):
            match = self._CONDITIONAL.match(line)
            word = match.group(1) if match else None
            if word in ("if", "ifdef", "ifndef"):
                first_hidden = bool(self._CPLUSPLUS_ONLY.match(line))
                only_in_else = bool(self._NOT_CPLUSPLUS.match(line))
                stack.append([first_hidden, only_in_else, 0])
            elif word in ("else", "elif") and stack:
                stack[-1][2] += 1
            elif word == "endif" and stack:
                stack.pop()
                continue
            if stack and any((frame[0] and frame[2] == 0) or (frame[1] and frame[2] >= 1) for frame in stack):
                hidden.add(number)
        return hidden

    #: The most branches of one `#if` that are read when only the first of each could be (a chain of platforms has several).
    _MAX_BRANCHES_READ = 6

    def _other_branch_texts(self, text: str) -> list[str]:
        """The texts that keep branch 1, 2, ... of each conditional (the first, where one has no such branch) and blank
        the others and the lines of the conditionals, all the same length: what `_first_branches_only` leaves unread."""
        lines = text.split("\n")
        # how many branches each conditional has, by the line it opens on
        counts: dict[int, int] = {}
        open_lines: list[int] = []
        for index, line in enumerate(lines):
            match = self._CONDITIONAL.match(line)
            word = match.group(1) if match else None
            if word in ("if", "ifdef", "ifndef"):
                open_lines.append(index)
                counts[index] = 1
            elif word in ("else", "elif") and open_lines:
                counts[open_lines[-1]] += 1
            elif word == "endif" and open_lines:
                open_lines.pop()
        widest = min(max(counts.values(), default=1), self._MAX_BRANCHES_READ)
        variants = []
        for wanted in range(1, widest):
            out = list(lines)
            frames: list[list] = []  # [opened at, branch now, branch kept, whether the parent is kept]
            for index, line in enumerate(lines):
                match = self._CONDITIONAL.match(line)
                word = match.group(1) if match else None
                parent_kept = all(frame[1] == frame[2] and frame[3] for frame in frames[-1:]) if frames else True
                if word in ("if", "ifdef", "ifndef"):
                    keep = wanted if counts.get(index, 1) > wanted else 0
                    frames.append([index, 0, keep, parent_kept])
                    out[index] = _blank(line)
                elif word in ("else", "elif") and frames:
                    frames[-1][1] += 1
                    out[index] = _blank(line)
                elif word == "endif" and frames:
                    frames.pop()
                    out[index] = _blank(line)
                elif frames and not (frames[-1][1] == frames[-1][2] and frames[-1][3]):
                    out[index] = _blank(line)
            variants.append("\n".join(out))
        return variants

    _CONDITIONAL = re.compile(r"^\s*#\s*(if|ifdef|ifndef|elif|else|endif)\b")

    def _first_branches_only(self, text: str) -> tuple[str, int]:
        """`text` with every `#else` and `#elif` branch (and what is nested in it) blanked, and the lines of the
        conditionals themselves blanked too (a conditional with unbalanced braces in its branches is not readable as one),
        all the same length. Returns it and how many branches were dropped."""
        lines = text.split("\n")
        stack: list[bool] = []  # whether a branch being dropped is open
        dropped = 0
        for index, line in enumerate(lines):
            match = self._CONDITIONAL.match(line)
            word = match.group(1) if match else None
            dropping = any(stack)
            if word in ("if", "ifdef", "ifndef"):
                stack.append(False)
                lines[index] = _blank(line)
            elif word in ("else", "elif") and stack:
                if not dropping:
                    stack[-1] = True
                    dropped += 1
                lines[index] = _blank(line)
            elif word == "endif" and stack:
                stack.pop()
                lines[index] = _blank(line)
            elif dropping:
                lines[index] = _blank(line)
        return "\n".join(lines), dropped

    #: `template<> X<int>::~X<int>() = default;` is valid C++ the old grammar cannot read; it has no body to index.
    _HARMLESS_LINE = re.compile(r"^\s*template\s*<\s*>.*=\s*(?:default|delete)\s*;\s*$")
    #: `#if __has_warning("-Wx")`, `__has_include(<x>)`, `__has_feature(x)`: conditions of compiler probes the grammar cannot read.
    _PROBE_CONDITION = re.compile(r"^(\s*#\s*)(if|elif)\b.*\b__has_(?:warning|include|include_next|feature|builtin|attribute|cpp_attribute|extension)\b.*$")

    #: Macros that decorate a declaration without being part of it. Export macros are everywhere in C++ libraries.
    DEFAULT_DECORATOR_PATTERNS = (
        r"\b[A-Z][A-Z0-9]*_(?:API|EXPORT|EXPORTS|DECL|DLL|DLLEXPORT|VISIBLE|PUBLIC|LOCAL)\b",
        r"\b(?:Q_DECL_EXPORT|Q_DECL_IMPORT|Q_DECL_HIDDEN|EXPORT|DLLEXPORT|DLL_EXPORT|APIENTRY|WINAPI|CALLBACK)\b",
        r"\b[A-Z][A-Z0-9]*_(?:CDECL|STDCALL|FASTCALL|VECTORCALL|FORCEINLINE|NOINLINE|NORETURN|NODISCARD|CALLCONV)\b",
        r"\b__(?:cdecl|stdcall|fastcall|vectorcall|forceinline|inline)\b",
        r"__declspec\s*\(\s*\w+\s*\)",
        r"__attribute__\s*\(\((?:[^()]|\([^()]*\))*\)\)",
    )
    PROJECT_MACROS_FILE = "cpp_macros.txt"

    def _decorator_pattern(self) -> "re.Pattern[str]":
        """The default patterns and the macro names the project lists, one per line, in `_drspec/cpp_macros.txt`."""
        names: list[str] = []
        try:
            for line in (Path.cwd() / "_drspec" / self.PROJECT_MACROS_FILE).read_text(encoding="utf-8").splitlines():
                line = line.split("#", 1)[0].strip()
                if re.fullmatch(r"[A-Za-z_]\w*", line):
                    names.append(re.escape(line))
        except OSError:
            pass
        parts = list(self.DEFAULT_DECORATOR_PATTERNS) + ([r"\b(?:" + "|".join(names) + r")\b"] if names else [])
        return re.compile("|".join(f"(?:{part})" for part in parts))

    _DEFINE_HEAD = re.compile(r"^[ \t]*#[ \t]*define[ \t]+\w+(?:\([^)]*\))?")

    def _prepare(self, source_code: str) -> tuple[str, int]:
        """The source with harmless constructs and decorator macros replaced by blanks of the same size (so no line or column
        moves), and how many harmless constructs were skipped."""
        skipped = 0
        lines = source_code.split("\n")
        pattern = self._decorator_pattern()
        for index, line in enumerate(lines):
            probe = self._PROBE_CONDITION.match(line)
            if self._HARMLESS_LINE.match(line):
                lines[index] = _blank(line)
                skipped += 1
            elif probe:  # the condition is replaced by a true one, so the lines under it are read and `#endif` still pairs
                replacement = probe.group(1) + probe.group(2) + " 1"
                lines[index] = replacement + " " * (len(line) - len(replacement))
                skipped += 1
            elif pattern.search(line) and not self._DEFINE_HEAD.match(line):
                # (a `#define` line is left as it is: with its name or body blanked, the grammar reads the next line as
                # part of the directive)
                lines[index] = pattern.sub(lambda match: " " * len(match.group(0)), line)
        return self._blank_destructor_arguments(self._blank_braced_defaults(self._blank_macros("\n".join(lines)))), skipped

    #: A destructor named with its template arguments, in the definition of a member of a class template: `A<X>::~A<X>() = default;`.
    _DESTRUCTOR_ARGUMENTS = re.compile(r"::\s*~\s*[A-Za-z_]\w*\s*(<[^;(){}<>]*(?:<[^;(){}]*>[^;(){}<>]*)*>)(?=\s*\()")

    def _blank_destructor_arguments(self, text: str) -> str:
        """The template arguments of a destructor's own name (`A<X>::~A<X>()`) become blanks of the same size: the grammar reads
        no argument list there, errors on the line, and the code after it is lost with it. The function keeps its name `A::~A`
        (the same one `A<T>::~A()` has)."""
        if "~" not in text or "<" not in text:
            return text
        code = self._NOT_CODE.sub(lambda match: re.sub(r"[^\n]", " ", match.group(0)), text)  # (comments and strings do not count)
        found = [match for match in self._DESTRUCTOR_ARGUMENTS.finditer(code) if not self._is_preprocessor_line(code, match.start())]
        if not found:
            return text
        characters = list(text)
        for match in found:
            start, end = match.span(1)
            characters[start:end] = [" "] * (end - start)
        return "".join(characters)

    #: `= {` after a parameter: the default argument of a function written as a braced list (`const Pose& p = {}`).
    _BRACED_DEFAULT = re.compile(r"(?<![=!<>+\-*/%&|^])=(?!=)\s*\{")

    def _blank_braced_defaults(self, text: str) -> str:
        """A default argument written as a braced list (`= {}`, `= {1.0, 0.0}`, `= {Vec(0., 0.)}`) becomes `= 0` followed by blanks
        of the same size: the grammar reads no braced list there, errors in the head, and the function is lost. Only an `=` inside
        parentheses whose list is followed by `,` or `)` is touched; a declaration `T x = {…};` is read as it is."""
        if "{" not in text or "=" not in text:
            return text
        code = self._NOT_CODE.sub(lambda match: re.sub(r"[^\n]", " ", match.group(0)), text)  # (comments and strings do not count)
        found = [match for match in self._BRACED_DEFAULT.finditer(code)]
        if not found:
            return text
        characters = list(text)
        depth_at, depth = {}, 0
        wanted = {match.start() for match in found}
        for index, char in enumerate(code):  # the parentheses open at each candidate `=`
            if index in wanted:
                depth_at[index] = depth
            if char == "(":
                depth += 1
            elif char == ")":
                depth = max(0, depth - 1)
        for match in found:
            if depth_at.get(match.start(), 0) == 0 or self._is_preprocessor_line(code, match.start()):
                continue
            opening = match.end() - 1
            level, close = 0, None
            for index in range(opening, min(len(code), opening + 2000)):
                if code[index] == "{":
                    level += 1
                elif code[index] == "}":
                    level -= 1
                    if level == 0:
                        close = index
                        break
            if close is None or not re.match(r"\s*[,)]", code[close + 1 :]):
                continue
            blank = []
            for index in range(opening, close + 1):  # the braced list: a `0`, then blanks (a line break stays one)
                char = text[index]
                blank.append("\n" if char == "\n" else " " * len(char.encode("utf-8")))
            blank[0] = "0"
            characters[opening : close + 1] = blank + [""] * (close + 1 - opening - len(blank))
        return "".join(characters)

    _CONTROL_WORDS = frozenset("if for while switch catch return sizeof alignof decltype typeof defined else do".split())
    #: A macro invocation after the parameter list of a function, before `;` or the body: `void F(const char* fmt, ...) ATTR(2);`
    _TRAILING_MACRO = re.compile(r"\)(\s*)([A-Z][A-Z0-9_]{2,})(\s*\((?:[^()]|\([^()]*\))*\))?(?=\s*[;{])")
    #: A macro in front of a member or function with nothing else to say what it is: `IM_NOINLINE constexpr W() {…}`.
    _LEADING_MACRO = re.compile(r"(?m)^([ \t]*)([A-Z][A-Z0-9]*(?:_[A-Z0-9]+)+)([ \t]+)(?=(?:constexpr|inline|static|virtual|explicit|friend|extern)\b)")
    #: A macro on a line of its own (`IM_MSVC_RUNTIME_CHECKS_OFF`): not a statement, so it is not read as one.
    _LONE_MACRO = re.compile(r"(?m)^([ \t]*)([A-Z][A-Z0-9]*(?:_[A-Z0-9]+)+)[ \t]*$")

    def _blank_macros(self, text: str) -> str:
        """Blank the macro invocations that decorate a declaration, with spaces of the same length. Unlike an error,
        these are read without complaint as something else, and the functions beside them are lost silently."""
        characters = list(text)

        def blank(start: int, end: int) -> None:
            characters[start:end] = list(" " * (end - start))

        for match in self._LEADING_MACRO.finditer(text):
            if not self._is_preprocessor_line(text, match.start(2)):
                blank(*match.span(2))
        lone = [match for match in self._LONE_MACRO.finditer(text) if not self._is_preprocessor_line(text, match.start(2))]
        if lone:
            code = self._NOT_CODE.sub(lambda match: re.sub(r"[^\n]", " ", match.group(0)), text)  # (comments and strings do not count)
            depth_before = self._parenthesis_depths(code, [match.start(2) for match in lone])
            for match, depth in zip(lone, depth_before):
                # (an argument on a line of its own is not a decoration: inside parentheses, or after the `,` of a list)
                if depth == 0 and not re.search(r"[,(]\s*$", code[max(0, match.start(2) - 200) : match.start(2)]):
                    blank(*match.span(2))
        for match in self._TRAILING_MACRO.finditer(text):
            if self._is_preprocessor_line(text, match.start()):
                continue
            if self._function_name_before(text, match.start()) is None:
                continue
            blank(match.start(2), match.end(3) if match.group(3) else match.end(2))
        return "".join(characters)

    @staticmethod
    def _parenthesis_depths(code: str, positions: list[int]) -> list[int]:
        """How many parentheses are open before each of the (ascending) positions, in one pass over the code."""
        depths, depth, index = [], 0, 0
        for position in positions:
            while index < position:
                char = code[index]
                if char == "(":
                    depth += 1
                elif char == ")" and depth:
                    depth -= 1
                index += 1
            depths.append(depth)
        return depths

    def _function_name_before(self, text: str, close: int) -> Optional[str]:
        """The identifier right before the `(` that the `)` at `close` closes, unless it is a control keyword (so
        `if (x) FOO(1);` is left alone) or there is none."""
        depth, position = 0, close
        while position >= 0 and close - position < 600:
            char = text[position]
            if char == ")":
                depth += 1
            elif char == "(":
                depth -= 1
                if depth == 0:
                    break
            position -= 1
        else:
            return None
        if position < 0:
            return None
        match = re.search(r"([A-Za-z_~][\w:~]*)\s*$", text[max(0, position - 80):position])
        if not match or match.group(1) in self._CONTROL_WORDS:
            return None
        return match.group(1)

    _DEFINITION_LINE = re.compile(
        r"^\s*[\w:<>,~*&\s]*\b[A-Za-z_~][\w:]*\s*\([^;{}]*\)\s*(?:const|noexcept|override|final|\s)*\{"  # a function
        r"|^\s*(?:class|struct)\s+[\w\s]*\w(?:\s*:[^{;]*)?\s*\{?\s*(?://.*)?$"  # a class, whose methods go with it
    )

    #: A line that starts a control statement (`if (x) {`, `} else if (y) {`, `while (z) {`): not the head of a function.
    _CONTROL_LINE = re.compile(r"^\s*(?:\}\s*)?(?:else\s+)?(?:if|for|while|switch|catch|do|return|sizeof|case)\b")

    def _suspected_losses(self, root: Node, lines: list[str], extracted: list[ExtractedFunction]) -> list[int]:
        """Lines inside an error region that look like the start of a function definition the parser did not
        extract. The error swallowed them, so they cannot be reported as skipped by name."""
        starts = {function.start_line for function in extracted}
        losses: list[int] = []

        def visit(node: Node) -> None:
            if node.type == "ERROR":
                for row in range(node.start_point[0], node.end_point[0] + 1):
                    if (
                        row < len(lines) and self._DEFINITION_LINE.match(lines[row]) and (row + 1) not in starts
                        and not self._CONTROL_LINE.match(lines[row])
                    ):
                        losses.append(row + 1)
                return
            for child in node.children:
                if child.has_error:
                    visit(child)

        if root.has_error:
            visit(root)
            losses.extend(self._lost_function_heads(root, starts))
        return sorted(set(losses))

    #: The name in front of the first parenthesis of a head: `a::b`, `~A`, `A::~A`, `operator+`, `operator()`.
    _HEAD_NAME = re.compile(r"((?:operator\s*(?:\(\s*\)|[^\s\w(]+|\w+)|[~\w]+)(?:\s*::\s*(?:operator\s*(?:\(\s*\)|[^\s\w(]+)|~?\w+))*)\s*\(")

    def _names_of_losses(self, text: str, lines: list[int]) -> dict[int, str]:
        """`{line: name}` for the lines of suspected losses whose text gives one: the name in front of the first
        parenthesis, looking at the line and the few after it (a head may be split), with comments and strings left out.
        A user can then tell which function is missing."""
        code = self._NOT_CODE.sub(lambda match: re.sub(r"[^\n]", " ", match.group(0)), text).split("\n")
        names: dict[int, str] = {}
        for line in lines:
            if not 1 <= line <= len(code):
                continue
            head = " ".join(part.strip() for part in code[line - 1 : line + 4])
            written = code[line - 1].strip()
            if self._CLASS_HEAD.match(written):
                names[line] = re.sub(r"\s+", " ", written.rstrip("{ ")).strip()  # `struct name`: what is lost is its body
                continue
            for found in self._HEAD_NAME.finditer(head):
                name = re.sub(r"\s+", "", found.group(1))
                if name in self._NOT_A_NAME or self._is_prefix_macro(head, found.end()):
                    continue
                names[line] = name
                break
        return names

    #: The head of a class, struct, union or enum written alone on its line (`struct name`, `class A : public B {`).
    _CLASS_HEAD = re.compile(r"^(?:typedef\s+)?(?:class|struct|union|enum(?:\s+class)?)\s+[\w\s:,<>]*\w\s*\{?$")

    @staticmethod
    def _is_prefix_macro(head: str, opened: int) -> bool:
        """`NAME(args) rest(`: the parenthesis at `opened` (just past a `(`) closes and a word follows, so the name in
        front of it decorates what follows (`MA_ATOMIC(4, int) counter`) and is not the function's own."""
        depth = 1
        for index in range(opened, len(head)):
            if head[index] == "(":
                depth += 1
            elif head[index] == ")":
                depth -= 1
                if depth == 0:
                    rest = head[index + 1 :].lstrip()
                    return bool(rest) and (rest[0].isalpha() or rest[0] == "_") and not rest.startswith(("const", "noexcept", "override", "final"))
        return False

    #: Words in front of a parenthesis that are not the name of a function.
    _NOT_A_NAME = frozenset({"if", "for", "while", "switch", "catch", "return", "sizeof", "defined", "static_assert", "alignas", "decltype", "typeof", "__attribute__", "__declspec", "void", "int"})

    def _lost_function_heads(self, root: Node, starts: set[int]) -> list[int]:
        """Lines where a stretch that failed to parse looks like the head and body of a function: a name, an opening
        parenthesis and a brace or a statement. Such a function is not indexed, so it is counted as skipped."""
        lines: list[int] = []

        def visit(node: Node) -> None:
            if node.type == "ERROR":
                for run in self._loose_runs(node):
                    types = {part.type for part in run}
                    opens = "(" in types and ("identifier" in types or "qualified_identifier" in types or "field_identifier" in types)
                    has_body = "{" in types or bool(types & {"return_statement", "compound_statement", "expression_statement"})
                    if opens and has_body and (run[0].start_point[0] + 1) not in starts:
                        lines.append(run[0].start_point[0] + 1)
                return
            for child in node.children:
                if child.has_error:
                    visit(child)

        visit(root)
        return lines

    _COMPLETE_SUFFIXES = ("_definition", "_declaration", "_specifier", "_statement", "_item", "_directive")
    _COMPLETE_TYPES = frozenset({"comment", "declaration", "preproc_call", "preproc_def", "preproc_include", "preproc_ifdef", "preproc_if"})

    def _is_complete(self, node: Node) -> bool:
        """A node that parsed as a whole top-level construct, so it cannot be where the error is."""
        return node.is_named and (node.type in self._COMPLETE_TYPES or node.type.endswith(self._COMPLETE_SUFFIXES))

    def _loose_runs(self, node: Node) -> list[list[Node]]:
        """Inside an error node, the stretches of children that did not parse into a construct: loose tokens
        (a type, a name, a bracket) around the complete constructs the parser did recover."""
        runs: list[list[Node]] = []
        current: list[Node] = []
        for child in node.children:
            if self._is_complete(child):
                if current:
                    runs.append(current)
                current = []
            elif child.type == "ERROR":
                if current:
                    runs.append(current)
                current = []
                runs.extend(self._loose_runs(child))
            else:
                current.append(child)
        if current:
            runs.append(current)
        return runs

    def _error_start(self, node: Node) -> tuple[int, int]:
        """Where an error really is. An error node that wraps the whole file starts at the file's first line, but
        the first stretch that failed to parse is where the text went wrong."""
        runs = self._loose_runs(node)
        if runs:
            first = runs[0][0]
            return first.start_point[0] + 1, first.start_point[1]
        return node.start_point[0] + 1, node.start_point[1]

    def _collect_errors(self, node: Node, errors: list[ParseError]) -> None:
        """Recursively collect syntax errors from the AST (only the subtrees that hold one are entered)."""
        if not (node.has_error or node.type == "ERROR" or node.is_missing):
            return
        if node.type == "ERROR" or node.is_missing:
            line, column = self._error_start(node) if node.type == "ERROR" else (node.start_point[0] + 1, node.start_point[1])
            errors.append(
                ParseError(
                    line=line,
                    column=column,
                    message=f"Syntax error: {node.type}" if node.type == "ERROR" else "Missing token",
                )
            )

        for child in node.children:
            self._collect_errors(child, errors)

    def _extract_calls_for_function(self, func_node: Node, source_code: str) -> list[RawCall]:
        """Collect every call expression in `func_node`, not descending
        into a nested function's own body."""
        calls: list[RawCall] = []
        for child in func_node.children:
            self._collect_calls(child, source_code, calls)
        return calls

    def _collect_calls(self, node: Node, source_code: str, calls: list[RawCall]) -> None:
        if node.type == "call_expression":
            call = self._extract_call(node, source_code)
            if call is not None:
                calls.append(call)
        if node.type == "function_definition":
            return  # a nested function has its own calls (a lambda's calls are the enclosing function's)
        for child in node.children:
            self._collect_calls(child, source_code, calls)

    def _extract_call(self, node: Node, source_code: str) -> Optional[RawCall]:
        """Extract one `call_expression` node into a `RawCall`, or `None`
        if its receiver is too complex to represent (never guessed as a
        bare call instead — see `RawCall`'s docstring)."""
        function_part = node.child_by_field_name("function")
        if function_part is None:
            return None

        text = self._get_node_text(node, source_code)

        if function_part.type == "identifier":
            return RawCall(receiver=None, name=function_part.text.decode("utf8"), text=text)

        if function_part.type == "field_expression":
            # `obj.method(...)` / `obj->method(...)` / `this->method(...)`
            # — tree-sitter-cpp uses the same node type for both `.` and
            # `->`, so no separate handling is needed for either.
            argument_node = function_part.child_by_field_name("argument")
            field_node = function_part.child_by_field_name("field")
            if argument_node is None or field_node is None:
                return None
            name = field_node.text.decode("utf8")

            # A name, `this`, or a short chain of them: `ctx`, `this->ctx`, `getSteering()`, `bodies[]`, `h.get()`
            # (arguments and indices left out). Anything else is too complex to represent and is skipped.
            if self._c_mode and argument_node.type not in ("this", "identifier"):
                return None  # C has no methods to find through a chain: a call through a member stays out, as before
            receiver = self._receiver_expression(argument_node)
            if receiver is None:
                return None

            return RawCall(receiver=receiver, name=name, text=text)

        if function_part.type == "qualified_identifier":
            # `Namespace::func(...)` and the explicit-base-class-call idiom
            # `Base::method(...)` are structurally identical in this
            # grammar; telling them apart needs the class hierarchy, which
            # isn't tracked here. Reported as a module-qualified-shaped
            # receiver regardless — it only ever resolves via a real
            # namespace import, never by guessing it's a base class, so
            # the ambiguity is harmless.
            scope_node = function_part.child_by_field_name("scope")
            name_node = function_part.child_by_field_name("name")
            if scope_node is None or name_node is None:
                return None
            if scope_node.type not in ("namespace_identifier", "identifier"):
                return None
            return RawCall(receiver=scope_node.text.decode("utf8"), name=name_node.text.decode("utf8"), text=text)

        return None  # some other call shape — skip

    #: Method names that change the object they are called on (a standard container, a string, a smart pointer): a member
    #: receiving one of them is written. The class of the receiver is not looked at: a name list, as the first rule.
    _MUTATING_METHODS = frozenset(
        "push_back push_front emplace emplace_back emplace_front insert erase clear resize assign pop pop_back pop_front push "
        "reserve swap append remove remove_if sort reverse splice merge reset release fill shrink_to_fit insert_or_assign "
        "try_emplace extract replace add set".split()
    )

    def _extract_member_accesses(self, func_node: Node, class_name: Optional[str], declared: dict) -> list[RawMemberAccess]:
        """Every `a.m`, `p->m` and `this->m` of the body that is not the function of a call (a method call is a call), and,
        inside a method (`class_name` given), every bare name that is not a call, a declaration or a qualified name: the
        resolver decides which of those is a field of the class (a local or a parameter is not). A lambda's accesses are the
        enclosing function's; a nested function has its own."""
        accesses: list[RawMemberAccess] = []
        if self._c_mode:
            return accesses
        body = self._get_child_by_type(func_node, "compound_statement")
        if body is None:
            return accesses
        self._collect_member_accesses(body, accesses, class_name is not None, set(declared) | self._declared_names(func_node))
        return accesses

    def _declared_names(self, func_node: Node) -> set:
        """Every name the function declares itself, of any type (a parameter, a local, a loop variable, a lambda's parameter):
        a bare use of one of them is not a field of the class. (`declared_param_types` keeps only the types it can use.)"""
        names: set = set()

        def walk(node: Node) -> None:
            for child in node.children:
                if child.type == "lambda_expression":
                    continue  # its parameters and locals hide a field inside it only (`_collect_member_accesses` adds them there)
                if child.type == "declaration":  # (a `condition_clause` is not one: `if (flag)` holds an expression, and `if (int x = f())` a declaration the walk reaches)
                    type_node = child.child_by_field_name("type")
                    for item in child.children:
                        if item is type_node or item.type in (";", ",", "type_qualifier", "storage_class_specifier"):
                            continue
                        name = self._declarator_name(item)
                        if name:
                            names.add(name)
                elif child.type in ("parameter_declaration", "optional_parameter_declaration", "for_range_loop"):
                    declarator = child.child_by_field_name("declarator")
                    name = self._declarator_name(declarator) if declarator is not None else None
                    if name:
                        names.add(name)
                walk(child)

        walk(func_node)
        return names

    def _collect_member_accesses(self, node: Node, accesses: list[RawMemberAccess], bare: bool, declared: dict) -> None:
        for child in node.children:
            kind = child.type
            if kind == "function_definition":
                continue  # a nested function has its own
            if kind == "lambda_expression":
                self._collect_member_accesses(child, accesses, bare, declared | self._declared_names(child))
                continue
            if kind == "field_expression":
                access = self._field_access(child)
                if access is not None:
                    access.written_through = self._written_through(child)
                    accesses.append(access)
            elif bare and kind == "identifier" and child.text.decode("utf8") not in declared and self._is_bare_use(child):
                accesses.append(self._with_argument_position(
                    RawMemberAccess(receiver=None, member=child.text.decode("utf8"), kind=self._access_kind(child), line=child.start_point[0] + 1, text=child.text.decode("utf8")),
                    child,
                ))
            self._collect_member_accesses(child, accesses, bare, declared)

    def _written_through(self, node: Node) -> str:
        """The members a base is written through: the chain above `node` (`a.b` in `a.b.c.d = x`) goes through members (`.c`, `.d`)
        and subscripts (`a.b[0].c = x`) up to an assignment or an increment of its last member, and then those members are the names
        the write goes through (`c.d`). Empty when the chain is only read, when the base is itself the written thing, or when a call
        is in the way (`a.b.get()` is not a write of `b`). Only the base is marked: the written member keeps its own kind."""
        current = node
        names: list = []
        while current.parent is not None:
            parent = current.parent
            if parent.type == "subscript_expression" and self._same(parent.child_by_field_name("argument"), current):
                current = parent
            elif parent.type == "field_expression" and self._same(parent.child_by_field_name("argument"), current):
                field_node = parent.child_by_field_name("field")
                if field_node is None:
                    return ""
                names.append(field_node.text.decode("utf8"))
                current = parent
            else:
                break
        if not names or current is node:
            return ""
        return ".".join(names) if self._access_kind(current) in ("write", "readwrite") else ""

    @staticmethod
    def _same(first: Optional[Node], second: Optional[Node]) -> bool:
        return first is not None and second is not None and (first.start_byte, first.end_byte, first.type) == (second.start_byte, second.end_byte, second.type)

    def _field_access(self, node: Node) -> Optional[RawMemberAccess]:
        """`a.m` as a member access, unless it is the function of a call (then it is that call's method)."""
        parent = node.parent
        if parent is not None and parent.type == "call_expression" and self._same(parent.child_by_field_name("function"), node):
            return None
        argument = node.child_by_field_name("argument")
        field_node = node.child_by_field_name("field")
        if argument is None or field_node is None or field_node.type != "field_identifier":
            return None
        receiver = self._receiver_expression(argument)
        return self._with_argument_position(
            RawMemberAccess(
                receiver=receiver if receiver is not None else "",
                member=field_node.text.decode("utf8"),
                kind=self._access_kind(node),
                line=field_node.start_point[0] + 1,
                text=node.text.decode("utf8")[:200],
            ),
            node,
        )

    def _with_argument_position(self, access: RawMemberAccess, node: Node) -> RawMemberAccess:
        """A read that is an argument of a call also says which call and which argument (`f(r.data)`, `obj.set(x.v, 2)`), for the
        resolver to look at the callee's parameter: a non-const reference makes it a read and a write."""
        if access.kind != "read":
            return access
        top, parent = node, node.parent
        while parent is not None and parent.type == "parenthesized_expression":
            top, parent = parent, parent.parent
        if parent is None or parent.type != "argument_list" or parent.parent is None or parent.parent.type != "call_expression":
            return access
        function = parent.parent.child_by_field_name("function")
        if function is None:
            return access
        position = 0
        for child in parent.children:
            if child.type in ("(", ")", ","):
                continue
            if self._same(child, top):
                break
            position += 1
        if function.type == "identifier":
            receiver, name = None, function.text.decode("utf8")
        elif function.type == "field_expression":
            argument, field_node = function.child_by_field_name("argument"), function.child_by_field_name("field")
            if argument is None or field_node is None:
                return access
            receiver, name = self._receiver_expression(argument), field_node.text.decode("utf8")
            if receiver is None:
                return access
        elif function.type == "qualified_identifier":
            scope, name_node = function.child_by_field_name("scope"), function.child_by_field_name("name")
            if scope is None or name_node is None or scope.type not in ("namespace_identifier", "identifier"):
                return access
            receiver, name = scope.text.decode("utf8"), name_node.text.decode("utf8")
        else:
            return access
        access.call_receiver, access.call_name, access.arg_index = receiver, name, position
        return access

    #: The parents in which an identifier is not the use of a variable: a call's function, a declaration, a parameter,
    #: a qualified name, a capture, a label, a type.
    _NOT_A_USE = frozenset(
        "call_expression init_declarator parameter_declaration optional_parameter_declaration qualified_identifier lambda_capture_specifier "
        "labeled_statement goto_statement declaration function_declarator pointer_declarator reference_declarator array_declarator "
        "field_declaration template_argument_list alias_declaration type_definition enumerator preproc_def preproc_function_def "
        "field_initializer for_range_loop sizeof_expression".split()
    )

    def _is_bare_use(self, node: Node) -> bool:
        parent = node.parent
        if parent is None:
            return False
        if parent.type == "call_expression":
            return not self._same(parent.child_by_field_name("function"), node)  # an argument is a use, the called name is not
        if parent.type in ("init_declarator", "for_range_loop"):
            return self._same(parent.child_by_field_name("value"), node) or self._same(parent.child_by_field_name("right"), node)
        if parent.type in ("field_initializer",):
            return False
        return parent.type not in self._NOT_A_USE

    def _access_kind(self, node: Node) -> str:
        """`write` for `=` (also of an element: `m[i] = x`), `readwrite` for `+=` and `++`, `write` for a mutating method called on
        it (`m.push_back(x)`), `read` for anything else."""
        top = node
        parent = top.parent
        while parent is not None and (
            (parent.type == "subscript_expression" and self._same(parent.child_by_field_name("argument"), top))
            or parent.type == "parenthesized_expression"
        ):
            top, parent = parent, parent.parent
        if parent is None:
            return "read"
        if parent.type == "assignment_expression" and self._same(parent.child_by_field_name("left"), top):
            operator = parent.child_by_field_name("operator")
            return "write" if operator is not None and operator.text == b"=" else "readwrite"
        if parent.type == "update_expression":
            return "readwrite"
        if parent.type == "field_expression" and self._same(parent.child_by_field_name("argument"), top):
            call = parent.parent
            method = parent.child_by_field_name("field")
            if (
                call is not None and call.type == "call_expression" and self._same(call.child_by_field_name("function"), parent)
                and method is not None and method.text.decode("utf8") in self._MUTATING_METHODS
            ):
                return "write"
        return "read"

    _MAX_RECEIVER = 160

    def _receiver_expression(self, node: Node) -> Optional[str]:
        """The receiver of a call as a short expression (see `cpp_types.split_chain`), or `None` when it is anything else
        than names, `this`, member access, calls and indexing."""
        text = self._chain(node)
        return text if text is not None and len(text) <= self._MAX_RECEIVER else None

    def _chain(self, node: Node) -> Optional[str]:
        kind = node.type
        if kind in ("identifier", "this", "field_identifier"):
            return node.text.decode("utf8")
        if kind == "qualified_identifier":
            text = node.text.decode("utf8")
            if re.fullmatch(r"(?:::)?[A-Za-z_]\w*(?:::[A-Za-z_]\w*)*", text):
                return text
            name = node.child_by_field_name("name")
            scope = node.child_by_field_name("scope")
            if name is not None and name.type == "template_function" and scope is not None:  # `std::make_shared<T>` is `std::` and the template
                templated = self._chain(name)
                return f"{scope.text.decode('utf8')}::{templated}" if templated is not None else None
            return None
        if kind == "field_expression":
            argument = node.child_by_field_name("argument")
            field = node.child_by_field_name("field")
            if argument is None or field is None or field.type != "field_identifier":
                return None
            base = self._chain(argument)
            if base is None:
                return None
            operator = node.child_by_field_name("operator")
            return f"{base}{'->' if operator is not None and operator.text == b'->' else '.'}{field.text.decode('utf8')}"
        if kind == "call_expression":
            function = node.child_by_field_name("function")
            base = self._chain(function) if function is not None else None
            return None if base is None else base + "()"
        if kind == "template_function":  # `std::make_shared<T>`: the pointer's type is `T`, read by the resolver from the text
            name = node.child_by_field_name("name")
            arguments = node.child_by_field_name("arguments")
            if name is not None and arguments is not None and re.fullmatch(r"(?:::)?(?:std::)?make_(?:shared|unique)", name.text.decode("utf8")):
                return name.text.decode("utf8") + arguments.text.decode("utf8")
            return None
        if kind == "subscript_expression":
            argument = node.child_by_field_name("argument")
            base = self._chain(argument) if argument is not None else None
            return None if base is None else base + "[]"
        if kind == "parenthesized_expression":
            inner = [child for child in node.children if child.type not in ("(", ")")]
            return self._chain(inner[0]) if len(inner) == 1 else None
        if kind == "pointer_expression":  # `*p`: the pointer's type is the class's, as everywhere here
            argument = node.child_by_field_name("argument")
            operator = node.child_by_field_name("operator")
            if argument is None or operator is None or operator.text != b"*":
                return None
            return self._chain(argument)
        return None

    def _extract_includes(self, root: Node) -> list[str]:
        """The targets of the file's `#include "..."` lines, in order. A header in angle brackets is a
        system or library header, never one of the project's files, and is left out."""
        found: list[str] = []
        for child in root.children:
            if child.type != "preproc_include":
                continue
            target = child.child_by_field_name("path")
            if target is not None and target.type == "string_literal":
                found.append(target.text.decode("utf8").strip('"'))
        return found

    _TYPE_NOISE = re.compile(r"\b(?:const|volatile|struct|class|union|enum|typename|static|extern|mutable|register|constexpr)\b")
    _TEMPLATE_ARGS = re.compile(r"<[^<>]*>")

    def _clean_type(self, text: str) -> Optional[str]:
        """A type as written, reduced to the name of the class it refers to: `const math::Calc&` -> `math::Calc`.
        `None` for `auto`, built-in types and anything that is not a plain, possibly qualified, name."""
        previous = None
        while previous != text:
            previous, text = text, self._TEMPLATE_ARGS.sub("", text)
        text = self._TYPE_NOISE.sub("", text)
        text = "".join(text.replace("&", " ").replace("*", " ").split())
        if not text or text == "auto" or not re.fullmatch(r"(?:::)?[A-Za-z_]\w*(?:::[A-Za-z_]\w*)*", text):
            return None
        return None if text in _CPP_BUILTIN_TYPES else text

    def _declarator_name(self, node: Node) -> Optional[str]:
        stack = [node]
        while stack:
            current = stack.pop()
            if current.type in ("identifier", "field_identifier"):
                return current.text.decode("utf8")
            if current.type in ("argument_list", "initializer_list", "parameter_list"):
                continue
            stack.extend(reversed(current.children))
        return None

    @staticmethod
    def _is_array_declarator(node: Optional[Node]) -> bool:
        """Whether a declarator (`bodies[8]`, `*bodies[8]`) declares an array."""
        while node is not None:
            if node.type == "array_declarator":
                return True
            if node.type in ("pointer_declarator", "reference_declarator", "init_declarator", "parenthesized_declarator"):
                inner = node.child_by_field_name("declarator")
                if inner is None:
                    inner = next((child for child in node.children if child.type.endswith("declarator")), None)
                node = inner
                continue
            return False
        return False

    def _variable_type(self, type_node: Optional[Node], declarator: Optional[Node]) -> Optional[str]:
        if type_node is None or type_node.type == "placeholder_type_specifier":
            return None
        return normalize_type(type_node.text.decode("utf8"), array=self._is_array_declarator(declarator))

    def _declared_types(self, function_node: Node) -> dict[str, str]:
        """Variable -> what is known of its type, from the function's parameters and the local declarations in its body
        (nested functions excluded; the parameters and locals of its lambdas count when they do not clash with a name
        already declared).

        A written type is kept as written, reduced to its name and template arguments (`std::vector<Body>`, `Body[]`
        for an array). `auto` is not guessed: a variable declared `auto x = <expression>` is `=<expression>`, and the
        variable of `for (auto& x : <expression>)` is `@<expression>`, for the resolver to work out from declared
        types alone."""
        types: dict[str, str] = {}
        declarator = self._find_function_declarator(function_node)
        parameters = declarator.child_by_field_name("parameters") if declarator is not None else None

        def add_parameters(parameter_list: Optional[Node], keep_existing: bool) -> None:
            for parameter in (parameter_list.children if parameter_list is not None else []):
                if parameter.type != "parameter_declaration":
                    continue
                name_node = parameter.child_by_field_name("declarator")
                name = self._declarator_name(name_node) if name_node is not None else None
                cleaned = self._variable_type(parameter.child_by_field_name("type"), name_node)
                if name and cleaned and (not keep_existing or name not in types):
                    types[name] = cleaned

        add_parameters(parameters, False)

        def declare(name: Optional[str], value: Optional[str], keep_existing: bool) -> None:
            if name and value and (not keep_existing or name not in types):
                types[name] = value

        def walk(node: Node, keep_existing: bool) -> None:
            for child in node.children:
                if child.type == "function_definition":
                    continue
                if child.type == "lambda_expression":
                    lambda_declarator = child.child_by_field_name("declarator")
                    add_parameters(lambda_declarator.child_by_field_name("parameters") if lambda_declarator is not None else None, True)
                    body = child.child_by_field_name("body")
                    if body is not None:
                        walk(body, True)
                    continue
                if child.type == "declaration":
                    type_node = child.child_by_field_name("type")
                    automatic = type_node is not None and type_node.type == "placeholder_type_specifier"
                    for item in child.children:
                        if item is type_node or item.type in (";", ",", "type_qualifier", "storage_class_specifier"):
                            continue
                        name = self._declarator_name(item)
                        if automatic:
                            value = item.child_by_field_name("value") if item.type == "init_declarator" else None
                            expression = self._receiver_expression(value) if value is not None else None
                            declare(name, "=" + expression if expression else None, keep_existing)
                        else:
                            declare(name, self._variable_type(type_node, item), keep_existing)
                elif child.type == "for_range_loop":
                    type_node = child.child_by_field_name("type")
                    loop_declarator = child.child_by_field_name("declarator")
                    name = self._declarator_name(loop_declarator) if loop_declarator is not None else None
                    if type_node is not None and type_node.type == "placeholder_type_specifier":
                        right = child.child_by_field_name("right")
                        expression = self._receiver_expression(right) if right is not None else None
                        declare(name, "@" + expression if expression else None, keep_existing)
                    else:
                        declare(name, self._variable_type(type_node, loop_declarator), keep_existing)
                walk(child, keep_existing)

        body = self._get_child_by_type(function_node, "compound_statement")
        if body is not None:
            walk(body, False)
        return types

    # --- the classes and the return types of a file, for the call resolver ---------------------------------------------

    def _type_facts(self, root: Node) -> dict:
        """`{"classes": [{name, bases, fields, template}], "returns": {qualified function name: return type}}` for the
        file; empty when it has neither. The resolver works from these (and the declared types of each function) to give a
        call's receiver a type: a class's bases and fields, a function's declared return type."""
        classes: list[dict] = []
        aliases: list[dict] = []  # `using X = T;` and `typedef T X;`: X's qualified name and T as written (its template arguments included)
        returns: dict[str, str] = {}
        clashes: set[str] = set()
        reference_params: dict[str, str] = {}  # qualified function name -> a flag per parameter: 1 = a non-const reference
        clashing_params: set[str] = set()

        def note_return(qualified: str, type_node: Optional[Node]) -> None:
            written = normalize_type(type_node.text.decode("utf8")) if type_node is not None else None
            if not written or not qualified:
                return
            if returns.get(qualified, written) != written:
                clashes.add(qualified)  # overloads that return different types: the name says nothing
            returns[qualified] = written

        def function_return(node: Node, namespace: Optional[str], class_name: Optional[str]) -> None:
            declarator = self._find_function_declarator(node)
            if declarator is None:
                return
            name_info = self._get_function_name(declarator)
            if not name_info:
                return
            name, method_class = name_info
            if method_class:
                qualified = self._build_qualified_name(namespace, method_class, name)
            elif class_name:
                qualified = f"{class_name}::{name}"
            else:
                qualified = self._build_qualified_name(namespace, name)
            note_return(qualified, node.child_by_field_name("type"))
            note_reference_parameters(qualified, declarator)

        def note_reference_parameters(qualified: str, declarator: Node) -> None:
            parameters = declarator.child_by_field_name("parameters")
            if parameters is None or not qualified:
                return
            flags = ""
            for parameter in parameters.children:
                if parameter.type not in ("parameter_declaration", "optional_parameter_declaration"):
                    continue
                inner = parameter.child_by_field_name("declarator")
                is_const = any(child.type == "type_qualifier" and child.text == b"const" for child in parameter.children)
                flags += "1" if (inner is not None and inner.type == "reference_declarator" and inner.text.startswith(b"&") and not inner.text.startswith(b"&&") and not is_const) else "0"
            if not flags:
                return
            if reference_params.get(qualified, flags) != flags:
                clashing_params.add(qualified)  # overloads that take it differently: nothing is said
            reference_params[qualified] = flags

        def note_alias(node: Node, namespace: Optional[str], outer: Optional[str]) -> None:
            """A type alias (`using X = Base<Arg>;` or `typedef Base<Arg> X;`) at namespace or class scope: the resolver follows a base or
            a declared type that is such an alias to its target."""
            if node.type == "alias_declaration":
                name_node, type_node = node.child_by_field_name("name"), node.child_by_field_name("type")
            else:
                name_node, type_node = node.child_by_field_name("declarator"), node.child_by_field_name("type")
            if name_node is None or type_node is None or name_node.type != "type_identifier":
                return
            target = normalize_type(type_node.text.decode("utf8"))
            if not target:
                return
            name = name_node.text.decode("utf8")
            qualified = f"{outer}::{name}" if outer else self._build_qualified_name(namespace, name)
            aliases.append({"name": qualified, "target": target})

        def class_facts(node: Node, namespace: Optional[str], outer: Optional[str], template: list[str]) -> None:
            name = self._get_class_name(node)
            body = self._get_child_by_type(node, "field_declaration_list")
            if not name or body is None:
                return
            full = f"{outer}::{name}" if outer else self._build_qualified_name(namespace, name)
            bases: list[str] = []
            clause = self._get_child_by_type(node, "base_class_clause")
            for base in (clause.children if clause is not None else []):
                if base.type in ("type_identifier", "qualified_identifier", "template_type"):
                    written = normalize_type(base.text.decode("utf8"))
                    if written:
                        bases.append(written)
            fields: dict[str, str] = {}
            member_names: list[str] = []  # every data member, of any type (`fields` keeps the ones whose type can be used)

            def members(container: Node) -> None:
                for member in container.children:
                    if member.type in self._PREPROCESSOR_BLOCKS:
                        members(member)
                    elif member.type == "field_declaration":
                        type_node = member.child_by_field_name("type")
                        declared = [child for index, child in enumerate(member.children) if member.field_name_for_child(index) == "declarator"]
                        if self._find_function_declarator(member) is not None:  # a method declaration: its return type
                            function_return(member, namespace, full)
                            continue
                        for declarator in declared:
                            field_name = self._declarator_name(declarator)
                            written = self._variable_type(type_node, declarator)
                            if field_name:
                                member_names.append(field_name)
                            if field_name and written:
                                fields[field_name] = written
                    elif member.type == "function_definition":
                        function_return(member, namespace, full)
                    elif member.type == "template_declaration":
                        for inner in member.children:
                            if inner.type == "function_definition" or (inner.type in ("declaration", "field_declaration") and self._find_function_declarator(inner) is not None):
                                function_return(inner, namespace, full)
                            elif inner.type in ("class_specifier", "struct_specifier"):
                                class_facts(inner, namespace, full, self._template_names(member))
                    elif member.type in ("class_specifier", "struct_specifier"):
                        class_facts(member, namespace, full, [])
                    elif member.type in ("alias_declaration", "type_definition"):
                        note_alias(member, namespace, full)

            members(body)
            fact: dict = {"name": full, "bases": bases, "fields": fields}
            if member_names:
                fact["members"] = member_names
            if template:
                fact["template"] = template
            classes.append(fact)

        names: set[str] = set()  # the variables declared at file or namespace level (`static ID3D12Device* g_device;`)

        def walk(node: Node, namespace: Optional[str]) -> None:
            for child in node.children:
                kind = child.type
                if kind == "declaration" and self._find_function_declarator(child) is None:
                    type_node = child.child_by_field_name("type")
                    for item in child.children:
                        if item is type_node or item.type in (";", ",", "type_qualifier", "storage_class_specifier"):
                            continue
                        variable = self._declarator_name(item)
                        if variable:
                            names.add(variable)
                    continue
                if kind == "namespace_definition":
                    inner = self._get_child_by_type(child, "declaration_list")
                    if inner is not None:
                        ns_name = self._get_namespace_name(child)
                        walk(inner, f"{namespace}::{ns_name}" if namespace else ns_name)
                elif kind in ("class_specifier", "struct_specifier"):
                    class_facts(child, namespace, None, [])
                elif kind in ("alias_declaration", "type_definition"):
                    note_alias(child, namespace, None)
                elif kind == "template_declaration":
                    for inner in child.children:
                        if inner.type in ("class_specifier", "struct_specifier"):
                            class_facts(inner, namespace, None, self._template_names(child))
                        elif inner.type == "function_definition" or (inner.type == "declaration" and self._find_function_declarator(inner) is not None):
                            function_return(inner, namespace, None)
                elif kind == "function_definition" or (kind == "declaration" and self._find_function_declarator(child) is not None):
                    function_return(child, namespace, None)
                else:
                    walk(child, namespace)

        walk(root, None)
        for qualified in clashes:
            returns.pop(qualified, None)
        facts: dict = {}
        if classes:
            facts["classes"] = classes
        if aliases:
            facts["aliases"] = aliases
        for qualified in clashing_params:
            reference_params.pop(qualified, None)
        if reference_params:
            facts["refparams"] = reference_params
        if returns:
            facts["returns"] = returns
        if names:
            facts["globals"] = sorted(names)
        return facts

    @staticmethod
    def _template_names(template: Node) -> list[str]:
        """The names of the parameters of a `template <...>` (type and non-type, in order)."""
        parameters = template.child_by_field_name("parameters")
        names: list[str] = []
        for parameter in (parameters.children if parameters is not None else []):
            if parameter.type in ("type_parameter_declaration", "variadic_type_parameter_declaration"):
                name = next((child.text.decode("utf8") for child in parameter.children if child.type == "type_identifier"), None)
            elif parameter.type in ("parameter_declaration", "optional_parameter_declaration"):
                declarator = parameter.child_by_field_name("declarator")
                name = declarator.text.decode("utf8") if declarator is not None and declarator.type == "identifier" else None
            else:
                continue
            names.append(name or "?")
        return names

    def _extract_imports(self, node: Node, name_imports: list[RawNameImport]) -> None:
        """Walk the whole file for name-binding `using` declarations.
        `using namespace X;` (no specific name bound) and `#include` (no
        symbol-level binding at all — unlike Python/JS, C++ doesn't name
        specific functions at the import site) are both out of scope:
        neither binds one specific name the way `using X::Y;` does.
        """
        for child in node.children:
            if child.type == "using_declaration":
                self._extract_using_declaration(child, name_imports)
            else:
                self._extract_imports(child, name_imports)

    def _extract_using_declaration(self, node: Node, name_imports: list[RawNameImport]) -> None:
        """`using std::vector;` -> binds `vector` to `std::vector`."""
        qualified = self._get_child_by_type(node, "qualified_identifier")
        if qualified is None:
            return
        scope_node = qualified.child_by_field_name("scope")
        name_node = qualified.child_by_field_name("name")
        if scope_node is None or name_node is None:
            return
        name_imports.append(
            RawNameImport(module_path=scope_node.text.decode("utf8"), name=name_node.text.decode("utf8"))
        )


def hello_world():
    print('something is here')
