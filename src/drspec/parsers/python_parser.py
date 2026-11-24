"""Python source code parser using Tree-sitter."""

from __future__ import annotations

from typing import Optional

import tree_sitter_python as tspython
from tree_sitter import Language, Parser, Node

from drspec.parsers.parameters import python_parameter_tokens
from drspec.parsers.models import (
    ExtractedConstant,
    ExtractedFunction,
    ParseError,
    ParseResult,
    RawCall,
    RawModuleImport,
    RawNameImport,
    RawPropertyRead,
    RawSymbolRead,
)


class PythonParser:
    """Parser for Python source code using Tree-sitter.

    Extracts function definitions including:
    - Top-level functions
    - Class methods
    - Nested functions
    - Async functions
    - Decorated functions
    """

    def __init__(self) -> None:
        """Initialize the Python parser with tree-sitter version compatibility."""
        try:
            # Some tree-sitter 0.21.x builds use the new Language/set_language API...
            self._language = Language(tspython.language(), "python")
            self._parser = Parser()
            self._parser.set_language(self._language)
        except TypeError:
            # ...others still expect the old Language/.language-attribute API.
            self._language = Language(tspython.language())
            self._parser = Parser()
            self._parser.language = self._language

    def parse(self, source_code: str, file_path: Optional[str] = None) -> ParseResult:
        """Parse Python source code and extract functions.

        Args:
            source_code: Python source code as string.
            file_path: Optional file path for context.

        Returns:
            ParseResult containing extracted functions and any errors.
        """
        tree = self._parser.parse(bytes(source_code, "utf8"))
        result = ParseResult(file_path=file_path)

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

        # Every definition is returned, in source order. Two with the same name and parameters (the branches of an `if` or
        # a `try`, or a later definition of the same name) are told apart by the scanner, which gives the last one the
        # identifier the single definition had and the others their own.
        result.functions = raw_functions

        # A separate pass over the whole tree, not scoped to any one function.
        self._extract_imports(tree.root_node, result.name_imports, result.module_imports)

        # Unlike imports, scoped to true top-level statements only (not
        # inside if/try/class bodies).
        result.constants = self._extract_module_constants(tree.root_node, source_code)

        return result

    def parse_file(self, file_path: str) -> ParseResult:
        """Parse a Python file and extract functions.

        Args:
            file_path: Path to the Python file.

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
            parent: Parent function/class name for nested functions.
        """
        for child in node.children:
            if child.type == "function_definition":
                func = self._extract_function(child, source_code, parent, is_method=False)
                functions.append(func)
                self._extract_functions(
                    child,
                    source_code,
                    functions,
                    parent=func.qualified_name,
                )

            elif child.type == "class_definition":
                class_name = self._get_class_name(child)
                class_body = self._get_child_by_type(child, "block")
                if class_body:
                    self._extract_class_methods(
                        class_body,
                        source_code,
                        functions,
                        class_name=class_name,
                    )

            else:
                self._extract_functions(child, source_code, functions, parent)

    #: Statements whose blocks can hold method definitions in a class body, and the blocks they are made of.
    _CLASS_BODY_STATEMENTS = frozenset({
        "if_statement", "elif_clause", "else_clause", "try_statement", "except_clause", "except_group_clause", "finally_clause",
        "with_statement", "for_statement", "while_statement", "block",
    })

    def _extract_class_methods(
        self,
        class_body: Node,
        source_code: str,
        functions: list[ExtractedFunction],
        class_name: str,
    ) -> None:
        """Extract methods from a class body.

        Args:
            class_body: The class body block node.
            source_code: Original source code.
            functions: List to append extracted functions to.
            class_name: Name of the containing class.
        """
        for child in class_body.children:
            if child.type == "function_definition":
                func = self._extract_function(
                    child, source_code, parent=class_name, is_method=True
                )
                functions.append(func)
                self._extract_functions(
                    child,
                    source_code,
                    functions,
                    parent=func.qualified_name,
                )

            elif child.type == "decorated_definition":
                func_node = self._get_child_by_type(child, "function_definition")
                if func_node:
                    func = self._extract_function(
                        func_node, source_code, parent=class_name, is_method=True
                    )
                    func.decorators = self._extract_decorators(child)
                    functions.append(func)
                    # (functions defined inside a decorated method are found like those inside a plain one)
                    self._extract_functions(
                        func_node,
                        source_code,
                        functions,
                        parent=func.qualified_name,
                    )

            elif child.type in self._CLASS_BODY_STATEMENTS:
                # methods defined under an `if`, `try`, `with` or loop in the class body belong to the class too
                self._extract_class_methods(child, source_code, functions, class_name=class_name)

            elif child.type == "class_definition":
                nested_class_name = self._get_class_name(child)
                nested_full_name = f"{class_name}.{nested_class_name}"
                nested_body = self._get_child_by_type(child, "block")
                if nested_body:
                    self._extract_class_methods(
                        nested_body,
                        source_code,
                        functions,
                        class_name=nested_full_name,
                    )

    def _extract_function(
        self,
        node: Node,
        source_code: str,
        parent: Optional[str],
        is_method: bool,
    ) -> ExtractedFunction:
        """Extract function details from a function_definition node.

        Args:
            node: The function_definition node.
            source_code: Original source code.
            parent: Parent class/function name.
            is_method: True if this is a class method.

        Returns:
            ExtractedFunction with all details.
        """
        name_node = self._get_child_by_type(node, "identifier")
        name = name_node.text.decode("utf8") if name_node else "<unknown>"

        qualified_name = f"{parent}.{name}" if parent else name

        is_async = any(child.type == "async" for child in node.children)

        signature = self._get_signature(node, source_code)

        source_node = node.parent if node.parent and node.parent.type == "decorated_definition" else node
        body = self._get_node_text(source_node, source_code)

        # start_line covers decorators too, even though signature still starts at def.
        start_line = source_node.start_point[0] + 1
        end_line = node.end_point[0] + 1

        decorators: list[str] = []
        if node.parent and node.parent.type == "decorated_definition":
            decorators = self._extract_decorators(node.parent)

        calls = self._extract_calls_for_function(node, source_code)
        property_reads = self._extract_property_reads_for_function(node, source_code)
        symbol_reads = self._extract_symbol_reads_for_function(node, source_code)
        first_param_name, declared_param_types = self._extract_param_facts(node)

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
            is_async=is_async,
            is_property="property" in decorators,
            calls=calls,
            property_reads=property_reads,
            symbol_reads=symbol_reads,
            first_param_name=first_param_name,
            declared_param_types=declared_param_types,
            parameter_tokens=python_parameter_tokens(node),
            has_error=node.has_error,
        )

    def _get_signature(self, node: Node, source_code: str) -> str:
        """Extract the function signature (def line).

        Args:
            node: The function_definition node.
            source_code: Original source code.

        Returns:
            The signature line including def/async def and parameters.
        """
        lines = source_code.split("\n")
        start_line = node.start_point[0]
        signature_line = lines[start_line] if start_line < len(lines) else ""

        body_node = self._get_child_by_type(node, "block")
        if body_node and body_node.start_point[0] > start_line:
            end_sig_line = body_node.start_point[0] - 1
            signature_lines = lines[start_line : end_sig_line + 1]
            signature_line = " ".join(line.strip() for line in signature_lines)

        return signature_line.strip()

    def _get_class_name(self, node: Node) -> str:
        """Get the class name from a class_definition node."""
        name_node = self._get_child_by_type(node, "identifier")
        return name_node.text.decode("utf8") if name_node else "<unknown>"

    def _get_child_by_type(self, node: Node, type_name: str) -> Optional[Node]:
        """Find a child node by type."""
        for child in node.children:
            if child.type == type_name:
                return child
        return None

    def _get_node_text(self, node: Node, source_code: str) -> str:
        """Get the full text of a node."""
        return node.text.decode("utf-8")

    def _extract_decorators(self, decorated_node: Node) -> list[str]:
        """Extract decorator strings from a decorated_definition node."""
        decorators = []
        for child in decorated_node.children:
            if child.type == "decorator":
                dec_text = child.text.decode("utf8").strip()
                if dec_text.startswith("@"):
                    dec_text = dec_text[1:]
                decorators.append(dec_text)
        return decorators

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

    def _extract_calls_for_function(self, func_node: Node, source_code: str) -> list[RawCall]:
        """Collect every call expression in `func_node`, not descending into
        a nested function's own body — that function's calls are collected
        separately, when it's visited as its own `ExtractedFunction`.
        """
        calls: list[RawCall] = []
        for child in func_node.children:
            self._collect_calls(child, source_code, calls)
        return calls

    def _collect_calls(self, node: Node, source_code: str, calls: list[RawCall]) -> None:
        if node.type == "call":
            call = self._extract_call(node, source_code)
            if call is not None:
                calls.append(call)
        if node.type == "function_definition":
            return
        for child in node.children:
            self._collect_calls(child, source_code, calls)

    def _extract_call(self, node: Node, source_code: str) -> Optional[RawCall]:
        """Extract one `call` node into a `RawCall`, or `None` if its
        receiver is too complex to represent (never guessed as a bare
        call instead — see `RawCall`'s docstring).
        """
        function_part = node.child_by_field_name("function")
        if function_part is None:
            return None

        text = self._get_node_text(node, source_code)

        if function_part.type == "identifier":
            return RawCall(receiver=None, name=function_part.text.decode("utf8"), text=text)

        if function_part.type != "attribute":
            return None  # e.g. a subscript or parenthesized expression being called

        attribute_node = function_part.child_by_field_name("attribute")
        object_node = function_part.child_by_field_name("object")
        if attribute_node is None or object_node is None:
            return None
        name = attribute_node.text.decode("utf8")

        if object_node.type == "identifier":
            receiver = object_node.text.decode("utf8")
        elif object_node.type == "call" and self._is_bare_super_call(object_node):
            # `super().method(...)` — the receiver is the *result* of
            # calling `super()`, not a simple name. Tag it with the same
            # `"super"` sentinel JS/C++ use for their keyword form, rather
            # than resolving which ancestor class it refers to.
            receiver = "super"
        else:
            return None  # receiver is some other expression — too complex, skip

        return RawCall(receiver=receiver, name=name, text=text)

    def _is_bare_super_call(self, call_node: Node) -> bool:
        """True if `call_node` is exactly `super()` — a bare call to the
        name `super`, no arguments needed to check since Python's
        zero-arg `super()` is the only form worth recognizing here."""
        function_part = call_node.child_by_field_name("function")
        return function_part is not None and function_part.type == "identifier" and function_part.text == b"super"

    def _extract_property_reads_for_function(self, func_node: Node, source_code: str) -> list[RawPropertyRead]:
        """Collect every non-call attribute-access expression in
        `func_node`, not descending into a nested function's own body —
        same scoping rule as `_extract_calls_for_function`.
        """
        reads: list[RawPropertyRead] = []
        for child in func_node.children:
            self._collect_property_reads(child, source_code, reads)
        return reads

    def _collect_property_reads(self, node: Node, source_code: str, reads: list[RawPropertyRead]) -> None:
        if node.type == "function_definition":
            return

        if node.type == "call":
            # The call's own `function` part (e.g. the `obj.method` in
            # `obj.method(...)`) is a method call reference, already
            # handled by `_collect_calls` — never itself a property read.
            # Still walk *into* it (not skip it outright): a chained
            # receiver like `obj.attr.method()` can hold a real property
            # read (`obj.attr`) nested inside that same `function` part.
            function_part = node.child_by_field_name("function")
            for child in node.children:
                is_function_part = (
                    function_part is not None
                    and child.start_byte == function_part.start_byte
                    and child.end_byte == function_part.end_byte
                )
                if is_function_part:
                    for grandchild in child.children:
                        self._collect_property_reads(grandchild, source_code, reads)
                else:
                    self._collect_property_reads(child, source_code, reads)
            return

        if node.type == "attribute" and not self._is_assignment_target(node):
            read = self._extract_property_read(node, source_code)
            if read is not None:
                reads.append(read)

        # Falls through even after a successful extraction (not an
        # early return): a chain like `self.a.b` has the outer
        # `attribute` node's own `object` field be *another* `attribute`
        # node (`self.a`), which needs its own visit to be considered as
        # its own, separate property read.
        for child in node.children:
            self._collect_property_reads(child, source_code, reads)

    def _is_assignment_target(self, node: Node) -> bool:
        """True if `node` is the direct left-hand side of a plain `=`
        assignment (`self.x = ...`) — a write, not a read, so it's never
        a property read. Deliberately narrower than it could be:
        augmented assignment (`self.x += 1`) is left alone (still treated
        as a read) since it genuinely does invoke the getter first."""
        parent = node.parent
        if parent is None or parent.type != "assignment":
            return False
        left = parent.child_by_field_name("left")
        return left is not None and left.start_byte == node.start_byte and left.end_byte == node.end_byte

    def _extract_property_read(self, node: Node, source_code: str) -> Optional[RawPropertyRead]:
        """Extract one `attribute` node into a `RawPropertyRead`, or
        `None` if its receiver is too complex to represent — same
        discipline as `_extract_call` (never guessed)."""
        attribute_node = node.child_by_field_name("attribute")
        object_node = node.child_by_field_name("object")
        if attribute_node is None or object_node is None:
            return None
        name = attribute_node.text.decode("utf8")
        text = self._get_node_text(node, source_code)

        if object_node.type == "identifier":
            receiver = object_node.text.decode("utf8")
        elif object_node.type == "call" and self._is_bare_super_call(object_node):
            receiver = "super"
        else:
            return None  # receiver is some other expression — too complex, skip

        return RawPropertyRead(receiver=receiver, name=name, text=text)

    def _extract_module_constants(self, root: Node, source_code: str) -> list[ExtractedConstant]:
        """Module-level `NAME = <expr>`/`NAME: Type = <expr>` assignments
        only, directly under the module's own top level — not inside a
        class/function/if/try body (unlike `_extract_imports`, which
        walks the whole tree). Tuple/multi-target assignment
        (`a, b = ...`) and augmented assignment (`x += 1`) are both
        excluded — neither has a single plain-identifier target.
        """
        constants: list[ExtractedConstant] = []
        for top_level in root.children:
            node = top_level
            if node.type == "expression_statement" and len(node.children) == 1:
                node = node.children[0]
            if node.type != "assignment":
                continue
            left = node.child_by_field_name("left")
            if left is None or left.type != "identifier":
                continue
            constants.append(
                ExtractedConstant(
                    name=left.text.decode("utf8"),
                    value_text=self._get_node_text(top_level, source_code),
                    start_line=top_level.start_point[0] + 1,
                    end_line=top_level.end_point[0] + 1,
                )
            )
        return constants

    def _extract_symbol_reads_for_function(self, func_node: Node, source_code: str) -> list[RawSymbolRead]:
        """Collect every candidate bare-identifier read in `func_node`,
        not descending into a nested function's own body — same scoping
        rule as `_extract_calls_for_function`. A candidate list, not a
        final answer — see `RawSymbolRead`'s docstring for why this can't
        fully reject non-candidates the way `_extract_call`/
        `_extract_property_read` do.
        """
        param_names = self._collect_parameter_names(func_node)
        # The function's own name (`def handle_message(...)`) is a direct
        # `identifier` child of `func_node` too — excluded explicitly, or
        # every function would show up reading a "constant" matching its
        # own name the moment one happened to exist with that name.
        name_node = self._get_child_by_type(func_node, "identifier")
        reads: list[RawSymbolRead] = []
        for child in func_node.children:
            if name_node is not None and child.start_byte == name_node.start_byte and child.end_byte == name_node.end_byte:
                continue
            self._collect_symbol_reads(child, source_code, reads, param_names)
        return reads

    def _collect_parameter_names(self, func_node: Node) -> set[str]:
        """Every one of `func_node`'s own parameter names — excluded
        from symbol reads, since a parameter shadowing a module-level
        constant's name must never be mistaken for reading it."""
        names: set[str] = set()
        params_node = self._get_child_by_type(func_node, "parameters")
        if params_node is None:
            return names
        for child in params_node.children:
            if child.type == "identifier":
                names.add(child.text.decode("utf8"))
            elif child.type in ("typed_parameter", "typed_default_parameter", "default_parameter"):
                name_node = self._get_child_by_type(child, "identifier")
                if name_node is not None:
                    names.add(name_node.text.decode("utf8"))
        return names

    def _collect_symbol_reads(
        self,
        node: Node,
        source_code: str,
        reads: list[RawSymbolRead],
        param_names: set[str],
    ) -> None:
        if node.type == "function_definition":
            return

        if node.type == "call":
            # The call's own bare function name (`foo()`) is a function
            # reference, already handled by `_collect_calls` — never
            # itself a symbol read. An attribute-based call's function
            # part (`obj.method()`) is *not* skipped here: walking into
            # it is exactly what lets `obj` itself (a plain identifier)
            # still be collected as a real symbol read below.
            function_part = node.child_by_field_name("function")
            for child in node.children:
                is_bare_function_part = (
                    function_part is not None
                    and function_part.type == "identifier"
                    and child.start_byte == function_part.start_byte
                    and child.end_byte == function_part.end_byte
                )
                if is_bare_function_part:
                    continue
                self._collect_symbol_reads(child, source_code, reads, param_names)
            return

        if (
            node.type == "identifier"
            and not self._is_assignment_target(node)
            and not self._is_attribute_name_field(node)
            and node.text.decode("utf8") not in param_names
        ):
            reads.append(RawSymbolRead(name=node.text.decode("utf8"), text=self._get_node_text(node, source_code)))
            return

        for child in node.children:
            self._collect_symbol_reads(child, source_code, reads, param_names)

    def _is_attribute_name_field(self, node: Node) -> bool:
        """True if `node` is the `.attr` part of an `obj.attr` attribute
        node (the `attribute` field, not the `object` field) — never a
        symbol read on its own (`timeout` in `CONFIG.timeout` isn't a
        name reference, `CONFIG` is). Mirrors `_is_assignment_target`'s
        shape, for a different field on a different parent node type.
        """
        parent = node.parent
        if parent is None or parent.type != "attribute":
            return False
        attribute_field = parent.child_by_field_name("attribute")
        return (
            attribute_field is not None
            and attribute_field.start_byte == node.start_byte
            and attribute_field.end_byte == node.end_byte
        )

    def _extract_param_facts(self, node: Node) -> tuple[Optional[str], dict[str, str]]:
        """From a `function_definition` node's `parameters`, get the first
        parameter's name (for Python's `self`/`this`-equivalent detection
        — whatever it's actually named, not a hardcoded string) and any
        simple `name: Type` declared types.
        """
        params_node = self._get_child_by_type(node, "parameters")
        if params_node is None:
            return None, {}

        first_param_name: Optional[str] = None
        declared_types: dict[str, str] = {}

        for child in params_node.children:
            param_name: Optional[str] = None
            type_name: Optional[str] = None

            if child.type == "identifier":
                param_name = child.text.decode("utf8")
            elif child.type in ("typed_parameter", "typed_default_parameter", "default_parameter"):
                name_node = self._get_child_by_type(child, "identifier")
                if name_node is not None:
                    param_name = name_node.text.decode("utf8")
                type_node = self._get_child_by_type(child, "type")
                if type_node is not None:
                    # Only a plain identifier type (`Order`), not a
                    # generic/subscript (`Optional[Order]`) — never guess
                    # a type we can't resolve to a simple class name.
                    type_id = self._get_child_by_type(type_node, "identifier")
                    if type_id is not None and len(type_node.children) == 1:
                        type_name = type_id.text.decode("utf8")
                    elif len(type_node.children) == 1 and type_node.children[0].type == "attribute":
                        annotation = type_node.children[0].text.decode("utf8")
                        if all(part.isidentifier() for part in annotation.split('.')):
                            type_name = annotation
            else:
                continue  # splat patterns ("*args"/"**kwargs"), punctuation

            if param_name is not None and first_param_name is None:
                first_param_name = param_name
            if param_name is not None and type_name is not None:
                declared_types[param_name] = type_name

        return first_param_name, declared_types

    def _extract_imports(
        self,
        node: Node,
        name_imports: list[RawNameImport],
        module_imports: list[RawModuleImport],
    ) -> None:
        """Walk the whole file for `import`/`from...import` statements —
        not scoped to top-level only, since conditional imports (inside
        `if`/`try`) are common enough in real code to be worth catching
        too.
        """
        for child in node.children:
            if child.type == "import_statement":
                self._extract_import_statement(child, module_imports)
            elif child.type == "import_from_statement":
                self._extract_import_from_statement(child, name_imports)
            else:
                self._extract_imports(child, name_imports, module_imports)

    def _extract_import_statement(self, node: Node, module_imports: list[RawModuleImport]) -> None:
        """`import X` / `import X as Y` / `import X.Y as Z`."""
        for child in node.children:
            if child.type == "dotted_name":
                module_path = self._dotted_name_text(child)
                # No alias: Python binds just the first segment
                # ("os" for `import os.path`), not the full dotted path —
                # matches real Python name-binding semantics.
                module_imports.append(
                    RawModuleImport(module_path=module_path, alias=module_path.split(".")[0])
                )
            elif child.type == "aliased_import":
                dotted = self._get_child_by_type(child, "dotted_name")
                alias = self._get_child_by_type(child, "identifier")
                if dotted is not None and alias is not None:
                    module_imports.append(
                        RawModuleImport(
                            module_path=self._dotted_name_text(dotted),
                            alias=alias.text.decode("utf8"),
                        )
                    )

    def _extract_import_from_statement(self, node: Node, name_imports: list[RawNameImport]) -> None:
        """`from X import a, b as c` / `from . import a` / `from .pkg import a`."""
        module_node = None
        for child in node.children:
            if child.type in ("dotted_name", "relative_import"):
                module_node = child
                break
        if module_node is None:
            return
        module_path = self._module_path_text(module_node)

        # Everything after the module clause and the `import` keyword is
        # one import item, each either a bare `dotted_name` (just a name,
        # not a dotted path here — `from X import a.b` isn't valid Python,
        # so a single identifier is all `dotted_name` ever means in this
        # position) or an `aliased_import` (`a as b`).
        seen_import_keyword = False
        for child in node.children:
            if child.type == "import":
                seen_import_keyword = True
                continue
            if not seen_import_keyword:
                continue
            if child.type == "dotted_name":
                name_imports.append(RawNameImport(module_path=module_path, name=self._dotted_name_text(child)))
            elif child.type == "aliased_import":
                dotted = self._get_child_by_type(child, "dotted_name")
                alias = self._get_child_by_type(child, "identifier")
                if dotted is not None:
                    name_imports.append(
                        RawNameImport(
                            module_path=module_path,
                            name=self._dotted_name_text(dotted),
                            alias=alias.text.decode("utf8") if alias is not None else None,
                        )
                    )

    def _dotted_name_text(self, node: Node) -> str:
        """Join a `dotted_name` node's identifier children with '.'."""
        return ".".join(
            child.text.decode("utf8") for child in node.children if child.type == "identifier"
        )

    def _module_path_text(self, node: Node) -> str:
        """Text for a `from`-clause module, handling both an absolute
        `dotted_name` and a relative `relative_import` (leading dots,
        e.g. `.utils`, `..pkg.mod`) — file-stem matching later only cares
        about the last segment, so the leading dots are harmless noise,
        not something that needs special resolution here.
        """
        if node.type == "dotted_name":
            return self._dotted_name_text(node)
        # relative_import: child dots (import_prefix) + optional dotted_name
        dotted = self._get_child_by_type(node, "dotted_name")
        return dotted.text.decode("utf8") if dotted is not None else "."
