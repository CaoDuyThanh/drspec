"""Python source code parser using Tree-sitter."""

from __future__ import annotations

from typing import Optional

import tree_sitter_python as tspython
from tree_sitter import Language, Parser, Node

from drspec.parsers.models import ExtractedFunction, ParseError, ParseResult


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
            # Try new API first (some tree-sitter 0.21.x builds)
            self._language = Language(tspython.language(), "python")
            self._parser = Parser()
            self._parser.set_language(self._language)
        except TypeError:
            # Fallback to old API (other tree-sitter 0.21.x builds)
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

        # Deduplicate: when same function is defined multiple times (e.g., copy-paste error),
        # keep the last definition (later in file takes precedence)
        seen: dict[str, ExtractedFunction] = {}
        for func in raw_functions:
            # Later definitions overwrite earlier ones
            seen[func.qualified_name] = func

        result.functions = list(seen.values())
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
                # Extract nested functions
                self._extract_functions(
                    child,
                    source_code,
                    functions,
                    parent=func.qualified_name,
                )

            elif child.type == "class_definition":
                class_name = self._get_class_name(child)
                # Extract methods from class body
                class_body = self._get_child_by_type(child, "block")
                if class_body:
                    self._extract_class_methods(
                        class_body,
                        source_code,
                        functions,
                        class_name=class_name,
                    )

            else:
                # Continue traversing for other node types
                self._extract_functions(child, source_code, functions, parent)

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
                # Extract nested functions within methods
                self._extract_functions(
                    child,
                    source_code,
                    functions,
                    parent=func.qualified_name,
                )

            elif child.type == "decorated_definition":
                # Handle decorated methods
                func_node = self._get_child_by_type(child, "function_definition")
                if func_node:
                    func = self._extract_function(
                        func_node, source_code, parent=class_name, is_method=True
                    )
                    # Add decorators from the decorated_definition
                    func.decorators = self._extract_decorators(child)
                    functions.append(func)

            elif child.type == "class_definition":
                # Nested class
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
        # Get function name
        name_node = self._get_child_by_type(node, "identifier")
        name = name_node.text.decode("utf8") if name_node else "<unknown>"

        # Build qualified name
        qualified_name = f"{parent}.{name}" if parent else name

        # Check if async
        is_async = any(child.type == "async" for child in node.children)

        # Get signature (first line)
        signature = self._get_signature(node, source_code)

        # Get full body
        body = self._get_node_text(node, source_code)

        # Get line numbers (tree-sitter uses 0-indexed lines)
        start_line = node.start_point[0] + 1
        end_line = node.end_point[0] + 1

        # Extract decorators (if decorated_definition parent)
        decorators: list[str] = []
        if node.parent and node.parent.type == "decorated_definition":
            decorators = self._extract_decorators(node.parent)

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

        # Handle multi-line signatures
        body_node = self._get_child_by_type(node, "block")
        if body_node and body_node.start_point[0] > start_line:
            # Signature spans multiple lines
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
        return source_code[node.start_byte : node.end_byte]

    def _extract_decorators(self, decorated_node: Node) -> list[str]:
        """Extract decorator strings from a decorated_definition node."""
        decorators = []
        for child in decorated_node.children:
            if child.type == "decorator":
                # Get decorator text without @
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
