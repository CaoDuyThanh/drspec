"""Tests for the JavaScript parser: what it extracts from source text (names, signatures, bodies, lines, calls, imports, parameters)."""


import pytest

from drspec.parsers.javascript_parser import JavaScriptParser

pytestmark = pytest.mark.unit  # source text in, parse result out


@pytest.fixture
def parser():
    return JavaScriptParser()


class TestFunctionDeclarations:
    """Tests for function declaration extraction."""

    def test_extract_simple_function(self, parser):
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
        assert func.signature == "function hello() {"
        assert func.body == 'function hello() {\n    return "world";\n}'
        assert (func.start_line, func.end_line) == (2, 4)
        assert func.is_method is False and func.parent is None

    def test_extract_function_with_params(self, parser):
        code = '''
function add(a, b) {
    return a + b;
}
'''
        result = parser.parse(code)

        assert len(result.functions) == 1
        func = result.functions[0]
        assert func.name == "add"
        assert func.signature == "function add(a, b) {"
        assert func.parameter_tokens == ["a", "b"]

    def test_extract_async_function(self, parser):
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
        assert func.signature == "async function fetchData() {"

    def test_extract_generator_function(self, parser):
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
        assert func.signature == "function* generateNumbers() {"
        assert (func.start_line, func.end_line) == (2, 5)


class TestArrowFunctions:
    """Tests for arrow function extraction."""

    def test_extract_arrow_function(self, parser):
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
        assert func.signature == "const greet = () => {"
        assert (func.start_line, func.end_line) == (2, 4)

    def test_extract_arrow_with_params(self, parser):
        code = '''
const add = (a, b) => a + b;
'''
        result = parser.parse(code)

        assert len(result.functions) == 1
        func = result.functions[0]
        assert func.name == "add"
        assert func.signature == func.body == "const add = (a, b) => a + b;"  # an expression body: the whole statement is both
        assert (func.start_line, func.end_line) == (2, 2)

    def test_extract_async_arrow(self, parser):
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
        assert func.signature == "const fetch = async (url) => {"

    def test_extract_let_arrow(self, parser):
        code = '''
let handler = (event) => {
    console.log(event);
};
'''
        result = parser.parse(code)

        assert len(result.functions) == 1
        func = result.functions[0]
        assert func.name == "handler"
        assert func.signature == "let handler = (event) => {"


class TestFunctionExpressions:
    """Tests for function expression extraction."""

    def test_extract_function_expression(self, parser):
        code = '''
const sayHello = function() {
    return "hello";
};
'''
        result = parser.parse(code)

        assert len(result.functions) == 1
        func = result.functions[0]
        assert func.name == "sayHello"
        assert func.signature == "const sayHello = function() {"
        assert (func.start_line, func.end_line) == (2, 4)


class TestClassMethods:
    """Tests for class method extraction."""

    def test_extract_class_methods(self, parser):
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
        assert (add_func.start_line, add_func.end_line) == (3, 5)  # the lines of the method, not of the class

        assert sub_func.qualified_name == "Calculator.subtract"
        assert (sub_func.start_line, sub_func.end_line) == (7, 9)

    def test_extract_constructor(self, parser):
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
        assert func.is_method is True

    def test_extract_getters_setters(self, parser):
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
        # the two share a name: what tells them apart is the qualified name
        assert (getter.qualified_name, setter.qualified_name) == ("Person.get_name", "Person.set_name")
        assert getter.signature == "get name() {" and setter.signature == "set name(value) {"

    def test_extract_static_method(self, parser):
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
        assert func.decorators == ["static"]
        assert func.qualified_name == "MathUtils.add"

    def test_extract_async_method(self, parser):
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
        assert func.qualified_name == "Client.fetch"


class TestExports:
    """Tests for export statement handling."""

    def test_extract_exported_function(self, parser):
        code = '''
export function add(a, b) {
    return a + b;
}
'''
        result = parser.parse(code)

        assert len(result.functions) == 1
        func = result.functions[0]
        assert func.name == "add"
        assert func.decorators == ["export"]
        assert func.signature == "export function add(a, b) {"

    def test_extract_default_exported_function(self, parser):
        code = '''
export default function main() {
    console.log("main");
}
'''
        result = parser.parse(code)

        assert len(result.functions) == 1
        func = result.functions[0]
        assert func.name == "main"
        assert func.decorators == ["export"]
        assert func.signature == "export default function main() {"

    def test_extract_exported_arrow(self, parser):
        code = '''
export const greet = () => "hello";
'''
        result = parser.parse(code)

        assert len(result.functions) == 1
        func = result.functions[0]
        assert func.name == "greet"
        assert func.signature == 'export const greet = () => "hello";'
        assert (func.start_line, func.end_line) == (2, 2)

    def test_extract_exported_class_methods(self, parser):
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
        assert func.is_method is True and func.parent == "Service"


