"""Tests for the C++ parser: what it extracts from source text (names, signatures, lines, parameters, calls, imports, errors)."""


import pytest

from drspec.parsers.cpp_parser import CppParser

pytestmark = pytest.mark.unit  # source text in, parse result out


@pytest.fixture
def parser():
    return CppParser()


class TestFreeFunctions:
    def test_extract_simple_function(self, parser):
        code = '''
void hello() {
    return;
}
'''
        result = parser.parse(code)

        assert len(result.functions) == 1
        func = result.functions[0]
        assert func.name == "hello"
        assert func.qualified_name == "hello"
        assert func.signature == "void hello() {"
        assert (func.start_line, func.end_line) == (2, 4)
        assert func.is_method is False and func.parent is None and func.parameter_tokens == []

    def test_extract_function_with_params(self, parser):
        code = '''
int add(int a, int b) {
    return a + b;
}
'''
        result = parser.parse(code)

        assert len(result.functions) == 1
        func = result.functions[0]
        assert func.name == "add"
        assert func.signature == "int add(int a, int b) {"
        assert func.parameter_tokens == ["int", "int"]  # the types: what an identifier is made of

    def test_extract_function_with_return_type(self, parser):
        code = '''
std::vector<int> getNumbers() {
    return {};
}
'''
        result = parser.parse(code)

        assert len(result.functions) == 1
        func = result.functions[0]
        assert func.name == "getNumbers"
        assert func.signature == "std::vector<int> getNumbers() {"


class TestNamespaces:
    def test_extract_namespaced_function(self, parser):
        code = '''
namespace utils {
    void helper() {
        return;
    }
}
'''
        result = parser.parse(code)

        assert len(result.functions) == 1
        func = result.functions[0]
        assert func.name == "helper"
        assert func.qualified_name == "utils::helper"
        assert func.parent == "utils"
        assert (func.start_line, func.end_line) == (3, 5)

    def test_extract_nested_namespace(self, parser):
        code = '''
namespace outer {
    namespace inner {
        void deep() {
            return;
        }
    }
}
'''
        result = parser.parse(code)

        assert len(result.functions) == 1
        func = result.functions[0]
        assert func.name == "deep"
        assert func.qualified_name == "outer::inner::deep"

    def test_extract_multiple_namespaced_functions(self, parser):
        code = '''
namespace math {
    int add(int a, int b) {
        return a + b;
    }

    int subtract(int a, int b) {
        return a - b;
    }
}
'''
        result = parser.parse(code)

        assert [(f.qualified_name, f.start_line, f.end_line) for f in result.functions] == [("math::add", 3, 5), ("math::subtract", 7, 9)]


class TestClassMethods:
    def test_extract_class_methods(self, parser):
        code = '''
class Calculator {
public:
    int add(int a, int b) {
        return a + b;
    }

    int subtract(int a, int b) {
        return a - b;
    }
};
'''
        result = parser.parse(code)

        assert len(result.functions) == 2
        add_func = next(f for f in result.functions if f.name == "add")
        sub_func = next(f for f in result.functions if f.name == "subtract")

        assert add_func.qualified_name == "Calculator::add"
        assert add_func.is_method is True
        assert add_func.parent == "Calculator"
        assert add_func.decorators == ["public"]
        assert (add_func.start_line, add_func.end_line) == (4, 6)  # the lines of the method, not of the class

        assert sub_func.qualified_name == "Calculator::subtract"
        assert (sub_func.start_line, sub_func.end_line) == (8, 10)

    def test_extract_constructor_destructor(self, parser):
        code = '''
class Person {
public:
    Person(const std::string& name) {
        this->name = name;
    }

    ~Person() {
    }
};
'''
        result = parser.parse(code)

        assert [(f.name, f.qualified_name, f.start_line, f.end_line) for f in result.functions] == [
            ("Person", "Person::Person", 4, 6),
            ("~Person", "Person::~Person", 8, 9),
        ]
        assert result.functions[0].parameter_tokens == ["const std::string&"]

    def test_extract_private_methods(self, parser):
        code = '''
class Helper {
private:
    void internal() {
        return;
    }

public:
    void external() {
        internal();
    }
};
'''
        result = parser.parse(code)

        assert len(result.functions) == 2
        internal = next(f for f in result.functions if f.name == "internal")
        external = next(f for f in result.functions if f.name == "external")

        assert internal.decorators == ["private"]
        assert external.decorators == ["public"]

    def test_extract_static_method(self, parser):
        code = '''
class MathUtils {
public:
    static int add(int a, int b) {
        return a + b;
    }
};
'''
        result = parser.parse(code)

        assert len(result.functions) == 1
        func = result.functions[0]
        assert func.name == "add"
        assert "static" in func.decorators
        assert func.signature == "static int add(int a, int b) {"

    def test_extract_virtual_method(self, parser):
        code = '''
class Base {
public:
    virtual void process() {
        return;
    }
};
'''
        result = parser.parse(code)

        assert len(result.functions) == 1
        func = result.functions[0]
        assert "virtual" in func.decorators
        assert func.signature == "virtual void process() {"

    def test_extract_const_method(self, parser):
        code = '''
class Container {
public:
    int size() const {
        return 0;
    }
};
'''
        result = parser.parse(code)

        assert len(result.functions) == 1
        func = result.functions[0]
        assert func.signature == "int size() const {"


