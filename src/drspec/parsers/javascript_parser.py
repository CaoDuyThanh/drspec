"""JavaScript source code parser using Tree-sitter."""

from __future__ import annotations

import re
from typing import Optional

import tree_sitter_javascript as tsjs
from tree_sitter import Language, Parser, Node

from drspec.parsers.parameters import javascript_parameter_tokens
from drspec.parsers.models import (
    ExtractedFunction,
    ParseError,
    ParseResult,
    RawCall,
    RawModuleImport,
    RawNameImport,
)


_FUNCTION_VALUES = ("arrow_function", "function_expression", "function", "generator_function")
_CLASS_NODES = ("class_declaration", "abstract_class_declaration")
_SUITE_BLOCKS = frozenset({"describe", "context", "suite"})
_CASE_BLOCKS = frozenset({"it", "test", "specify"})
_HOOK_BLOCKS = frozenset({"beforeEach", "afterEach", "beforeAll", "afterAll", "before", "after"})
_TEST_MODIFIERS = frozenset({"only", "skip", "todo", "concurrent", "serial", "fails"})


def _clean_title(text: str) -> str:
    """A test title as part of a function name: `#` and `::` mean something in an identifier."""
    return re.sub(r"\s+", " ", text.replace("::", ":").replace("#", "_")).strip()


