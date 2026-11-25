"""Tests for the Python parser."""


import pytest

from drspec.parsers.python_parser import PythonParser


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

    def test_parse_file(self, parser, tmp_path):
        """Test parsing a file from disk."""
        code = '''
def file_function():
    return "from file"
'''
        path = tmp_path / "source.py"
        path.write_text(code)
        temp_path = str(path)

        result = parser.parse_file(temp_path)

        assert len(result.functions) == 1
        assert result.file_path == temp_path
        func = result.functions[0]
        assert func.name == "file_function"

    def test_parse_file_not_found(self, parser, tmp_path):
        """Test parsing non-existent file raises error."""
        with pytest.raises(FileNotFoundError):
            parser.parse_file(str(tmp_path / "missing.py"))


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
    """Regression tests for tree-sitter API compatibility across versions."""

    def test_parser_initialization(self):
        """Regression test for the tree-sitter 0.21.x Language()/
        Parser.set_language() API change."""
        parser = PythonParser()
        assert parser is not None
        assert parser._language is not None
        assert parser._parser is not None

    def test_parser_can_parse_simple_code(self):
        """Ensures Language is properly set on Parser after initialization."""
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


class TestCallExtraction:
    """Tests for real call-site extraction."""

    def test_bare_call(self, parser):
        result = parser.parse("def foo():\n    bar()\n")
        calls = result.functions[0].calls

        assert len(calls) == 1
        assert calls[0].receiver is None
        assert calls[0].name == "bar"
        assert calls[0].text == "bar()"

    def test_attribute_call(self, parser):
        result = parser.parse("def foo():\n    order.process()\n")
        calls = result.functions[0].calls

        assert len(calls) == 1
        assert calls[0].receiver == "order"
        assert calls[0].name == "process"

    def test_super_call_tagged_with_sentinel(self, parser):
        result = parser.parse("class C:\n    def __init__(self):\n        super().__init__()\n")
        calls = result.functions[0].calls

        super_calls = [c for c in calls if c.name == "__init__" and c.receiver == "super"]
        assert len(super_calls) == 1

    def test_nested_call_in_arguments_also_extracted(self):
        parser = PythonParser()
        result = parser.parse("def foo():\n    outer(inner())\n")
        names = {c.name for c in result.functions[0].calls}

        assert names == {"outer", "inner"}

    def test_calls_in_nested_function_not_attributed_to_outer(self, parser):
        result = parser.parse(
            "def outer():\n    def inner():\n        inner_call()\n    outer_call()\n"
        )
        outer_func = next(f for f in result.functions if f.name == "outer")
        inner_func = next(f for f in result.functions if f.name == "inner")

        assert {c.name for c in outer_func.calls} == {"outer_call"}
        assert {c.name for c in inner_func.calls} == {"inner_call"}

    def test_call_inside_docstring_is_not_extracted(self, parser):
        """A doctest example inside a docstring can never be mistaken for a
        call — it's not a `call` AST node at all."""
        code = (
            'def foo():\n'
            '    """Example:\n'
            '        >>> foo()\n'
            '    """\n'
            '    return 1\n'
        )
        result = parser.parse(code)

        assert result.functions[0].calls == []

    def test_receiver_too_complex_is_skipped(self, parser):
        """`get_obj().method()` — the receiver is itself a call result,
        not `super()`, so it's too complex to represent — skipped, not
        guessed as a bare call."""
        result = parser.parse("def foo():\n    get_obj().method()\n")
        names = {c.name for c in result.functions[0].calls}

        # get_obj() itself is still extracted as its own bare call;
        # "method" is not, since its receiver isn't representable.
        assert names == {"get_obj"}


class TestPropertyReadExtraction:
    """Tests for non-call attribute-read extraction."""

    def test_simple_attribute_read(self, parser):
        result = parser.parse("def foo():\n    return obj.value\n")
        reads = result.functions[0].property_reads

        assert len(reads) == 1
        assert reads[0].receiver == "obj"
        assert reads[0].name == "value"
        assert reads[0].text == "obj.value"

    def test_method_call_is_not_a_property_read(self, parser):
        """`obj.method()` is a call, already covered by `calls` — never
        double-counted as a property read too."""
        result = parser.parse("def foo():\n    return obj.method()\n")

        assert result.functions[0].property_reads == []
        assert len(result.functions[0].calls) == 1

    def test_assignment_target_is_not_a_read(self, parser):
        """`self.x = 5` is a write, not a read."""
        result = parser.parse("class C:\n    def foo(self):\n        self.x = 5\n")
        foo = next(f for f in result.functions if f.name == "foo")

        assert foo.property_reads == []

    def test_augmented_assignment_target_is_still_a_read(self, parser):
        """`self.x += 1` genuinely invokes the getter first."""
        result = parser.parse("class C:\n    def foo(self):\n        self.x += 1\n")
        foo = next(f for f in result.functions if f.name == "foo")

        assert len(foo.property_reads) == 1
        assert foo.property_reads[0].name == "x"

    def test_super_property_read_tagged_with_sentinel(self, parser):
        result = parser.parse("class C:\n    def foo(self):\n        return super().value\n")
        foo = next(f for f in result.functions if f.name == "foo")

        assert len(foo.property_reads) == 1
        assert foo.property_reads[0].receiver == "super"
        assert foo.property_reads[0].name == "value"

    def test_chained_attribute_only_innermost_identifier_receiver_extracted(self, parser):
        """`other.helper.value` — the outer `.value` access has a
        complex receiver (`other.helper`, itself an `attribute` node, not
        a plain identifier), so it's skipped; the inner `other.helper`
        still qualifies on its own."""
        result = parser.parse("def foo():\n    return other.helper.value\n")
        reads = result.functions[0].property_reads

        assert len(reads) == 1
        assert reads[0].receiver == "other"
        assert reads[0].name == "helper"

    def test_property_read_inside_call_arguments_still_extracted(self, parser):
        result = parser.parse("def foo():\n    print(obj.value)\n")
        reads = result.functions[0].property_reads

        assert len(reads) == 1
        assert reads[0].receiver == "obj"
        assert reads[0].name == "value"

    def test_property_read_nested_in_chained_call_receiver_still_extracted(self, parser):
        """`obj.attr.method()` — the call's own callee reference
        (`obj.attr.method`) is never itself a property read, but the
        `obj.attr` nested inside it still is."""
        result = parser.parse("def foo():\n    return obj.attr.method()\n")
        reads = result.functions[0].property_reads

        assert len(reads) == 1
        assert reads[0].receiver == "obj"
        assert reads[0].name == "attr"

    def test_reads_in_nested_function_not_attributed_to_outer(self, parser):
        result = parser.parse(
            "def outer():\n    def inner():\n        return inner_obj.value\n    return outer_obj.value\n"
        )
        outer_func = next(f for f in result.functions if f.name == "outer")
        inner_func = next(f for f in result.functions if f.name == "inner")

        assert {r.receiver for r in outer_func.property_reads} == {"outer_obj"}
        assert {r.receiver for r in inner_func.property_reads} == {"inner_obj"}

    def test_receiver_too_complex_is_skipped(self, parser):
        result = parser.parse("def foo():\n    return get_obj().value\n")

        assert result.functions[0].property_reads == []