class TestSyntaxErrors:
    """Tests for syntax error handling."""

    def test_reports_syntax_error(self, parser):
        code = '''
function broken( {
    return x;
}
'''
        result = parser.parse(code)

        assert result.has_errors is True
        assert [(e.line, e.column) for e in result.errors] == [(2, 0), (3, 11)]  # where the text stops making sense, 1-based lines
        assert result.functions == []  # nothing recognisable as a function

    def test_extracts_functions_despite_errors(self, parser):
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
        assert [e.line for e in result.errors] == [6]  # the line of `function broken(`
        valid = [f for f in result.functions if f.name == "valid"]
        assert len(valid) == 1 and (valid[0].start_line, valid[0].end_line) == (2, 4)  # the function before the break is whole
        assert "broken" not in {f.name for f in result.functions}


class TestParseFile:
    """Tests for file parsing."""

    def test_parse_file(self, parser, tmp_path):
        code = '''
function fileFunction() {
    return "from file";
}
'''
        path = tmp_path / "source.js"
        path.write_text(code)
        temp_path = str(path)

        result = parser.parse_file(temp_path)

        assert len(result.functions) == 1
        assert result.file_path == temp_path
        func = result.functions[0]
        assert func.name == "fileFunction"
        assert (func.start_line, func.end_line) == (2, 4)


class TestEdgeCases:
    """Tests for edge cases."""

    def test_empty_source(self, parser):
        result = parser.parse("")

        assert result.functions == []
        assert result.errors == [] and result.has_errors is False

    def test_no_functions(self, parser):
        code = '''
const x = 1;
const y = 2;
console.log(x + y);
'''
        result = parser.parse(code)

        assert result.functions == []
        assert result.has_errors is False

    def test_multiple_functions(self, parser):
        code = '''
function foo() {}
const bar = () => {};
function baz() {}
'''
        result = parser.parse(code)

        assert [(f.name, f.start_line) for f in result.functions] == [("foo", 2), ("bar", 3), ("baz", 4)]  # in the order of the file


class TestCallExtraction:
    """Tests for real call-site extraction."""

    def test_bare_call(self, parser):
        result = parser.parse("function foo() {\n  bar();\n}")
        calls = result.functions[0].calls

        assert [(c.receiver, c.name) for c in calls] == [(None, "bar")]


    def test_this_call(self, parser):
        code = "class Widget {\n  run() {\n    this.helper();\n  }\n}"
        result = parser.parse(code)
        run_func = next(f for f in result.functions if f.name == "run")

        assert [(c.receiver, c.name) for c in run_func.calls] == [("this", "helper")]

    def test_super_call_tagged_with_sentinel(self, parser):
        code = "class Widget {\n  run() {\n    super.helper();\n  }\n}"
        result = parser.parse(code)
        run_func = next(f for f in result.functions if f.name == "run")

        assert [(c.receiver, c.name) for c in run_func.calls] == [("super", "helper")]

    def test_module_qualified_call(self, parser):
        result = parser.parse("function foo() {\n  mymodule.helper();\n}")
        calls = result.functions[0].calls

        assert [(c.receiver, c.name) for c in calls] == [("mymodule", "helper")]

    def test_calls_in_nested_function_not_attributed_to_outer(self, parser):
        code = "function outer() {\n  function inner() {\n    inner_call();\n  }\n  outer_call();\n}"
        result = parser.parse(code)
        outer_func = next(f for f in result.functions if f.name == "outer")

        assert {c.name for c in outer_func.calls} == {"outer_call"}
        inner_func = next(f for f in result.functions if f.name == "inner")
        assert {c.name for c in inner_func.calls} == {"inner_call"}  # the inner call belongs to the inner function, and is not lost


class TestImportExtraction:
    """Tests for real import-statement extraction."""

    def test_named_import(self, parser):
        result = parser.parse('import { helper } from "./utils";')

        assert [(i.module_path, i.name, i.alias) for i in result.name_imports] == [("./utils", "helper", None)]
        assert result.module_imports == []

    def test_aliased_named_import(self, parser):
        result = parser.parse('import { helper as h } from "./utils";')

        assert [(i.module_path, i.name, i.alias) for i in result.name_imports] == [("./utils", "helper", "h")]

    def test_multiple_named_imports(self, parser):
        result = parser.parse('import { helper, other } from "./utils";')

        assert [(i.module_path, i.name) for i in result.name_imports] == [("./utils", "helper"), ("./utils", "other")]

    def test_namespace_import(self, parser):
        result = parser.parse('import * as utils from "./utils";')

        assert [(i.module_path, i.alias) for i in result.module_imports] == [("./utils", "utils")]
        assert result.name_imports == []

    def test_default_import_is_skipped(self, parser):
        """Can't know which specific export a default import binds to —
        never guessed."""
        result = parser.parse('import defaultThing from "./utils";')

        assert result.name_imports == []
        assert result.module_imports == []
