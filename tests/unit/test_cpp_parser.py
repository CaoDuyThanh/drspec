"""Tests for the C++ parser."""

import tempfile
from pathlib import Path

import pytest

from drspec.parsers.cpp_parser import CppParser
from drspec.parsers.models import ExtractedFunction, ParseResult


@pytest.fixture
def parser():
    """Create a C++ parser instance."""
    return CppParser()


class TestFreeFunctions:
    """Tests for free function extraction."""

    def test_extract_simple_function(self, parser):
        """Test extracting a simple function."""
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
        assert "void hello()" in func.signature
        assert func.is_method is False

    def test_extract_function_with_params(self, parser):
        """Test extracting function with parameters."""
        code = '''
int add(int a, int b) {
    return a + b;
}
'''
        result = parser.parse(code)

        assert len(result.functions) == 1
        func = result.functions[0]
        assert func.name == "add"
        assert "int a, int b" in func.signature

    def test_extract_function_with_return_type(self, parser):
        """Test extracting function with complex return type."""
        code = '''
std::vector<int> getNumbers() {
    return {};
}
'''
        result = parser.parse(code)

        assert len(result.functions) == 1
        func = result.functions[0]
        assert func.name == "getNumbers"
        assert "std::vector<int>" in func.signature


class TestNamespaces:
    """Tests for namespace handling."""

    def test_extract_namespaced_function(self, parser):
        """Test extracting function in namespace."""
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

    def test_extract_nested_namespace(self, parser):
        """Test extracting function in nested namespace."""
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
        """Test extracting multiple functions in namespace."""
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

        assert len(result.functions) == 2
        names = {f.qualified_name for f in result.functions}
        assert names == {"math::add", "math::subtract"}


class TestClassMethods:
    """Tests for class method extraction."""

    def test_extract_class_methods(self, parser):
        """Test extracting methods from a class."""
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
        assert "public" in add_func.decorators

        assert sub_func.qualified_name == "Calculator::subtract"

    def test_extract_constructor_destructor(self, parser):
        """Test extracting constructor and destructor."""
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

        assert len(result.functions) == 2
        names = {f.name for f in result.functions}
        assert "Person" in names
        assert "~Person" in names

    def test_extract_private_methods(self, parser):
        """Test extracting private methods."""
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

        assert "private" in internal.decorators
        assert "public" in external.decorators

    def test_extract_static_method(self, parser):
        """Test extracting static class method."""
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

    def test_extract_virtual_method(self, parser):
        """Test extracting virtual method."""
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

    def test_extract_const_method(self, parser):
        """Test extracting const method."""
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
        assert "const" in func.signature


class TestOutOfClassDefinitions:
    """Tests for out-of-class method definitions."""

    def test_extract_out_of_class_method(self, parser):
        """Test extracting method defined outside class."""
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

    def test_extract_namespaced_out_of_class_method(self, parser):
        """Test extracting method with namespace prefix."""
        code = '''
void ns::Calculator::add(int a, int b) {
    return a + b;
}
'''
        result = parser.parse(code)

        assert len(result.functions) == 1
        func = result.functions[0]
        # The qualified name should include all parts
        assert "Calculator" in func.qualified_name
        assert "add" in func.qualified_name


class TestTemplates:
    """Tests for template function extraction."""

    def test_extract_template_function(self, parser):
        """Test extracting template function."""
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
        assert "template" in func.decorators

    def test_extract_template_class_method(self, parser):
        """Test extracting method from template class."""
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
        assert func.name == "push"
        assert func.is_method is True


class TestHeaderFiles:
    """Tests for header file handling."""

    def test_extract_declarations_from_header(self, parser):
        """Test extracting declarations from header file."""
        code = '''
void foo();
int bar(int x);
'''
        # Simulate header file
        result = parser.parse(code, file_path="test.h")

        assert len(result.functions) == 2
        for func in result.functions:
            assert "declaration" in func.decorators

    def test_extract_class_declarations_from_header(self, parser):
        """Test extracting class method declarations from header."""
        code = '''
class Calculator {
public:
    int add(int a, int b);
    int subtract(int a, int b);
};
'''
        result = parser.parse(code, file_path="calculator.hpp")

        assert len(result.functions) == 2
        for func in result.functions:
            assert "declaration" in func.decorators
            assert func.is_method is True


class TestStructs:
    """Tests for struct handling (similar to classes)."""

    def test_extract_struct_methods(self, parser):
        """Test extracting methods from a struct."""
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


class TestSyntaxErrors:
    """Tests for syntax error handling."""

    def test_reports_syntax_error(self, parser):
        """Test that syntax errors are reported."""
        code = '''
void broken( {
    return;
}
'''
        result = parser.parse(code)

        assert result.has_errors is True
        assert len(result.errors) > 0

    def test_extracts_functions_despite_errors(self, parser):
        """Test that valid functions are extracted even with errors."""
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
        valid_names = {f.name for f in result.functions}
        assert "valid" in valid_names


class TestParseFile:
    """Tests for file parsing."""

    def test_parse_cpp_file(self, parser):
        """Test parsing a .cpp file from disk."""
        code = '''
void fileFunction() {
    return;
}
'''
        with tempfile.NamedTemporaryFile(
            mode="w", suffix=".cpp", delete=False
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

    def test_parse_header_file(self, parser):
        """Test parsing a .h file from disk."""
        code = '''
void headerFunction();
'''
        with tempfile.NamedTemporaryFile(
            mode="w", suffix=".h", delete=False
        ) as f:
            f.write(code)
            f.flush()
            temp_path = f.name

        try:
            result = parser.parse_file(temp_path)

            assert len(result.functions) == 1
            func = result.functions[0]
            assert func.name == "headerFunction"
            assert "declaration" in func.decorators
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
int x = 1;
int y = 2;
'''
        result = parser.parse(code)

        assert len(result.functions) == 0

    def test_multiple_functions(self, parser):
        """Test extracting multiple functions."""
        code = '''
void foo() {}
void bar() {}
void baz() {}
'''
        result = parser.parse(code)

        assert len(result.functions) == 3
        names = {f.name for f in result.functions}
        assert names == {"foo", "bar", "baz"}

    def test_operator_overload(self, parser):
        """Test extracting operator overload."""
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
        assert "operator" in func.name