class TestIsPropertyDetection:
    """Tests for `@property` detection, including the getter/setter dedup fix."""

    def test_property_decorator_sets_is_property(self, parser):
        result = parser.parse("class C:\n    @property\n    def value(self):\n        return 1\n")

        assert result.functions[0].is_property is True

    def test_no_decorator_means_not_a_property(self, parser):
        result = parser.parse("class C:\n    def value(self):\n        return 1\n")

        assert result.functions[0].is_property is False

    def test_other_decorator_does_not_count(self, parser):
        result = parser.parse("class C:\n    @staticmethod\n    def value():\n        return 1\n")

        assert result.functions[0].is_property is False

    def test_getter_setter_pair_both_flagged_is_property(self, parser):
        """A getter and its setter are two functions (their parameters differ); the
        getter carries the flag and the setter, which is not `@property`-decorated, does not."""
        code = (
            "class C:\n"
            "    @property\n"
            "    def value(self):\n"
            "        return self._value\n"
            "\n"
            "    @value.setter\n"
            "    def value(self, v):\n"
            "        self._value = v\n"
        )
        result = parser.parse(code)

        assert [(f.qualified_name, f.parameter_tokens, f.is_property) for f in result.functions] == [
            ("C.value", [], True),
            ("C.value", ["v"], False),
        ]


class TestParamFactExtraction:
    """Tests for first-param-name and declared-type extraction."""

    def test_first_param_name_for_method(self, parser):
        result = parser.parse("class C:\n    def process(self):\n        pass\n")
        func = result.functions[0]

        assert func.first_param_name == "self"

    def test_first_param_name_whatever_its_actually_named(self, parser):
        result = parser.parse("class C:\n    def process(order):\n        pass\n")
        func = result.functions[0]

        assert func.first_param_name == "order"

    def test_declared_simple_type(self, parser):
        result = parser.parse("def handle(order: Order):\n    pass\n")
        func = result.functions[0]

        assert func.declared_param_types == {"order": "Order"}

    def test_declared_type_with_default_value(self, parser):
        result = parser.parse("def handle(order: Order = None):\n    pass\n")
        func = result.functions[0]

        assert func.declared_param_types == {"order": "Order"}

    def test_generic_type_not_captured(self, parser):
        """`Optional[Order]` isn't a plain identifier type — never guess
        a type we can't resolve to a simple class name."""
        result = parser.parse("def handle(order: Optional[Order]):\n    pass\n")
        func = result.functions[0]

        assert func.declared_param_types == {}

    def test_self_excluded_from_types(self, parser):
        result = parser.parse("class C:\n    def process(self, order: Order):\n        pass\n")
        func = result.functions[0]

        assert "self" not in func.declared_param_types
        assert func.declared_param_types == {"order": "Order"}


class TestImportExtraction:
    """Tests for real import-statement extraction."""

    def test_bare_name_import(self, parser):
        result = parser.parse("from utils import helper\n")

        assert len(result.name_imports) == 1
        assert result.name_imports[0].module_path == "utils"
        assert result.name_imports[0].name == "helper"
        assert result.name_imports[0].alias is None

    def test_aliased_name_import(self, parser):
        result = parser.parse("from utils import helper as h\n")

        assert result.name_imports[0].alias == "h"

    def test_multiple_names_one_statement(self, parser):
        result = parser.parse("from utils import helper, other\n")

        names = {i.name for i in result.name_imports}
        assert names == {"helper", "other"}

    def test_plain_module_import(self, parser):
        result = parser.parse("import os\n")

        assert len(result.module_imports) == 1
        assert result.module_imports[0].module_path == "os"
        assert result.module_imports[0].alias == "os"

    def test_aliased_module_import(self, parser):
        result = parser.parse("import os.path as osp\n")

        assert result.module_imports[0].module_path == "os.path"
        assert result.module_imports[0].alias == "osp"

    def test_relative_import(self, parser):
        result = parser.parse("from .utils import helper\n")

        assert result.name_imports[0].module_path == "utils"
        assert result.name_imports[0].name == "helper"
