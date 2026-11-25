"""Members of a class declared inside another class: its methods and constructors are found, with or without an
initializer list, in a header or a source file, and no definition is reported as missed. The tree wraps a nested class
as a field declaration, which the class-member walk used to read as a plain field and never looked inside."""

from __future__ import annotations

import pytest

from drspec.parsers.cpp_parser import CppParser


def _parse(text: str):
    return CppParser().parse(text, "nested.hpp")


def _qualified(text: str) -> list:
    return sorted(f.qualified_name for f in _parse(text).functions)


class TestNestedConstructors:
    def test_a_nested_constructor_with_an_initializer_list_is_found(self):
        result = _parse("struct O { struct S { int a; S(int x) : a(x) {} }; };")
        assert [f.qualified_name for f in result.functions] == ["O::S::S"]
        assert result.errors == []

    def test_the_same_constructor_at_the_top_level_is_found_the_same_way(self):
        assert _qualified("struct S { int a; S(int x) : a(x) {} };") == ["S::S"]

    @pytest.mark.parametrize(
        "source",
        [
            "struct O { struct S { int a; S(int x) : a(x) {} }; };",
            "struct O { int q; struct S { int a; S(const O& o) : a(o.q) {} }; };",
            "struct O { int q; struct S { const O& o; S(const O& x) : o(x) {} }; };",
            "struct O { int q; struct S { const O& o; explicit S(const O& x) : o(x) {} }; };",
            "class O { public: struct S { int a; S(int x) : a(x) {} }; };",
            "namespace n { struct O { struct S { int a; S(int x) : a(x) {} }; }; }",
        ],
    )
    def test_a_nested_constructor_is_found_in_each_context(self, source):
        result = _parse(source)
        assert result.errors == []
        assert [f.name for f in result.functions if f.name == "S"] == ["S"]

    def test_a_multi_line_initializer_list_has_no_fake_function(self):
        source = (
            "struct O { int q; int r;\n"
            "  struct S {\n"
            "    int a; int b;\n"
            "    S(const O& o)\n"
            "      : a(o.q),\n"
            "        b(o.r)\n"
            "    {\n"
            "    }\n"
            "  };\n"
            "};\n"
        )
        result = _parse(source)
        assert [f.qualified_name for f in result.functions] == ["O::S::S"]
        assert result.errors == []

    @pytest.mark.parametrize("count", [3, 12, 40])
    def test_a_constructor_with_many_initializers_is_found(self, count):
        initializers = ", ".join(f"m{i}(x)" for i in range(count))
        fields = " ".join(f"int m{i};" for i in range(count))
        source = f"struct O {{ struct S {{ {fields} S(int x) : {initializers} {{}} }}; }};\n"
        result = _parse(source)
        assert result.errors == []
        assert [f.qualified_name for f in result.functions] == ["O::S::S"]


class TestNestedMethods:
    def test_a_nested_method_is_found_with_its_class(self):
        assert _qualified("struct O { struct S { int a; int get() { return a; } }; };") == ["O::S::get"]

    def test_a_method_of_the_outer_class_is_still_found_beside_the_nested_one(self):
        source = "struct O { struct S { int a; int get() { return a; } } s; int m() { return 1; } };"
        assert _qualified(source) == ["O::S::get", "O::m"]

    def test_a_deeply_nested_constructor_is_found_with_all_its_outer_classes(self):
        source = "struct A { struct B { struct C { C(int x) : v(x) {} int v; }; }; };"
        result = _parse(source)
        assert [f.qualified_name for f in result.functions] == ["A::B::C::C"]
        assert result.errors == []

    def test_a_nested_class_that_declares_a_member_variable_adds_no_function(self):
        source = "struct O { struct S { int a; const int* p; }; int value; };"
        assert _parse(source).functions == []
        assert _parse(source).errors == []

    def test_an_unnamed_nested_struct_does_not_stop_the_members_after_it(self):
        source = "struct O { struct { int a; } field; int m() { return 2; } };"
        assert _qualified(source) == ["O::m"]
