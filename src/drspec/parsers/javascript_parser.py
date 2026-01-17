"""JavaScript source code parser using Tree-sitter."""

from __future__ import annotations

from typing import Optional

import tree_sitter_javascript as tsjs
from tree_sitter import Language, Parser, Node

from drspec.parsers.models import ExtractedFunction, ParseError, ParseResult


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

    def __init__(self) -> None:
        """Initialize the JavaScript parser with tree-sitter version compatibility."""
        try:
            # Try new API first (some tree-sitter 0.21.x builds)
            self._language = Language(tsjs.language(), "javascript")
            self._parser = Parser()
            self._parser.set_language(self._language)
        except TypeError:
            # Fallback to old API (other tree-sitter 0.21.x builds)
            self._language = Language(tsjs.language())
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

        # Check for syntax errors
        if tree.root_node.has_error:
            result.has_errors = True
            self._collect_errors(tree.root_node, result.errors)

        # Extract functions from the AST
        raw_functions: list[ExtractedFunction] = []
        self._extract_functions(
            tree.root_node,
            source_code,
            raw_functions,
            parent=None,
        )

        # Deduplicate: when same function is defined multiple times,
        # keep the last definition (later in file takes precedence)
        seen: dict[str, ExtractedFunction] = {}
        for func in raw_functions:
            seen[func.qualified_name] = func

        result.functions = list(seen.values())
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
            # Handle function declarations
            if child.type == "function_declaration":
                func = self._extract_function_declaration(child, source_code, parent)
                if func:
                    functions.append(func)
                    # Extract nested functions
                    body = self._get_child_by_type(child, "statement_block")
                    if body:
                        self._extract_functions(body, source_code, functions, func.qualified_name)

            # Handle generator function declarations
            elif child.type == "generator_function_declaration":
                func = self._extract_function_declaration(child, source_code, parent, is_generator=True)
                if func:
                    functions.append(func)

            # Handle variable declarations (for arrow functions and function expressions)
            elif child.type == "variable_declaration" or child.type == "lexical_declaration":
                self._extract_variable_functions(child, source_code, functions, parent)

            # Handle class declarations
            elif child.type == "class_declaration":
                class_name = self._get_class_name(child)
                class_body = self._get_child_by_type(child, "class_body")
                if class_body:
                    self._extract_class_methods(class_body, source_code, functions, class_name)

            # Handle export statements
            elif child.type == "export_statement":
                self._extract_exports(child, source_code, functions, parent)

            else:
                # Continue traversing for other node types
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
        # Get function name
        name_node = self._get_child_by_type(node, "identifier")
        if not name_node:
            return None
        name = name_node.text.decode("utf8")

        # Build qualified name
        qualified_name = f"{parent}.{name}" if parent else name

        # Check if async
        is_async = self._has_async_modifier(node)

        # Get signature (first line)
        signature = self._get_signature(node, source_code)

        # Get full body
        body = self._get_node_text(node, source_code)

        # Get line numbers
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
                # Get the name from the identifier
                name_node = self._get_child_by_type(child, "identifier")
                if not name_node:
                    continue
                name = name_node.text.decode("utf8")

                # Check if the value is an arrow function or function expression
                value_node = None
                for subchild in child.children:
                    if subchild.type in ("arrow_function", "function_expression", "generator_function"):
                        value_node = subchild
                        break

                if not value_node:
                    continue

                qualified_name = f"{parent}.{name}" if parent else name
                is_async = self._has_async_modifier(value_node)

                # Get signature from the whole declaration
                signature = self._get_signature(node, source_code)

                # Get body
                body = self._get_node_text(node, source_code)

                # Get line numbers from the variable declaration
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
                )
                functions.append(func)

                # Extract nested functions from arrow function body
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

            elif child.type == "field_definition":
                # Handle class field with arrow function
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
        # Get method name
        name_node = self._get_child_by_type(node, "property_identifier")
        if not name_node:
            # Could be computed property or private identifier
            name_node = self._get_child_by_type(node, "private_property_identifier")
        if not name_node:
            return None

        name = name_node.text.decode("utf8")

        # Check if async
        is_async = self._has_async_modifier(node)

        # Check for getter/setter/static
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

        # Build qualified name - include get_/set_ prefix for getters/setters
        # to make them unique (JavaScript allows both get x() and set x() on same property)
        if is_getter:
            qualified_name = f"{class_name}.get_{name}"
        elif is_setter:
            qualified_name = f"{class_name}.set_{name}"
        else:
            qualified_name = f"{class_name}.{name}"

        # Get signature
        signature = self._get_signature(node, source_code)

        # Get body
        body = self._get_node_text(node, source_code)

        # Get line numbers
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
        # Get field name
        name_node = self._get_child_by_type(node, "property_identifier")
        if not name_node:
            return

        # Check if value is an arrow function
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

            elif child.type == "class_declaration":
                class_name = self._get_class_name(child)
                class_body = self._get_child_by_type(child, "class_body")
                if class_body:
                    self._extract_class_methods(class_body, source_code, functions, class_name)

            elif child.type in ("variable_declaration", "lexical_declaration"):
                self._extract_variable_functions(child, source_code, functions, parent)

            elif child.type == "default":
                # Default export - continue to check siblings
                continue

            else:
                # Recurse for other nested structures
                self._extract_exports(child, source_code, functions, parent)

    def _get_class_name(self, node: Node) -> str:
        """Get the class name from a class_declaration node."""
        name_node = self._get_child_by_type(node, "identifier")
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
        return source_code[node.start_byte : node.end_byte]

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