class JavaScriptParser:
    """Parser for JavaScript source code using Tree-sitter.

    Extracts function definitions including:
    - Function declarations (`function foo() {}`)
    - Arrow functions (`const foo = () => {}`)
    - Function expressions (`const foo = function() {}`)
    - Class methods
    - Async functions and generators
    - Exported functions
    """

    def __init__(self, dialect: str = "javascript") -> None:
        """Initialize the JavaScript parser with tree-sitter version compatibility."""
        grammar = tsjs.language()
        if dialect in ("typescript", "tsx"):
            import tree_sitter_typescript as tsts
            grammar = tsts.language_tsx() if dialect == "tsx" else tsts.language_typescript()
        try:
            # Some tree-sitter 0.21.x builds use the new Language/set_language API...
            self._language = Language(grammar, dialect)
            self._parser = Parser()
            self._parser.set_language(self._language)
        except TypeError:
            # ...others still expect the old Language/.language-attribute API.
            self._language = Language(grammar)
            self._parser = Parser()
            self._parser.language = self._language

    def parse(self, source_code: str, file_path: Optional[str] = None) -> ParseResult:
        """Parse JavaScript source code and extract functions.

        Args:
            source_code: JavaScript source code as string.
            file_path: Optional file path for context.

        Returns:
            ParseResult containing extracted functions and any errors.
        """
        tree = self._parser.parse(bytes(source_code, "utf8"))
        result = ParseResult(file_path=file_path)
        self._ns: list[str] = []
        self._js: dict = {"bindings": [], "exports": {}, "reexports": [], "classes": [], "namespaces": [], "objects": []}

        if tree.root_node.has_error:
            result.has_errors = True
            self._collect_errors(tree.root_node, result.errors)

        raw_functions: list[ExtractedFunction] = []
        self._extract_functions(
            tree.root_node,
            source_code,
            raw_functions,
            parent=None,
        )

        # The last definition wins when the same name occurs twice with the same parameters;
        # definitions with other parameters are kept as separate functions.
        seen: dict[tuple[str, tuple[str, ...]], ExtractedFunction] = {}
        for func in raw_functions:
            seen[(func.qualified_name, tuple(func.parameter_tokens))] = func

        result.functions = list(seen.values())

        # A separate pass over the whole tree, not scoped to any one function.
        self._extract_imports(tree.root_node, result.name_imports, result.module_imports)
        self._extract_module_facts(tree.root_node)
        result.facts = self._js

        return result

    def parse_file(self, file_path: str) -> ParseResult:
        """Parse a JavaScript file and extract functions.

        Args:
            file_path: Path to the JavaScript file.

        Returns:
            ParseResult containing extracted functions and any errors.

        Raises:
            FileNotFoundError: If the file doesn't exist.
            IOError: If the file can't be read.
        """
        with open(file_path, "r", encoding="utf-8") as f:
            source_code = f.read()
        return self.parse(source_code, file_path=file_path)

    def _extract_functions(
        self,
        node: Node,
        source_code: str,
        functions: list[ExtractedFunction],
        parent: Optional[str] = None,
    ) -> None:
        """Recursively extract functions from an AST node.

        Args:
            node: Current AST node.
            source_code: Original source code.
            functions: List to append extracted functions to.
            parent: Parent class/function name for nested functions.
        """
        for child in node.children:
            if child.type == "function_declaration":
                func = self._extract_function_declaration(child, source_code, parent)
                if func:
                    functions.append(func)
                    body = self._get_child_by_type(child, "statement_block")
                    if body:
                        self._extract_functions(body, source_code, functions, func.qualified_name)

            elif child.type == "generator_function_declaration":
                func = self._extract_function_declaration(child, source_code, parent, is_generator=True)
                if func:
                    functions.append(func)

            elif child.type == "variable_declaration" or child.type == "lexical_declaration":
                self._extract_variable_functions(child, source_code, functions, parent)

            elif child.type in _CLASS_NODES:
                self._extract_class(child, source_code, functions)

            elif child.type == "interface_declaration":
                self._record_class(child, interface=True)

            elif child.type in ("internal_module", "module"):
                self._extract_namespace(child, source_code, functions)

            elif child.type == "export_statement":
                self._extract_exports(child, source_code, functions, parent)

            elif child.type == "call_expression" and self._extract_test_block(child, source_code, functions, parent):
                pass

            elif child.type == "assignment_expression" and self._extract_exported_assignment(child, source_code, functions, parent):
                pass

            else:
                self._extract_functions(child, source_code, functions, parent)

    def _extract_function_declaration(
        self,
        node: Node,
        source_code: str,
        parent: Optional[str],
        is_generator: bool = False,
    ) -> Optional[ExtractedFunction]:
        """Extract function details from a function_declaration node.

        Args:
            node: The function_declaration node.
            source_code: Original source code.
            parent: Parent class/function name.
            is_generator: True if this is a generator function.

        Returns:
            ExtractedFunction with all details, or None if extraction fails.
        """
        name_node = self._get_child_by_type(node, "identifier")
        if not name_node:
            return None
        name = name_node.text.decode("utf8")

        qualified_name = f"{parent}.{name}" if parent else name
        is_async = self._has_async_modifier(node)
        signature = self._get_signature(node, source_code)
        body = self._get_node_text(node, source_code)
        start_line = node.start_point[0] + 1
        end_line = node.end_point[0] + 1

        return ExtractedFunction(
            name=name,
            qualified_name=qualified_name,
            signature=signature,
            body=body,
            start_line=start_line,
            end_line=end_line,
            parent=parent,
            decorators=[],
            is_method=False,
            is_async=is_async,
            calls=self._extract_calls_for_function(node, source_code),
            parameter_tokens=javascript_parameter_tokens(node),
            declared_param_types=self._declared_types(node),
            has_error=node.has_error,
        )

    def _extract_variable_functions(
        self,
        node: Node,
        source_code: str,
        functions: list[ExtractedFunction],
        parent: Optional[str],
    ) -> None:
        """Extract arrow functions and function expressions from variable declarations.

        Args:
            node: The variable_declaration or lexical_declaration node.
            source_code: Original source code.
            functions: List to append extracted functions to.
            parent: Parent class/function name.
        """
        for child in node.children:
            if child.type == "variable_declarator":
                name_node = self._get_child_by_type(child, "identifier")
                if not name_node:
                    continue
                name = name_node.text.decode("utf8")

                object_node = self._get_child_by_type(child, "object")
                if object_node is not None:
                    self._extract_object_members(object_node, f"{parent}.{name}" if parent else name, source_code, functions)
                    continue

                value_node = None
                for subchild in child.children:
                    if subchild.type in ("arrow_function", "function_expression", "generator_function"):
                        value_node = subchild
                        break

                if not value_node:
                    continue

                qualified_name = f"{parent}.{name}" if parent else name
                is_async = self._has_async_modifier(value_node)

                signature = self._get_signature(node, source_code)
                body = self._get_node_text(node, source_code)
                start_line = node.start_point[0] + 1
                end_line = node.end_point[0] + 1

                func = ExtractedFunction(
                    name=name,
                    qualified_name=qualified_name,
                    signature=signature,
                    body=body,
                    start_line=start_line,
                    end_line=end_line,
                    parent=parent,
                    decorators=[],
                    is_method=False,
                    is_async=is_async,
                    calls=self._extract_calls_for_function(value_node, source_code),
                    parameter_tokens=javascript_parameter_tokens(value_node),
                    declared_param_types=self._declared_types(value_node),
                    has_error=value_node.has_error,
                )
                functions.append(func)

                if value_node.type == "arrow_function":
                    body_node = self._get_child_by_type(value_node, "statement_block")
                    if body_node:
                        self._extract_functions(body_node, source_code, functions, qualified_name)

    def _extract_class_methods(
        self,
        class_body: Node,
        source_code: str,
        functions: list[ExtractedFunction],
        class_name: str,
    ) -> None:
        """Extract methods from a class body.

        Args:
            class_body: The class body node.
            source_code: Original source code.
            functions: List to append extracted functions to.
            class_name: Name of the containing class.
        """
        for child in class_body.children:
            if child.type == "method_definition":
                func = self._extract_method(child, source_code, class_name)
                if func:
                    functions.append(func)

            elif child.type in ("field_definition", "public_field_definition"):
                self._extract_field_function(child, source_code, functions, class_name)

    def _extract_method(
        self,
        node: Node,
        source_code: str,
        class_name: str,
    ) -> Optional[ExtractedFunction]:
        """Extract a method from a method_definition node.

        Args:
            node: The method_definition node.
            source_code: Original source code.
            class_name: Name of the containing class.

        Returns:
            ExtractedFunction with all details, or None if extraction fails.
        """
        name_node = self._get_child_by_type(node, "property_identifier")
        if not name_node:
            # Could be a computed property or a private identifier
            name_node = self._get_child_by_type(node, "private_property_identifier")
        if not name_node:
            return None

        name = name_node.text.decode("utf8")
        is_async = self._has_async_modifier(node)

        decorators: list[str] = []
        is_getter = False
        is_setter = False
        for child in node.children:
            if child.type == "get":
                decorators.append("getter")
                is_getter = True
            elif child.type == "set":
                decorators.append("setter")
                is_setter = True
            elif child.type == "static":
                decorators.append("static")

        # get_/set_ prefix keeps a getter and setter of the same name
        # unique, since JS allows both `get x()` and `set x()` on one property.
        if is_getter:
            qualified_name = f"{class_name}.get_{name}"
        elif is_setter:
            qualified_name = f"{class_name}.set_{name}"
        else:
            qualified_name = f"{class_name}.{name}"

        signature = self._get_signature(node, source_code)
        body = self._get_node_text(node, source_code)
        start_line = node.start_point[0] + 1
        end_line = node.end_point[0] + 1

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
            is_async=is_async,
            calls=self._extract_calls_for_function(node, source_code),
            parameter_tokens=javascript_parameter_tokens(node),
            declared_param_types=self._declared_types(node),
            has_error=node.has_error,
        )

    def _extract_field_function(
        self,
        node: Node,
        source_code: str,
        functions: list[ExtractedFunction],
        class_name: str,
    ) -> None:
        """Extract arrow function from a class field definition.

        Args:
            node: The field_definition node.
            source_code: Original source code.
            functions: List to append extracted functions to.
            class_name: Name of the containing class.
        """
        name_node = self._get_child_by_type(node, "property_identifier")
        if not name_node:
            return

        arrow_node = self._get_child_by_type(node, "arrow_function")
        if not arrow_node:
            return

        name = name_node.text.decode("utf8")
        qualified_name = f"{class_name}.{name}"
        is_async = self._has_async_modifier(arrow_node)

        signature = self._get_signature(node, source_code)
        body = self._get_node_text(node, source_code)
        start_line = node.start_point[0] + 1
        end_line = node.end_point[0] + 1

        func = ExtractedFunction(
            name=name,
            qualified_name=qualified_name,
            signature=signature,
            body=body,
            start_line=start_line,
            end_line=end_line,
            parent=class_name,
            decorators=[],
            is_method=True,
            is_async=is_async,
            calls=self._extract_calls_for_function(arrow_node, source_code),
            parameter_tokens=javascript_parameter_tokens(arrow_node),
            declared_param_types=self._declared_types(arrow_node),
            has_error=arrow_node.has_error,
        )
        functions.append(func)

    def _extract_exports(
        self,
        node: Node,
        source_code: str,
        functions: list[ExtractedFunction],
        parent: Optional[str],
    ) -> None:
        """Extract functions from export statements.

        Args:
            node: The export_statement node.
            source_code: Original source code.
            functions: List to append extracted functions to.
            parent: Parent class/function name.
        """
        for child in node.children:
            if child.type == "function_declaration":
                func = self._extract_function_declaration(child, source_code, parent)
                if func:
                    func.decorators.append("export")
                    functions.append(func)

            elif child.type in _CLASS_NODES:
                self._extract_class(child, source_code, functions)

            elif child.type == "interface_declaration":
                self._record_class(child, interface=True)

            elif child.type in ("internal_module", "module"):
                self._extract_namespace(child, source_code, functions)

            elif child.type in ("variable_declaration", "lexical_declaration"):
                self._extract_variable_functions(child, source_code, functions, parent)

            elif child.type == "object" and any(c.type == "default" for c in node.children):
                self._extract_object_members(child, "default", source_code, functions)

            elif child.type == "default":
                continue

            else:
                self._extract_exports(child, source_code, functions, parent)

    def _get_class_name(self, node: Node) -> str:
        """Get the class name from a class_declaration node."""
        name_node = node.child_by_field_name("name")
        return name_node.text.decode("utf8") if name_node else "<anonymous>"

    def _has_async_modifier(self, node: Node) -> bool:
        """Check if a node has the async modifier."""
        for child in node.children:
            if child.type == "async":
                return True
        return False

    def _get_signature(self, node: Node, source_code: str) -> str:
        """Extract the function signature (first line).

        Args:
            node: The function node.
            source_code: Original source code.

        Returns:
            The signature line.
        """
        lines = source_code.split("\n")
        start_line = node.start_point[0]
        if start_line < len(lines):
            return lines[start_line].strip()
        return ""

    def _get_child_by_type(self, node: Node, type_name: str) -> Optional[Node]:
        """Find a child node by type."""
        for child in node.children:
            if child.type == type_name:
                return child
        return None

    def _get_node_text(self, node: Node, source_code: str) -> str:
        """Get the full text of a node."""
        return node.text.decode("utf-8")

    def _collect_errors(self, node: Node, errors: list[ParseError]) -> None:
        """Recursively collect syntax errors from the AST.

        Args:
            node: Current AST node.
            errors: List to append errors to.
        """
        if node.type == "ERROR" or node.is_missing:
            errors.append(
                ParseError(
                    line=node.start_point[0] + 1,
                    column=node.start_point[1],
                    message=f"Syntax error: {node.type}" if node.type == "ERROR" else "Missing token",
                )
            )

        for child in node.children:
            self._collect_errors(child, errors)

    # Node types that introduce a *new* function scope — a call found
    # inside one of these belongs to that nested function, not the one
    # currently being extracted.
    _NESTED_FUNCTION_TYPES = (
        "function_declaration",
        "generator_function_declaration",
        "function_expression",
        "generator_function",
        "arrow_function",
        "method_definition",
    )

    def _extract_calls_for_function(self, func_node: Node, source_code: str) -> list[RawCall]:
        """Collect every call expression in `func_node`, not descending
        into a nested function's own body."""
        calls: list[RawCall] = []
        for child in func_node.children:
            self._collect_calls(child, source_code, calls)
        return calls

    def _collect_calls(self, node: Node, source_code: str, calls: list[RawCall]) -> None:
        call: Optional[RawCall] = None
        if node.type == "call_expression":
            call = self._extract_call(node, source_code)
        elif node.type == "new_expression":
            call = self._extract_new(node, source_code)
        elif node.type in ("jsx_self_closing_element", "jsx_opening_element"):
            call = self._extract_jsx(node)
        if call is not None:
            calls.append(call)
        if node.type in self._NESTED_FUNCTION_TYPES:
            return
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

        if function_part.type == "super":  # `super(...)` in a constructor
            return RawCall(receiver="super", name="constructor", text=text, kind="super")

        if function_part.type != "member_expression":
            return None  # e.g. an IIFE or other complex callee

        object_node = function_part.child_by_field_name("object")
        property_node = function_part.child_by_field_name("property")
        if object_node is None or property_node is None:
            return None
        name = property_node.text.decode("utf8")

        if object_node.type in ("this", "super"):
            # `this.method()` / `super.method()` — JS gives `super` its
            # own keyword node type directly (unlike Python's `super()`
            # call), so no nested-call unwrapping is needed here.
            receiver = object_node.type
        elif object_node.type == "identifier":
            receiver = object_node.text.decode("utf8")
        else:
            constructed = self._constructed_class(object_node)
            if constructed is None:
                return None  # receiver is some other expression — too complex, skip
            receiver = "new " + constructed  # `new Repo().find(id)`: the method of the class just constructed

        return RawCall(receiver=receiver, name=name, text=text)

    def _constructed_class(self, node: Node) -> Optional[str]:
        """The class name in `new X(...)`, also when the expression is parenthesised: `(new X()).m()`."""
        while node.type == "parenthesized_expression" and node.named_child_count == 1:
            node = node.named_children[0]
        if node.type != "new_expression":
            return None
        constructor = node.child_by_field_name("constructor")
        if constructor is None or constructor.type not in ("identifier", "member_expression", "nested_identifier"):
            return None
        return constructor.text.decode("utf8")

    def _extract_imports(
        self,
        node: Node,
        name_imports: list[RawNameImport],
        module_imports: list[RawModuleImport],
    ) -> None:
        """Walk the whole file for `import` statements."""
        for child in node.children:
            if child.type == "import_statement":
                self._extract_import_statement(child, name_imports, module_imports)
            else:
                self._extract_imports(child, name_imports, module_imports)

    def _extract_import_statement(
        self,
        node: Node,
        name_imports: list[RawNameImport],
        module_imports: list[RawModuleImport],
    ) -> None:
        """`import { a, b as c } from "X"` / `import * as a from "X"` /
        `import defaultThing from "X"` (skipped — see below)."""
        source_node = self._get_child_by_type(node, "string")
        if source_node is None:
            return
        module_path = self._string_text(source_node)

        clause = self._get_child_by_type(node, "import_clause")
        if clause is None:
            return

        for child in clause.children:
            if child.type == "named_imports":
                for spec in child.children:
                    if spec.type != "import_specifier":
                        continue
                    names = [c for c in spec.children if c.type == "identifier"]
                    if not names:
                        continue
                    name = names[0].text.decode("utf8")
                    alias = names[1].text.decode("utf8") if len(names) > 1 else None
                    name_imports.append(RawNameImport(module_path=module_path, name=name, alias=alias))
            elif child.type == "namespace_import":
                alias_node = self._get_child_by_type(child, "identifier")
                if alias_node is not None:
                    module_imports.append(
                        RawModuleImport(module_path=module_path, alias=alias_node.text.decode("utf8"))
                    )
            elif child.type == "identifier":
                # Default import (`import defaultThing from "X"`) — which
                # specific export this binds to isn't knowable without
                # tracking which export in the target file is the default
                # one, which this project's model doesn't track. Skipped
                # rather than guessing `defaultThing` matches a same-named
                # function in the target file.
                continue

    # ---- additions: classes, namespaces, object literals, test blocks, module facts

    def _qualify(self, name: str) -> str:
        return ".".join(self._ns + [name])

    @staticmethod
    def _type_name(node: Optional[Node]) -> Optional[str]:
        """The class or interface a type annotation names, or `None` for anything else (unions, arrays, literals)."""
        if node is None:
            return None
        if node.type == "type_annotation":
            named = [c for c in node.children if c.is_named]
            return JavaScriptParser._type_name(named[0]) if named else None
        if node.type in ("type_identifier", "identifier", "nested_type_identifier", "member_expression"):
            return node.text.decode("utf8")
        if node.type == "generic_type":
            return JavaScriptParser._type_name(node.child_by_field_name("name") or (node.children[0] if node.children else None))
        return None

    def _declared_types(self, func_node: Node) -> dict[str, str]:
        """Variable -> class name, for typed parameters (`s: Shape`), typed locals (`const c: Circle = ...`) and
        locals created with `new X(...)`; nothing else is guessed."""
        types: dict[str, str] = {}
        params = func_node.child_by_field_name("parameters")
        for param in (params.children if params is not None else []):
            if param.type not in ("required_parameter", "optional_parameter"):
                continue
            name_node = param.child_by_field_name("pattern") or next((c for c in param.children if c.type == "identifier"), None)
            annotation = next((c for c in param.children if c.type == "type_annotation"), None)
            type_name = self._type_name(annotation)
            if name_node is not None and name_node.type == "identifier" and type_name:
                types[name_node.text.decode("utf8")] = type_name
        body = func_node.child_by_field_name("body")
        if body is not None:
            self._local_types(body, types)
        return types

    def _local_types(self, node: Node, types: dict[str, str]) -> None:
        for child in node.children:
            if child.type in self._NESTED_FUNCTION_TYPES:
                continue
            if child.type == "variable_declarator":
                name_node = child.child_by_field_name("name")
                if name_node is not None and name_node.type == "identifier":
                    type_name = self._type_name(next((c for c in child.children if c.type == "type_annotation"), None))
                    value = child.child_by_field_name("value")
                    if type_name is None and value is not None and value.type == "new_expression":
                        type_name = self._type_name(value.child_by_field_name("constructor"))
                    if type_name:
                        types[name_node.text.decode("utf8")] = type_name
            self._local_types(child, types)

    def _heritage(self, node: Node) -> tuple[Optional[str], list[str]]:
        extends: Optional[str] = None
        implements: list[str] = []
        for child in node.children:
            if child.type == "class_heritage":
                for part in child.children:
                    if part.type == "extends_clause":
                        for item in part.children:
                            name = self._type_name(item)
                            if name:
                                extends = name
                                break
                    elif part.type == "implements_clause":
                        implements.extend(n for n in (self._type_name(i) for i in part.children) if n)
                    else:
                        name = self._type_name(part)
                        if name:
                            extends = name
            elif child.type == "extends_type_clause":
                implements.extend(n for n in (self._type_name(i) for i in child.children) if n)
        return extends, implements

    def _record_class(self, node: Node, interface: bool, name: Optional[str] = None) -> None:
        name = name or self._qualify(self._get_class_name(node))
        extends, implements = self._heritage(node)
        self._js["classes"].append({
            "name": name, "extends": extends, "implements": implements,
            "interface": interface, "abstract": node.type == "abstract_class_declaration",
        })

    def _extract_class(self, node: Node, source_code: str, functions: list[ExtractedFunction]) -> None:
        name = self._qualify(self._get_class_name(node))
        self._record_class(node, interface=False, name=name)
        body = self._get_child_by_type(node, "class_body")
        if body:
            self._extract_class_methods(body, source_code, functions, name)

    def _extract_namespace(self, node: Node, source_code: str, functions: list[ExtractedFunction]) -> None:
        name_node = node.child_by_field_name("name")
        body = self._get_child_by_type(node, "statement_block")
        if name_node is None or body is None or name_node.type not in ("identifier", "nested_identifier"):
            return
        full = self._qualify(name_node.text.decode("utf8"))
        self._js["namespaces"].append(full)
        saved = self._ns
        self._ns = full.split(".")
        try:
            self._extract_functions(body, source_code, functions, full)
        finally:
            self._ns = saved

    def _build_function(
        self, name: str, qualified: str, value_node: Node, whole: Node, source_code: str, parent: Optional[str], is_method: bool = False,
        decorators: Optional[list[str]] = None,
    ) -> ExtractedFunction:
        return ExtractedFunction(
            name=name,
            qualified_name=qualified,
            signature=self._get_signature(whole, source_code),
            body=self._get_node_text(whole, source_code),
            start_line=whole.start_point[0] + 1,
            end_line=whole.end_point[0] + 1,
            parent=parent,
            decorators=list(decorators or []),
            is_method=is_method,
            is_async=self._has_async_modifier(value_node),
            calls=self._extract_calls_for_function(value_node, source_code),
            parameter_tokens=javascript_parameter_tokens(value_node),
            declared_param_types=self._declared_types(value_node),
            has_error=value_node.has_error,
        )

    def _extract_object_members(self, object_node: Node, owner: str, source_code: str, functions: list[ExtractedFunction]) -> None:
        """Methods and function-valued properties of an object literal are functions named `owner.member`."""
        if owner not in self._js["objects"]:
            self._js["objects"].append(owner)
        for member in object_node.children:
            if member.type == "method_definition":
                func = self._extract_method(member, source_code, owner)
                if func:
                    functions.append(func)
            elif member.type == "pair":
                key = member.child_by_field_name("key")
                value = member.child_by_field_name("value")
                if key is None or value is None or value.type not in _FUNCTION_VALUES or key.type not in ("property_identifier", "identifier"):
                    continue
                name = key.text.decode("utf8")
                functions.append(self._build_function(name, f"{owner}.{name}", value, member, source_code, owner, is_method=True))

    def _literal_text(self, node: Node) -> Optional[str]:
        if node.type == "string":
            fragment = self._get_child_by_type(node, "string_fragment")
            return fragment.text.decode("utf8") if fragment is not None else ""
        if node.type == "template_string" and not any(c.type == "template_substitution" for c in node.children):
            return node.text.decode("utf8").strip("`")
        return None

    def _extract_test_block(self, node: Node, source_code: str, functions: list[ExtractedFunction], parent: Optional[str]) -> bool:
        """`describe("suite", () => {...})`, `it("case", () => {...})` and hooks are functions named `suite > case`."""
        callee = node.child_by_field_name("function")
        arguments = node.child_by_field_name("arguments")
        if callee is None or arguments is None:
            return False
        if callee.type == "member_expression":  # describe.only, it.skip
            obj, prop = callee.child_by_field_name("object"), callee.child_by_field_name("property")
            if obj is None or prop is None or prop.text.decode("utf8") not in _TEST_MODIFIERS:
                return False
            callee = obj
        if callee.type != "identifier":
            return False
        kind = callee.text.decode("utf8")
        if kind not in _SUITE_BLOCKS | _CASE_BLOCKS | _HOOK_BLOCKS:
            return False
        named = [c for c in arguments.children if c.is_named]
        if not named or named[-1].type not in _FUNCTION_VALUES:
            return False
        callback = named[-1]
        if kind in _HOOK_BLOCKS:
            title = kind
        else:
            literal = self._literal_text(named[0]) if len(named) > 1 else None
            if literal is None:
                return False
            title = _clean_title(literal)
        if not title:
            return False
        qualified = f"{parent} > {title}" if parent else title
        func = self._build_function(title, qualified, callback, node, source_code, parent)
        func.decorators.append("test")
        functions.append(func)
        body = self._get_child_by_type(callback, "statement_block")
        if body:
            self._extract_functions(body, source_code, functions, qualified)
        return True

    def _exports_target(self, left: Node) -> Optional[str]:
        """`exports.x` / `module.exports.x` -> `x`."""
        if left.type != "member_expression":
            return None
        obj, prop = left.child_by_field_name("object"), left.child_by_field_name("property")
        if obj is None or prop is None:
            return None
        if obj.type == "identifier" and obj.text == b"exports":
            return prop.text.decode("utf8")
        if obj.type == "member_expression" and obj.text == b"module.exports":
            return prop.text.decode("utf8")
        return None

    def _extract_exported_assignment(self, node: Node, source_code: str, functions: list[ExtractedFunction], parent: Optional[str]) -> bool:
        """`exports.f = function () {...}` / `module.exports.f = () => {...}` define the exported function `f`."""
        left, right = node.child_by_field_name("left"), node.child_by_field_name("right")
        if left is None or right is None or right.type not in _FUNCTION_VALUES or parent:
            return False
        name = self._exports_target(left)
        if name is None:
            return False
        functions.append(self._build_function(name, name, right, node, source_code, None))
        self._js["exports"][name] = name
        return True

    def _extract_module_facts(self, root: Node) -> None:
        bindings = self._js["bindings"]
        for child in root.children:
            if child.type == "import_statement":
                self._import_bindings(child, bindings)
            elif child.type == "export_statement":
                self._export_facts(child)
            elif child.type == "expression_statement":
                for part in child.children:
                    if part.type == "assignment_expression":
                        self._commonjs_exports(part)
        self._find_requires(root, bindings)

    def _import_bindings(self, node: Node, bindings: list[dict]) -> None:
        source = self._get_child_by_type(node, "string")
        clause = self._get_child_by_type(node, "import_clause")
        if source is None or clause is None:
            return
        module = self._string_text(source)
        for child in clause.children:
            if child.type == "identifier":
                bindings.append({"local": child.text.decode("utf8"), "module": module, "name": "default", "kind": "import"})
            elif child.type == "namespace_import":
                alias = self._get_child_by_type(child, "identifier")
                if alias is not None:
                    bindings.append({"local": alias.text.decode("utf8"), "module": module, "name": "*", "kind": "import"})
            elif child.type == "named_imports":
                for spec in child.children:
                    if spec.type != "import_specifier":
                        continue
                    names = [c.text.decode("utf8") for c in spec.children if c.is_named and c.type in ("identifier", "string")]
                    if names:
                        bindings.append({"local": names[-1], "module": module, "name": names[0], "kind": "import"})

    def _export_facts(self, node: Node) -> None:
        exports, reexports = self._js["exports"], self._js["reexports"]
        source = self._get_child_by_type(node, "string")
        module = self._string_text(source) if source is not None else None
        is_default = any(c.type == "default" for c in node.children)
        for child in node.children:
            if child.type == "*" and module is not None:
                reexports.append({"module": module, "name": "*", "alias": None})
            elif child.type == "namespace_export" and module is not None:
                alias = self._get_child_by_type(child, "identifier")
                if alias is not None:
                    reexports.append({"module": module, "name": "*", "alias": alias.text.decode("utf8")})
            elif child.type == "export_clause":
                for spec in child.children:
                    if spec.type != "export_specifier":
                        continue
                    names = [c.text.decode("utf8") for c in spec.children if c.is_named]
                    if not names:
                        continue
                    exported = names[-1]
                    if module is not None:
                        reexports.append({"module": module, "name": names[0], "alias": exported if exported != names[0] else None})
                    else:
                        exports[exported] = names[0]
            elif is_default and child.type == "identifier":
                exports["default"] = child.text.decode("utf8")
            elif is_default and child.type == "object":
                exports["default"] = "default"
            elif child.type in ("function_declaration", "generator_function_declaration", "class_declaration", "abstract_class_declaration",
                                "interface_declaration", "internal_module", "enum_declaration"):
                name_node = child.child_by_field_name("name")
                if name_node is not None:
                    name = name_node.text.decode("utf8")
                    qualified = self._qualify(name) if child.type != "function_declaration" else name
                    exports[name] = name if child.type in ("function_declaration", "generator_function_declaration") else qualified
                    if is_default:
                        exports["default"] = exports[name]
            elif child.type in ("lexical_declaration", "variable_declaration"):
                for declarator in child.children:
                    if declarator.type == "variable_declarator":
                        name_node = declarator.child_by_field_name("name")
                        if name_node is not None and name_node.type == "identifier":
                            name = name_node.text.decode("utf8")
                            exports[name] = name

    def _commonjs_exports(self, assignment: Node) -> None:
        left, right = assignment.child_by_field_name("left"), assignment.child_by_field_name("right")
        if left is None or right is None:
            return
        exports = self._js["exports"]
        if left.type == "member_expression" and left.text == b"module.exports":
            if right.type == "identifier":
                exports["default"] = right.text.decode("utf8")
            elif right.type == "object":
                for member in right.children:
                    if member.type == "shorthand_property_identifier":
                        name = member.text.decode("utf8")
                        exports[name] = name
                    elif member.type == "pair":
                        key, value = member.child_by_field_name("key"), member.child_by_field_name("value")
                        if key is not None and value is not None and value.type == "identifier":
                            exports[key.text.decode("utf8")] = value.text.decode("utf8")
            return
        target = self._exports_target(left)
        if target is not None and right.type == "identifier":
            exports[target] = right.text.decode("utf8")

    def _find_requires(self, node: Node, bindings: list[dict]) -> None:
        """`const x = require("m")`, `const { a, b: c } = require("m")`, `const x = require("m").a`, and
        `const x = await import("m")` bind names to a module."""
        for child in node.children:
            if child.type == "variable_declarator":
                self._require_binding(child, bindings)
            self._find_requires(child, bindings)

    def _require_binding(self, declarator: Node, bindings: list[dict]) -> None:
        pattern, value = declarator.child_by_field_name("name"), declarator.child_by_field_name("value")
        if pattern is None or value is None:
            return
        kind = "require"
        if value.type == "await_expression":
            inner = next((c for c in value.children if c.is_named), None)
            if inner is None:
                return
            value, kind = inner, "dynamic"
        member: Optional[str] = None
        if value.type == "member_expression":  # require("m").name
            prop = value.child_by_field_name("property")
            value, member = value.child_by_field_name("object"), (prop.text.decode("utf8") if prop is not None else None)
            if value is None:
                return
        if value.type != "call_expression":
            return
        callee, arguments = value.child_by_field_name("function"), value.child_by_field_name("arguments")
        if callee is None or arguments is None:
            return
        if not ((callee.type == "identifier" and callee.text == b"require") or (callee.type == "import" and kind == "dynamic")):
            return
        literal = next((c for c in arguments.children if c.type == "string"), None)
        if literal is None:
            return
        module = self._string_text(literal)
        if pattern.type == "identifier":
            bindings.append({"local": pattern.text.decode("utf8"), "module": module, "name": member or "*", "kind": kind})
        elif pattern.type == "object_pattern" and member is None:
            for item in pattern.children:
                if item.type == "shorthand_property_identifier_pattern":
                    name = item.text.decode("utf8")
                    bindings.append({"local": name, "module": module, "name": name, "kind": kind})
                elif item.type == "pair_pattern":
                    key, local = item.child_by_field_name("key"), item.child_by_field_name("value")
                    if key is not None and local is not None and local.type == "identifier":
                        bindings.append({"local": local.text.decode("utf8"), "module": module, "name": key.text.decode("utf8"), "kind": kind})

    def _extract_new(self, node: Node, source_code: str) -> Optional[RawCall]:
        constructor = node.child_by_field_name("constructor")
        if constructor is None:
            return None
        text = self._get_node_text(node, source_code)
        if constructor.type == "identifier":
            return RawCall(receiver=None, name=constructor.text.decode("utf8"), text=text, kind="new")
        if constructor.type == "member_expression":
            obj, prop = constructor.child_by_field_name("object"), constructor.child_by_field_name("property")
            if obj is not None and prop is not None and obj.type == "identifier":
                return RawCall(receiver=obj.text.decode("utf8"), name=prop.text.decode("utf8"), text=text, kind="new")
        return None

    def _extract_jsx(self, node: Node) -> Optional[RawCall]:
        """`<Button />` is a use of the component `Button`; lower-case tags are DOM elements."""
        tag = node.child_by_field_name("name") or next((c for c in node.children if c.type in ("identifier", "member_expression")), None)
        if tag is None:
            return None
        if tag.type == "identifier":
            name = tag.text.decode("utf8")
            if name[:1].isupper():
                return RawCall(receiver=None, name=name, text=f"<{name}>", kind="jsx")
        elif tag.type == "member_expression":
            obj, prop = tag.child_by_field_name("object"), tag.child_by_field_name("property")
            if obj is not None and prop is not None and obj.type == "identifier":
                return RawCall(receiver=obj.text.decode("utf8"), name=prop.text.decode("utf8"), text=f"<{tag.text.decode('utf8')}>", kind="jsx")
        return None

    def _string_text(self, string_node: Node) -> str:
        """Unquoted text of a `string` node (e.g. `"./utils"` -> `./utils`)."""
        fragment = self._get_child_by_type(string_node, "string_fragment")
        return fragment.text.decode("utf8") if fragment is not None else ""
