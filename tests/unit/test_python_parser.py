"""Tests for the Python parser."""

import tempfile
from pathlib import Path

import pytest

from drspec.parsers.python_parser import PythonParser
from drspec.parsers.models import ExtractedFunction, ParseResult


@pytest.fixture
def parser():
    """Create a Python parser instance."""
    return PythonParser()


class TestBasicFunctionExtraction:
    """Tests for basic function extraction."""

    def test_extract_simple_function(self, parser):
        """Test extracting a simple function."""
        code = '''
def hello():
    return "world"
'''
        result = parser.parse(code)

        assert len(result.functions) == 1
        func = result.functions[0]
        assert func.name == "hello"
        assert func.qualified_name == "hello"
        assert "def hello()" in func.signature
        assert "return" in func.body

    def test_extract_function_with_params(self, parser):
        """Test extracting function with parameters."""
        code = '''
def add(x: int, y: int) -> int:
    return x + y
'''
        result = parser.parse(code)

        assert len(result.functions) == 1
        func = result.functions[0]
        assert func.name == "add"
        assert "x: int" in func.signature
        assert "y: int" in func.signature
        assert "-> int" in func.signature

    def test_extract_multiple_functions(self, parser):
        """Test extracting multiple functions."""
        code = '''
def foo():
    pass

def bar():
    pass

def baz():
    pass
'''
        result = parser.parse(code)

        assert len(result.functions) == 3
        names = [f.name for f in result.functions]
        assert "foo" in names
        assert "bar" in names
        assert "baz" in names

    def test_function_line_numbers(self, parser):
        """Test that line numbers are correct."""
        code = '''
def first():
    pass

def second():
    pass
'''
        result = parser.parse(code)

        assert len(result.functions) == 2
        first = next(f for f in result.functions if f.name == "first")
        second = next(f for f in result.functions if f.name == "second")

        assert first.start_line == 2
        assert second.start_line == 5


class TestClassMethods:
    """Tests for class method extraction."""

    def test_extract_class_methods(self, parser):
        """Test extracting methods from a class."""
        code = '''
class Calculator:
    def add(self, a, b):
        return a + b

    def subtract(self, a, b):
        return a - b
'''
        result = parser.parse(code)

        assert len(result.functions) == 2
        add_func = next(f for f in result.functions if f.name == "add")
        sub_func = next(f for f in result.functions if f.name == "subtract")

        assert add_func.qualified_name == "Calculator.add"
        assert add_func.is_method is True
        assert add_func.parent == "Calculator"

        assert sub_func.qualified_name == "Calculator.subtract"
        assert sub_func.is_method is True

    def test_extract_static_and_class_methods(self, parser):
        """Test extracting static and class methods."""
        code = '''
class MyClass:
    @staticmethod
    def static_method():
        pass

    @classmethod
    def class_method(cls):
        pass
'''
        result = parser.parse(code)

        assert len(result.functions) == 2
        static = next(f for f in result.functions if f.name == "static_method")
        cls_method = next(f for f in result.functions if f.name == "class_method")

        assert "staticmethod" in static.decorators
        assert "classmethod" in cls_method.decorators


class TestNestedFunctions:
    """Tests for nested function extraction."""

    def test_extract_nested_functions(self, parser):
        """Test extracting nested functions."""
        code = '''
def outer():
    def inner():
        return "nested"
    return inner()
'''
        result = parser.parse(code)

        assert len(result.functions) == 2
        outer = next(f for f in result.functions if f.name == "outer")
        inner = next(f for f in result.functions if f.name == "inner")

        assert outer.qualified_name == "outer"
        assert inner.qualified_name == "outer.inner"
        assert inner.parent == "outer"

    def test_deeply_nested_functions(self, parser):
        """Test extracting deeply nested functions."""
        code = '''
def level1():
    def level2():
        def level3():
            pass
        pass
    pass
'''
        result = parser.parse(code)

        assert len(result.functions) == 3
        names = {f.qualified_name for f in result.functions}
        assert "level1" in names
        assert "level1.level2" in names
        assert "level1.level2.level3" in names


class TestAsyncFunctions:
    """Tests for async function extraction."""

    def test_extract_async_function(self, parser):
        """Test extracting async functions."""
        code = '''
async def fetch_data():
    return await get_data()
'''
        result = parser.parse(code)

        assert len(result.functions) == 1
        func = result.functions[0]
        assert func.name == "fetch_data"
        assert func.is_async is True
        assert "async def" in func.signature

    def test_extract_async_method(self, parser):
        """Test extracting async class methods."""
        code = '''
class Client:
    async def fetch(self, url):
        return await self._get(url)
'''
        result = parser.parse(code)

        assert len(result.functions) == 1
        func = result.functions[0]
        assert func.is_async is True
        assert func.is_method is True


