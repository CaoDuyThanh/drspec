"""Tests for the JavaScript parser."""

import tempfile
from pathlib import Path

import pytest

from drspec.parsers.javascript_parser import JavaScriptParser


@pytest.fixture
def parser():
    """Create a JavaScript parser instance."""
    return JavaScriptParser()


class TestFunctionDeclarations:
    """Tests for function declaration extraction."""

    def test_extract_simple_function(self, parser):
        """Test extracting a simple function declaration."""
        code = '''
function hello() {
    return "world";
}
'''
        result = parser.parse(code)

        assert len(result.functions) == 1
        func = result.functions[0]
        assert func.name == "hello"
        assert func.qualified_name == "hello"
        assert "function hello()" in func.signature
        assert "return" in func.body

    def test_extract_function_with_params(self, parser):
        """Test extracting function with parameters."""
        code = '''
function add(a, b) {
    return a + b;
}
'''
        result = parser.parse(code)

        assert len(result.functions) == 1
        func = result.functions[0]
        assert func.name == "add"
        assert "a, b" in func.signature

    def test_extract_async_function(self, parser):
        """Test extracting async function."""
        code = '''
async function fetchData() {
    return await getData();
}
'''
        result = parser.parse(code)

        assert len(result.functions) == 1
        func = result.functions[0]
        assert func.name == "fetchData"
        assert func.is_async is True
        assert "async function" in func.signature

    def test_extract_generator_function(self, parser):
        """Test extracting generator function."""
        code = '''
function* generateNumbers() {
    yield 1;
    yield 2;
}
'''
        result = parser.parse(code)

        assert len(result.functions) == 1
        func = result.functions[0]
        assert func.name == "generateNumbers"


class TestArrowFunctions:
    """Tests for arrow function extraction."""

    def test_extract_arrow_function(self, parser):
        """Test extracting arrow function."""
        code = '''
const greet = () => {
    return "hello";
};
'''
        result = parser.parse(code)

        assert len(result.functions) == 1
        func = result.functions[0]
        assert func.name == "greet"
        assert func.qualified_name == "greet"

    def test_extract_arrow_with_params(self, parser):
        """Test extracting arrow function with parameters."""
        code = '''
const add = (a, b) => a + b;
'''
        result = parser.parse(code)

        assert len(result.functions) == 1
        func = result.functions[0]
        assert func.name == "add"

    def test_extract_async_arrow(self, parser):
        """Test extracting async arrow function."""
        code = '''
const fetch = async (url) => {
    return await get(url);
};
'''
        result = parser.parse(code)

        assert len(result.functions) == 1
        func = result.functions[0]
        assert func.name == "fetch"
        assert func.is_async is True

    def test_extract_let_arrow(self, parser):
        """Test extracting arrow function with let."""
        code = '''
let handler = (event) => {
    console.log(event);
};
'''
        result = parser.parse(code)

        assert len(result.functions) == 1
        func = result.functions[0]
        assert func.name == "handler"


class TestFunctionExpressions:
    """Tests for function expression extraction."""

    def test_extract_function_expression(self, parser):
        """Test extracting function expression."""
        code = '''
const sayHello = function() {
    return "hello";
};
'''
        result = parser.parse(code)

        assert len(result.functions) == 1
        func = result.functions[0]
        assert func.name == "sayHello"


