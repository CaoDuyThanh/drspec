"""Definitions that used to be lost: a function inside a decorated method, the same function defined in both branches of an
`if` or `try`, and the same trait method in several `impl` blocks of one type. Each is a function with an identifier of
its own, and the identifier a single definition always had is the one that stays."""

from __future__ import annotations

from pathlib import Path

import pytest

from drspec.core.scanner import Scanner

pytestmark = pytest.mark.unit  # small source files in tmp_path, read by the real parsers


@pytest.fixture
def root(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    return tmp_path


def _scan(root: Path, name: str, text: str):
    (root / name).write_text(text)
    return Scanner().scan_file(name, relative_to=root)


def _ids(result) -> dict:
    return {f.function_id: f for f in result.functions}


def _names(result) -> list:
    return sorted(f.qualified_name for f in result.functions)


NESTED = '''class T:
    @mock.patch("time.time")
    def t1(self, m):
        def a_view(request):
            return 1
        return a_view

    @mock.patch("time.time")
    def t2(self, m):
        @decorated
        def a_view(request):
            return 1
        return a_view

    def t4(self, m):
        def a_view(request):
            return 1
        return a_view


@outer
def free():
    def inner():
        return 2
    return inner
'''


class TestAFunctionInsideADecoratedMethod:
    def test_it_is_indexed_like_one_inside_a_plain_method(self, root):
        result = _scan(root, "t.py", NESTED)
        assert _names(result) == ["T.t1", "T.t1.a_view", "T.t2", "T.t2.a_view", "T.t4", "T.t4.a_view", "free", "free.inner"]

    def test_a_decorated_function_inside_a_decorated_method_keeps_its_decorator(self, root):
        result = _scan(root, "t.py", NESTED)
        inner = next(f for f in result.functions if f.qualified_name == "T.t2.a_view")
        assert inner.decorators == ["decorated"]
        assert {f.qualified_name: f.decorators for f in result.functions if f.qualified_name in ("T.t1", "T.t1.a_view")} == {
            "T.t1": ['mock.patch("time.time")'], "T.t1.a_view": [],
        }  # the decorator of the method stays on the method and is not handed to the function inside it


class TestTheSameDefinitionTwiceInOneScope:
    TRY = "try:\n    import fast\n\n    def pick(x):\n        return fast.go(x)\nexcept ImportError:\n\n    def pick(x):\n        return x\n"

    def test_both_branches_are_functions(self, root):
        result = _scan(root, "d.py", self.TRY)
        picks = [f for f in result.functions if f.name == "pick"]
        assert sorted(f.start_line for f in picks) == [4, 8]
        assert len({f.function_id for f in picks}) == 2
        assert all(f.function_id.startswith("d.py::pick#") for f in picks)

    def test_the_last_keeps_the_identifier_a_single_definition_has(self, root):
        single = _scan(root, "s.py", "def pick(x):\n    return x\n")
        both = _scan(root, "d.py", self.TRY)
        single_hash = next(iter(_ids(single))).split("#")[1]
        last = max((f for f in both.functions if f.name == "pick"), key=lambda f: f.start_line)
        assert last.function_id == f"d.py::pick#{single_hash}"

    def test_identifiers_do_not_move_when_unrelated_code_is_added(self, root):
        before = {f.start_line: f.function_id for f in _scan(root, "d.py", self.TRY).functions}
        after = {f.function_id for f in _scan(root, "d.py", "def added():\n    return 0\n\n\n" + self.TRY + "\n\ndef another():\n    return 1\n").functions if f.name == "pick"}
        assert len(before) == 2 and after == set(before.values())

    def test_three_definitions_are_three_functions(self, root):
        result = _scan(root, "t.py", "def f(a):\n    return 1\n\n\ndef f(a):\n    return 2\n\n\ndef f(a):\n    return 3\n")
        single = next(iter(_ids(_scan(root, "s.py", "def f(a):\n    return 1\n")))).split("#")[1]
        by_line = {f.start_line: f.function_id for f in result.functions}
        assert sorted(by_line) == [1, 5, 9] and len(set(by_line.values())) == 3
        assert by_line[9] == f"t.py::f#{single}"  # the last keeps the identifier of a single definition; the two before it are renumbered

    def test_definitions_that_differ_in_their_parameters_are_as_they_were(self, root):
        result = _scan(root, "t.py", "def f(a):\n    return 1\n\n\ndef f(a, b):\n    return 2\n")
        one = next(iter(_ids(_scan(root, "s1.py", "def f(a):\n    return 1\n")))).split("#")[1]
        two = next(iter(_ids(_scan(root, "s2.py", "def f(a, b):\n    return 2\n")))).split("#")[1]
        # each keeps the identifier it has alone: different parameters were never "the same definition twice"
        assert {f.start_line: f.function_id for f in result.functions} == {1: f"t.py::f#{one}", 5: f"t.py::f#{two}"}

    def test_a_property_and_its_setter_are_still_two(self, root):
        result = _scan(root, "t.py", "class K:\n    @property\n    def v(self):\n        return 1\n\n    @v.setter\n    def v(self, value):\n        pass\n")
        alone = next(iter(_ids(_scan(root, "a.py", "class K:\n    @property\n    def v(self):\n        return 1\n")))).split("#")[1]
        assert [(f.name, f.start_line) for f in result.functions] == [("v", 2), ("v", 6)] and len({f.function_id for f in result.functions}) == 2
        assert result.functions[0].function_id == f"t.py::K.v#{alone}"  # the property keeps the identifier it has without its setter


IMPLS = '''struct A;
impl IntoIterator for &A { type Item = u32; fn into_iter(self) -> u32 { 1 } }
impl IntoIterator for &mut A { type Item = u32; fn into_iter(self) -> u32 { 2 } }
impl IntoIterator for A { type Item = u32; fn into_iter(self) -> u32 { 3 } }
impl AsRef<[u32]> for A { fn as_ref(&self) -> &[u32] { &[] } }
impl AsRef<Vec<u32>> for A { fn as_ref(&self) -> &Vec<u32> { todo!() } }
struct W<T>(T);
trait Tr { fn go(&self) -> u32; }
impl<T> Tr for W<Vec<T>> { fn go(&self) -> u32 { 1 } }
impl Tr for W<u32> { fn go(&self) -> u32 { 2 } }
impl PartialEq<u32> for A { fn eq(&self, o: &u32) -> bool { true } }
impl PartialEq<bool> for A { fn eq(&self, o: &bool) -> bool { true } }
'''


class TestTheSameMethodInSeveralImplBlocks:
    def test_each_impl_gives_a_function(self, root):
        result = _scan(root, "r.rs", IMPLS)
        names = [f.name for f in result.functions]
        assert names.count("into_iter") == 3 and names.count("as_ref") == 2 and names.count("go") == 2 and names.count("eq") == 2
        assert len({f.function_id for f in result.functions}) == len(result.functions) == 9
        assert sorted(f.discriminator for f in result.functions) == sorted([  # the header of the impl is what tells them apart
            "IntoIterator for &A", "IntoIterator for &mut A", "IntoIterator for A", "AsRef<[u32]> for A", "AsRef<Vec<u32>> for A",
            "Tr for W<Vec<T>>", "Tr for W<u32>", "PartialEq<u32> for A", "PartialEq<bool> for A",
        ])

    def test_the_first_keeps_the_identifier_it_had(self, root):
        single = _scan(root, "s.rs", "struct A;\nimpl IntoIterator for &A { type Item = u32; fn into_iter(self) -> u32 { 1 } }\n")
        many = _scan(root, "r.rs", IMPLS)
        single_hash = single.functions[0].function_id.split("#")[1]
        first = min((f for f in many.functions if f.name == "into_iter"), key=lambda f: f.start_line)
        assert first.function_id == f"r.rs::A::<IntoIterator>::into_iter#{single_hash}"

    def test_the_header_and_not_the_position_tells_them_apart(self, root):
        before = {f.discriminator: f.function_id for f in _scan(root, "r.rs", IMPLS).functions if f.name == "into_iter"}
        reordered = IMPLS.replace(
            "impl IntoIterator for &mut A { type Item = u32; fn into_iter(self) -> u32 { 2 } }\nimpl IntoIterator for A { type Item = u32; fn into_iter(self) -> u32 { 3 } }\n", ""
        ) + "impl IntoIterator for A { type Item = u32; fn into_iter(self) -> u32 { 3 } }\nimpl IntoIterator for &mut A { type Item = u32; fn into_iter(self) -> u32 { 2 } }\n"
        after = {f.discriminator: f.function_id for f in _scan(root, "r.rs", reordered).functions if f.name == "into_iter"}
        assert before["IntoIterator for &mut A"] == after["IntoIterator for &mut A"] and before["IntoIterator for A"] == after["IntoIterator for A"]

    def test_cfg_variants_in_one_impl_are_still_one_entry(self, root):
        text = "struct A;\nimpl A {\n    #[cfg(debug)]\n    fn check(&self) -> bool { true }\n    #[cfg(not(debug))]\n    fn check(&self) -> bool { false }\n}\n"
        assert [(f.name, f.qualified_name) for f in _scan(root, "c.rs", text).functions] == [("check", "A::check")]

    def test_cfg_variants_of_a_free_function_are_still_one_entry(self, root):
        text = "#[cfg(unix)]\nfn go() -> u32 { 1 }\n#[cfg(not(unix))]\nfn go() -> u32 { 2 }\n"
        assert [(f.name, f.qualified_name) for f in _scan(root, "c.rs", text).functions] == [("go", "go")]


class TestAnImplInsideAFunction:
    def test_its_methods_are_indexed(self, root):
        text = "fn main() {\n    struct Z;\n    trait Damage { fn hit(&mut self); }\n    impl Damage for Z { fn hit(&mut self) { } }\n}\n"
        result = _scan(root, "m.rs", text)
        assert sorted(f.name for f in result.functions) == ["hit", "main"]

    def test_two_functions_with_a_local_type_of_the_same_name_are_both_indexed(self, root):
        text = (
            "fn one() {\n    struct S;\n    impl Drop for S { fn drop(&mut self) { } }\n}\n\n"
            "fn two() {\n    struct S;\n    impl Drop for S { fn drop(&mut self) { } }\n}\n"
        )
        result = _scan(root, "m.rs", text)
        drops = sorted((f for f in result.functions if f.name == "drop"), key=lambda f: f.start_line)
        assert [f.start_line for f in drops] == [3, 8] and len({f.function_id for f in drops}) == 2
        assert drops[0].function_id == "m.rs::S::<Drop>::drop#d9e613a7"  # the first keeps the identifier it has alone


class TestAPartialFunctionKeepsItsRenumberedIdentifier:
    def test_the_partial_list_names_the_identifier_the_function_ended_up_with(self, root):
        text = (
            "def pick(x):\n    return 1\n\n\n"
            "def pick(x):\n    return 2 +* 3\n    z = 1\n"  # damaged expression, the head and the rest are whole
        )
        result = _scan(root, "p.py", text)
        by_line = {f.start_line: f.function_id for f in result.functions}
        single = next(iter(_ids(_scan(root, "s.py", "def pick(x):\n    return 1\n")))).split("#")[1]
        # the first definition was renumbered; the damaged one is the last and keeps the identifier a single definition has
        assert by_line[5] == f"p.py::pick#{single}" and by_line[1] != by_line[5]
        assert result.partial_functions == [{"file": "p.py", "name": "pick", "line": 5, "function_id": by_line[5]}]  # not an empty list that passes `all`


class TestMethodsUnderAStatementInAClassBody:
    CLASS = (
        "class Ops:\n    if FLAG:\n        def last(self):\n            return 1\n    else:\n        def last(self):\n            return 2\n\n"
        "    try:\n        import fast\n\n        def go(self):\n            return 3\n    except ImportError:\n        def go(self):\n            return 4\n\n"
        "    with ctx():\n        def inside(self):\n            return 5\n\n    def plain(self):\n        return 6\n"
    )

    def test_they_belong_to_the_class(self, root):
        result = _scan(root, "c.py", self.CLASS)
        assert sorted(f.qualified_name for f in result.functions) == ["Ops.go", "Ops.go", "Ops.inside", "Ops.last", "Ops.last", "Ops.plain"]
        assert all(f.parent == "Ops" and f.is_method for f in result.functions)

    def test_the_two_branches_have_identifiers_of_their_own(self, root):
        result = _scan(root, "c.py", self.CLASS)
        assert len({f.function_id for f in result.functions}) == 6

    def test_a_decorated_method_under_a_condition_is_found_with_its_nested_function(self, root):
        text = "class K:\n    if FLAG:\n        @cached\n        def m(self):\n            def inner():\n                return 1\n            return inner\n"
        assert sorted(f.qualified_name for f in _scan(root, "k.py", text).functions) == ["K.m", "K.m.inner"]