class TestDecorators:
    """Tests for decorator extraction."""

    def test_extract_single_decorator(self, parser):
        """Test extracting function with single decorator."""
        code = '''
@property
def value(self):
    return self._value
'''
        result = parser.parse(code)

        assert len(result.functions) == 1
        func = result.functions[0]
        assert "property" in func.decorators

    def test_extract_multiple_decorators(self, parser):
        """Test extracting function with multiple decorators."""
        code = '''
@decorator1
@decorator2
@decorator3
def decorated():
    pass
'''
        result = parser.parse(code)

        assert len(result.functions) == 1
        func = result.functions[0]
        assert len(func.decorators) == 3
        assert "decorator1" in func.decorators
        assert "decorator2" in func.decorators
        assert "decorator3" in func.decorators

    def test_decorator_with_arguments(self, parser):
        """Test extracting decorator with arguments."""
        code = '''
@route("/api/users", methods=["GET"])
def get_users():
    pass
'''
        result = parser.parse(code)

        assert len(result.functions) == 1
        func = result.functions[0]
        assert len(func.decorators) == 1
        assert 'route("/api/users"' in func.decorators[0]


class TestSyntaxErrors:
    """Tests for syntax error handling."""

    def test_reports_syntax_error(self, parser):
        """Test that syntax errors are reported."""
        code = '''
def broken(
    return x
'''
        result = parser.parse(code)

        assert result.has_errors is True
        assert len(result.errors) > 0

    def test_extracts_functions_despite_errors(self, parser):
        """Test that valid functions are extracted even with errors."""
        code = '''
def valid_func():
    return "ok"

def broken(
    x

def another_valid():
    pass
'''
        result = parser.parse(code)

        assert result.has_errors is True
        # Should still extract valid functions
        valid_names = {f.name for f in result.functions}
        assert "valid_func" in valid_names


class TestParseFile:
    """Tests for file parsing."""

    def test_parse_file(self, parser):
        """Test parsing a file from disk."""
        code = '''
def file_function():
    return "from file"
'''
        with tempfile.NamedTemporaryFile(
            mode="w", suffix=".py", delete=False
        ) as f:
            f.write(code)
            f.flush()
            temp_path = f.name

        try:
            result = parser.parse_file(temp_path)

            assert len(result.functions) == 1
            assert result.file_path == temp_path
            func = result.functions[0]
            assert func.name == "file_function"
        finally:
            Path(temp_path).unlink()

    def test_parse_file_not_found(self, parser):
        """Test parsing non-existent file raises error."""
        with pytest.raises(FileNotFoundError):
            parser.parse_file("/nonexistent/path.py")


class TestEdgeCases:
    """Tests for edge cases."""

    def test_empty_source(self, parser):
        """Test parsing empty source code."""
        result = parser.parse("")

        assert len(result.functions) == 0
        assert result.has_errors is False

    def test_no_functions(self, parser):
        """Test parsing code with no functions."""
        code = '''
x = 1
y = 2
print(x + y)
'''
        result = parser.parse(code)

        assert len(result.functions) == 0
        assert result.has_errors is False

    def test_lambda_not_extracted(self, parser):
        """Test that lambdas are not extracted as functions."""
        code = '''
square = lambda x: x ** 2
'''
        result = parser.parse(code)

        # Lambdas should not be extracted
        assert len(result.functions) == 0

    def test_function_in_if_block(self, parser):
        """Test extracting function defined in if block."""
        code = '''
if True:
    def conditional_func():
        pass
'''
        result = parser.parse(code)

        assert len(result.functions) == 1
        assert result.functions[0].name == "conditional_func"


class TestTreeSitterAPICompatibility:
    """Regression tests for tree-sitter API compatibility.

    These tests ensure the parser works across different tree-sitter versions.
    See system-review.md for details on the tree-sitter 0.21.x compatibility issue.
    """

    def test_parser_initialization(self):
        """Test that parser initializes without error.

        This is a regression test for the tree-sitter 0.21.x API change
        where Language() and Parser.set_language()/Parser.language changed.
        """
        # This should not raise any exceptions
        parser = PythonParser()
        assert parser is not None
        assert parser._language is not None
        assert parser._parser is not None

    def test_parser_can_parse_simple_code(self):
        """Test that parser can parse after initialization.

        Ensures Language is properly set on Parser.
        """
        parser = PythonParser()
        result = parser.parse("def test(): pass")

        assert result is not None
        assert len(result.functions) == 1
        assert result.functions[0].name == "test"

    def test_parser_language_attribute(self):
        """Test that parser has language properly configured."""
        parser = PythonParser()

        # The language should be a valid Language object
        assert parser._language is not None
        # Verify parser can actually parse (language is set correctly)
        result = parser.parse("def test(): pass")
        assert len(result.functions) == 1