class TestClassMethods:
    """Tests for class method extraction."""

    def test_extract_class_methods(self, parser):
        """Test extracting methods from a class."""
        code = '''
class Calculator {
    add(a, b) {
        return a + b;
    }

    subtract(a, b) {
        return a - b;
    }
}
'''
        result = parser.parse(code)

        assert len(result.functions) == 2
        add_func = next(f for f in result.functions if f.name == "add")
        sub_func = next(f for f in result.functions if f.name == "subtract")

        assert add_func.qualified_name == "Calculator.add"
        assert add_func.is_method is True
        assert add_func.parent == "Calculator"

        assert sub_func.qualified_name == "Calculator.subtract"

    def test_extract_constructor(self, parser):
        """Test extracting constructor."""
        code = '''
class Person {
    constructor(name) {
        this.name = name;
    }
}
'''
        result = parser.parse(code)

        assert len(result.functions) == 1
        func = result.functions[0]
        assert func.name == "constructor"
        assert func.qualified_name == "Person.constructor"

    def test_extract_getters_setters(self, parser):
        """Test extracting getters and setters."""
        code = '''
class Person {
    get name() {
        return this._name;
    }

    set name(value) {
        this._name = value;
    }
}
'''
        result = parser.parse(code)

        assert len(result.functions) == 2
        getter = next(f for f in result.functions if "getter" in f.decorators)
        setter = next(f for f in result.functions if "setter" in f.decorators)

        assert getter.name == "name"
        assert setter.name == "name"

    def test_extract_static_method(self, parser):
        """Test extracting static method."""
        code = '''
class MathUtils {
    static add(a, b) {
        return a + b;
    }
}
'''
        result = parser.parse(code)

        assert len(result.functions) == 1
        func = result.functions[0]
        assert func.name == "add"
        assert "static" in func.decorators

    def test_extract_async_method(self, parser):
        """Test extracting async class method."""
        code = '''
class Client {
    async fetch(url) {
        return await this.get(url);
    }
}
'''
        result = parser.parse(code)

        assert len(result.functions) == 1
        func = result.functions[0]
        assert func.is_async is True
        assert func.is_method is True


class TestExports:
    """Tests for export statement handling."""

    def test_extract_exported_function(self, parser):
        """Test extracting exported function."""
        code = '''
export function add(a, b) {
    return a + b;
}
'''
        result = parser.parse(code)

        assert len(result.functions) == 1
        func = result.functions[0]
        assert func.name == "add"
        assert "export" in func.decorators

    def test_extract_default_exported_function(self, parser):
        """Test extracting default exported function."""
        code = '''
export default function main() {
    console.log("main");
}
'''
        result = parser.parse(code)

        assert len(result.functions) == 1
        func = result.functions[0]
        assert func.name == "main"

    def test_extract_exported_arrow(self, parser):
        """Test extracting exported arrow function."""
        code = '''
export const greet = () => "hello";
'''
        result = parser.parse(code)

        assert len(result.functions) == 1
        func = result.functions[0]
        assert func.name == "greet"

    def test_extract_exported_class_methods(self, parser):
        """Test extracting methods from exported class."""
        code = '''
export class Service {
    start() {
        return true;
    }
}
'''
        result = parser.parse(code)

        assert len(result.functions) == 1
        func = result.functions[0]
        assert func.qualified_name == "Service.start"


class TestSyntaxErrors:
    """Tests for syntax error handling."""

    def test_reports_syntax_error(self, parser):
        """Test that syntax errors are reported."""
        code = '''
function broken( {
    return x;
}
'''
        result = parser.parse(code)

        assert result.has_errors is True
        assert len(result.errors) > 0

    def test_extracts_functions_despite_errors(self, parser):
        """Test that valid functions are extracted even with errors."""
        code = '''
function valid() {
    return "ok";
}

function broken(

function another() {
    return "also ok";
}
'''
        result = parser.parse(code)

        assert result.has_errors is True
        # Should still extract valid functions
        valid_names = {f.name for f in result.functions}
        assert "valid" in valid_names


class TestParseFile:
    """Tests for file parsing."""

    def test_parse_file(self, parser):
        """Test parsing a file from disk."""
        code = '''
function fileFunction() {
    return "from file";
}
'''
        with tempfile.NamedTemporaryFile(
            mode="w", suffix=".js", delete=False
        ) as f:
            f.write(code)
            f.flush()
            temp_path = f.name

        try:
            result = parser.parse_file(temp_path)

            assert len(result.functions) == 1
            assert result.file_path == temp_path
            func = result.functions[0]
            assert func.name == "fileFunction"
        finally:
            Path(temp_path).unlink()


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
const x = 1;
const y = 2;
console.log(x + y);
'''
        result = parser.parse(code)

        assert len(result.functions) == 0

    def test_multiple_functions(self, parser):
        """Test extracting multiple functions."""
        code = '''
function foo() {}
const bar = () => {};
function baz() {}
'''
        result = parser.parse(code)

        assert len(result.functions) == 3
        names = {f.name for f in result.functions}
        assert names == {"foo", "bar", "baz"}