class TestOutOfClassDefinitions:
    def test_extract_out_of_class_method(self, parser):
        code = '''
void Calculator::add(int a, int b) {
    return a + b;
}
'''
        result = parser.parse(code)

        assert len(result.functions) == 1
        func = result.functions[0]
        assert func.name == "add"
        assert func.qualified_name == "Calculator::add"
        assert func.parent == "Calculator"
        assert func.is_method is True
        assert func.signature == "void Calculator::add(int a, int b) {"

    def test_extract_namespaced_out_of_class_method(self, parser):
        code = '''
void ns::Calculator::add(int a, int b) {
    return a + b;
}
'''
        result = parser.parse(code)

        assert len(result.functions) == 1
        func = result.functions[0]
        assert (func.name, func.qualified_name, func.parent) == ("add", "ns::Calculator::add", "ns::Calculator")
        assert func.is_method is True


class TestTemplates:
    def test_extract_template_function(self, parser):
        code = '''
template<typename T>
T max(T a, T b) {
    return a > b ? a : b;
}
'''
        result = parser.parse(code)

        assert len(result.functions) == 1
        func = result.functions[0]
        assert func.name == "max"
        assert func.decorators == ["template"]
        assert (func.start_line, func.end_line) == (3, 5)  # the line of the function, not of its `template<...>` line
        assert func.parameter_tokens == ["T", "T"]

    def test_extract_template_class_method(self, parser):
        code = '''
template<typename T>
class Container {
public:
    void push(T value) {
        data = value;
    }
};
'''
        result = parser.parse(code)

        assert len(result.functions) == 1
        func = result.functions[0]
        assert (func.name, func.qualified_name) == ("push", "Container::push")
        assert func.is_method is True
        assert (func.start_line, func.end_line) == (5, 7)


class TestHeaderFiles:
    def test_extract_declarations_from_header(self, parser):
        code = '''
void foo();
int bar(int x);
'''
        # the .h extension is what signals header (declaration-only) parsing
        result = parser.parse(code, file_path="test.h")

        assert [(f.name, f.signature, f.start_line, f.end_line) for f in result.functions] == [
            ("foo", "void foo();", 2, 2),
            ("bar", "int bar(int x);", 3, 3),
        ]
        for func in result.functions:
            assert "declaration" in func.decorators


    def test_extract_class_declarations_from_header(self, parser):
        code = '''
class Calculator {
public:
    int add(int a, int b);
    int subtract(int a, int b);
};
'''
        result = parser.parse(code, file_path="calculator.hpp")

        assert [(f.qualified_name, f.start_line) for f in result.functions] == [("Calculator::add", 4), ("Calculator::subtract", 5)]
        for func in result.functions:
            assert func.decorators == ["public", "declaration"]
            assert func.is_method is True


class TestStructs:
    def test_extract_struct_methods(self, parser):
        code = '''
struct Point {
    int x, y;

    void move(int dx, int dy) {
        x += dx;
        y += dy;
    }
};
'''
        result = parser.parse(code)

        assert len(result.functions) == 1
        func = result.functions[0]
        assert func.name == "move"
        assert func.qualified_name == "Point::move"
        assert func.is_method is True
        assert (func.start_line, func.end_line) == (5, 8)


class TestSyntaxErrors:
    def test_reports_syntax_error(self, parser):
        code = '''
void broken( {
    return;
}
'''
        result = parser.parse(code)

        assert result.has_errors is True
        assert [(e.line, e.column) for e in result.errors] == [(2, 0)]

    def test_extracts_functions_despite_errors(self, parser):
        code = '''
void valid() {
    return;
}

void broken(

void another() {
    return;
}
'''
        result = parser.parse(code)

        assert result.has_errors is True
        assert [(e.line, e.column) for e in result.errors] == [(8, 14)]  # where the text stops making sense
        valid = [f for f in result.functions if f.name == "valid"]
        assert len(valid) == 1 and (valid[0].start_line, valid[0].end_line) == (2, 4)  # the function before the break is whole


