"""C++ source code parser using Tree-sitter."""

from __future__ import annotations

from typing import Optional

import tree_sitter_cpp as tscpp
from tree_sitter import Language, Parser, Node

from drspec.parsers.models import ExtractedFunction, ParseError, ParseResult


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
        try:
            # Try new API first (some tree-sitter 0.21.x builds)
            self._language = Language(tscpp.language(), "cpp")
            self._parser = Parser()
            self._parser.set_language(self._language)
        except TypeError:
            # Fallback to old API (other tree-sitter 0.21.x builds)
            self._language = Language(tscpp.language())
            self._parser = Parser()
            self._parser.language = self._language

    def parse(self, source_code: str, file_path: Optional[str] = None) -> ParseResult:
        """Parse C++ source code and extract functions.

        Args:
            source_code: C++ source code as string.
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

        # Determine if this is a header file
        is_header = self._is_header_file(file_path) if file_path else False

        # Extract functions from the AST
        raw_functions: list[ExtractedFunction] = []
        self._extract_functions(
            tree.root_node,
            source_code,
            raw_functions,
            namespace=None,
            class_name=None,
            is_header=is_header,
        )

        # Deduplicate: prefer definitions over declarations
        # When both declaration and definition exist for the same function_id,
        # keep only the definition (the one without "declaration" in decorators)
        seen: dict[str, ExtractedFunction] = {}
        for func in raw_functions:
            is_declaration = "declaration" in func.decorators
            if func.qualified_name in seen:
                existing = seen[func.qualified_name]
                existing_is_declaration = "declaration" in existing.decorators
                # Replace declaration with definition
                if existing_is_declaration and not is_declaration:
                    seen[func.qualified_name] = func
                # Keep definition, skip this declaration
                # (if both are declarations or both are definitions, keep first)
            else:
                seen[func.qualified_name] = func

        result.functions = list(seen.values())
        return result

    def parse_file(self, file_path: str) -> ParseResult:
        """Parse a C++ file and extract functions.

        Args:
            file_path: Path to the C++ file.

        Returns:
            ParseResult containing extracted functions and any errors.

        Raises:
            FileNotFoundError: If the file doesn't exist.
            IOError: If the file can't be read.
        """
        with open(file_path, "r", encoding="utf-8") as f:
            source_code = f.read()
        return self.parse(source_code, file_path=file_path)

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
            # Handle namespace definitions
            if child.type == "namespace_definition":
                ns_name = self._get_namespace_name(child)
                full_ns = f"{namespace}::{ns_name}" if namespace else ns_name
                # Find the declaration_list (namespace body)
                body = self._get_child_by_type(child, "declaration_list")
                if body:
                    self._extract_functions(
                        body, source_code, functions, namespace=full_ns, class_name=None, is_header=is_header
                    )

            # Handle class/struct definitions
            elif child.type in ("class_specifier", "struct_specifier"):
                cls_name = self._get_class_name(child)
                if cls_name:
                    full_class = self._build_qualified_name(namespace, cls_name)
                    # Find the field_declaration_list (class body)
                    body = self._get_child_by_type(child, "field_declaration_list")
                    if body:
                        self._extract_class_methods(
                            body, source_code, functions, namespace, full_class, is_header
                        )

            # Handle function definitions
            elif child.type == "function_definition":
                func = self._extract_function_definition(
                    child, source_code, namespace, class_name, is_header
                )
                if func:
                    functions.append(func)

            # Handle template declarations
            elif child.type == "template_declaration":
                self._extract_template(
                    child, source_code, functions, namespace, class_name, is_header
                )

            # Handle function declarations (prototypes)
            elif child.type == "declaration" and is_header:
                func = self._extract_function_declaration(child, source_code, namespace)
                if func:
                    functions.append(func)

            else:
                # Continue traversing
                self._extract_functions(
                    child, source_code, functions, namespace, class_name, is_header
                )

    def _extract_class_methods(
        self,
        class_body: Node,
        source_code: str,
        functions: list[ExtractedFunction],
        namespace: Optional[str],
        class_name: str,
        is_header: bool,
    ) -> None:
        """Extract methods from a class body.

        Args:
            class_body: The field_declaration_list node.
            source_code: Original source code.
            functions: List to append extracted functions to.
            namespace: Current namespace.
            class_name: Full qualified class name.
            is_header: True if parsing a header file.
        """
        current_access = "private"  # Default for classes

        for child in class_body.children:
            # Track access specifiers
            if child.type == "access_specifier":
                access_text = child.text.decode("utf8").strip().rstrip(":")
                current_access = access_text

            # In-class function definitions
            elif child.type == "function_definition":
                func = self._extract_method(
                    child, source_code, namespace, class_name, current_access, is_header
                )
                if func:
                    functions.append(func)

            # Method declarations (prototypes in class)
            elif child.type in ("declaration", "field_declaration"):
                func = self._extract_method_declaration(
                    child, source_code, namespace, class_name, current_access, is_header
                )
                if func:
                    functions.append(func)

            # Template method
            elif child.type == "template_declaration":
                self._extract_template(
                    child, source_code, functions, namespace, class_name, is_header
                )

            # Nested class
            elif child.type in ("class_specifier", "struct_specifier"):
                nested_name = self._get_class_name(child)
                if nested_name:
                    full_nested = f"{class_name}::{nested_name}"
                    nested_body = self._get_child_by_type(child, "field_declaration_list")
                    if nested_body:
                        self._extract_class_methods(
                            nested_body, source_code, functions, namespace, full_nested, is_header
                        )

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
        # Get declarator which contains name and params
        declarator = self._get_child_by_type(node, "function_declarator")
        if not declarator:
            # Try finding it nested inside a reference_declarator or pointer_declarator
            declarator = self._find_function_declarator(node)
        if not declarator:
            return None

        # Extract name and determine if it's a class method
        name_info = self._get_function_name(declarator)
        if not name_info:
            return None

        name, method_class = name_info

        # Determine the qualified name
        if method_class:
            # Out-of-class definition like ClassName::method
            qualified_name = self._build_qualified_name(namespace, method_class, name)
            parent = self._build_qualified_name(namespace, method_class)
            is_method = True
        elif class_name:
            # In-class definition
            qualified_name = f"{class_name}::{name}"
            parent = class_name
            is_method = True
        else:
            # Free function
            qualified_name = self._build_qualified_name(namespace, name)
            parent = namespace
            is_method = False

        # Get signature and body
        signature = self._get_signature(node, source_code)
        body = self._get_node_text(node, source_code)

        # Get line numbers
        start_line = node.start_point[0] + 1
        end_line = node.end_point[0] + 1

        # Check for virtual, static, etc.
        decorators = self._get_function_specifiers(node)
        if is_header and not self._has_body(node):
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
            is_async=False,  # C++ doesn't have async keyword like Python/JS
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
        # Get declarator
        declarator = self._get_child_by_type(node, "function_declarator")
        if not declarator:
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
        # Find function declarator within the declaration
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
        # Find the templated entity (function_definition, declaration, or class_specifier)
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
        # Look for identifier or namespace_identifier
        for child in node.children:
            if child.type in ("identifier", "namespace_identifier"):
                return child.text.decode("utf8")
        return "<anonymous>"

    def _get_class_name(self, node: Node) -> Optional[str]:
        """Get class name from class_specifier or struct_specifier node."""
        name_node = self._get_child_by_type(node, "type_identifier")
        if name_node:
            return name_node.text.decode("utf8")
        return None

    def _get_function_name(self, declarator: Node) -> Optional[tuple[str, Optional[str]]]:
        """Get function name and class qualifier from declarator.

        Args:
            declarator: The function_declarator node.

        Returns:
            Tuple of (name, class_name) where class_name is None for free functions.
        """
        # Look for qualified_identifier (ClassName::method) or plain identifier
        for child in declarator.children:
            if child.type == "qualified_identifier":
                # Out-of-class definition
                parts = self._parse_qualified_identifier(child)
                if len(parts) >= 2:
                    return parts[-1], "::".join(parts[:-1])
                elif len(parts) == 1:
                    return parts[0], None

            elif child.type in ("identifier", "field_identifier"):
                return child.text.decode("utf8"), None

            elif child.type == "destructor_name":
                # Destructor like ~ClassName
                return child.text.decode("utf8"), None

            elif child.type == "operator_name":
                # Operator overload
                return child.text.decode("utf8"), None

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
            elif child.type == "destructor_name":
                parts.append(child.text.decode("utf8"))
            elif child.type == "template_type":
                # Handle template instantiation like vector<int>
                type_node = self._get_child_by_type(child, "type_identifier")
                if type_node:
                    parts.append(type_node.text.decode("utf8"))
        return parts

    def _find_function_declarator(self, node: Node) -> Optional[Node]:
        """Recursively find a function_declarator node."""
        for child in node.children:
            if child.type == "function_declarator":
                return child
            # Look inside init_declarator, reference_declarator, pointer_declarator
            if child.type in ("init_declarator", "reference_declarator", "pointer_declarator", "declarator"):
                result = self._find_function_declarator(child)
                if result:
                    return result
        return None

    def _has_body(self, node: Node) -> bool:
        """Check if function has a body (compound_statement)."""
        return self._get_child_by_type(node, "compound_statement") is not None

    def _get_function_specifiers(self, node: Node) -> list[str]:
        """Extract function specifiers like virtual, static, const, etc."""
        specifiers = []

        # Look through all children for specifiers
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
        lines = source_code.split("\n")
        start_line = node.start_point[0]

        # Find where the body starts
        body = self._get_child_by_type(node, "compound_statement")
        if body:
            body_line = body.start_point[0]
            if body_line > start_line:
                # Signature spans multiple lines
                sig_lines = lines[start_line:body_line]
                return " ".join(line.strip() for line in sig_lines).strip()

        # Single line or declaration without body
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
        return source_code[node.start_byte:node.end_byte]

    def _collect_errors(self, node: Node, errors: list[ParseError]) -> None:
        """Recursively collect syntax errors from the AST."""
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