class TestParseFile:
    def test_parse_cpp_file(self, parser, tmp_path):
        code = '''
void fileFunction() {
    return;
}
'''
        path = tmp_path / "source.cpp"
        path.write_text(code)
        temp_path = str(path)

        result = parser.parse_file(temp_path)

        assert len(result.functions) == 1
        assert result.file_path == temp_path
        func = result.functions[0]
        assert func.name == "fileFunction"
        assert (func.start_line, func.end_line) == (2, 4)

    def test_parse_header_file(self, parser, tmp_path):
        code = '''
void headerFunction();
'''
        path = tmp_path / "source.h"
        path.write_text(code)
        temp_path = str(path)

        result = parser.parse_file(temp_path)

        assert len(result.functions) == 1
        func = result.functions[0]
        assert func.name == "headerFunction"
        assert func.decorators == ["declaration"]


class TestEdgeCases:
    def test_empty_source(self, parser):
        result = parser.parse("")

        assert result.functions == []
        assert result.errors == [] and result.has_errors is False

    def test_no_functions(self, parser):
        code = '''
int x = 1;
int y = 2;
'''
        result = parser.parse(code)

        assert result.functions == []
        assert result.has_errors is False

    def test_multiple_functions(self, parser):
        code = '''
void foo() {}
void bar() {}
void baz() {}
'''
        result = parser.parse(code)

        assert [(f.name, f.start_line) for f in result.functions] == [("foo", 2), ("bar", 3), ("baz", 4)]  # in the order of the file

    def test_operator_overload(self, parser):
        code = '''
class Vector {
public:
    Vector operator+(const Vector& other) {
        return Vector();
    }
};
'''
        result = parser.parse(code)

        assert len(result.functions) == 1
        func = result.functions[0]
        assert (func.name, func.qualified_name) == ("operator+", "Vector::operator+")
        assert func.parameter_tokens == ["const Vector&"]


class TestCallExtraction:
    def test_bare_call(self, parser):
        code = "void run() {\n  helper();\n}"
        result = parser.parse(code)
        calls = result.functions[0].calls

        assert [(c.receiver, c.name) for c in calls] == [(None, "helper")]

    def test_explicit_this_arrow_call(self, parser):
        code = "class Widget {\npublic:\n  void run() {\n    this->helper();\n  }\n};"
        result = parser.parse(code)
        run_func = next(f for f in result.functions if f.name == "run")

        assert [(c.receiver, c.name) for c in run_func.calls] == [("this", "helper")]

    def test_qualified_call(self, parser):
        code = "void run() {\n  Base::helper();\n}"
        result = parser.parse(code)
        calls = result.functions[0].calls

        assert [(c.receiver, c.name) for c in calls] == [("Base", "helper")]

    def test_calls_in_a_lambda_are_the_enclosing_functions(self, parser):
        # A lambda has no identity of its own: what it calls is called by the function it is written in.
        code = (
            "void outer() {\n"
            "  auto inner = []() {\n"
            "    inner_call();\n"
            "  };\n"
            "  outer_call();\n"
            "}"
        )
        result = parser.parse(code)
        outer_func = next(f for f in result.functions if f.name == "outer")

        assert [c.name for c in outer_func.calls] == ["inner_call", "outer_call"]  # both are the enclosing function's, in the order written
        assert len(result.functions) == 1  # the lambda is not a function of its own

    def test_calls_in_a_nested_function_definition_are_not_the_outer_functions(self, parser):
        code = (
            "struct Local {\n"
            "  void method() { method_call(); }\n"
            "};\n"
            "void outer() {\n"
            "  outer_call();\n"
            "}"
        )
        result = parser.parse(code)
        outer_func = next(f for f in result.functions if f.name == "outer")
        assert [c.name for c in outer_func.calls] == ["outer_call"]
        method = next(f for f in result.functions if f.name == "method")
        assert (method.qualified_name, [c.name for c in method.calls]) == ("Local::method", ["method_call"])  # its call is its own, and is not lost


class TestImportExtraction:
    """`#include` and `using namespace X;` are out of scope for import
    tracking — neither binds one specific name the way `using X::Y;` does."""

    def test_using_declaration(self, parser):
        result = parser.parse("using std::vector;")

        assert [(i.module_path, i.name, i.alias) for i in result.name_imports] == [("std", "vector", None)]

    def test_using_namespace_is_skipped(self, parser):
        result = parser.parse("using namespace std;")

        assert result.name_imports == []

    def test_include_is_not_tracked(self, parser):
        result = parser.parse('#include "foo.h"\nvoid run() {}')

        assert result.name_imports == []
