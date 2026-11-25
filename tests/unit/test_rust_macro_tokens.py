"""The calls in a macro are read from its tokens when it is a plain expression, and from a parse of its text otherwise.
Either way the calls are the same, in the same order and with the same text."""

from __future__ import annotations

import pytest

from drspec.parsers.rust_parser import RustParser

PLAIN = [
    "assert_eq!(a.len(), 1);",
    'println!("{} {}", helper(x), other::path(y, z));',
    "let v = vec![make(); 3];",
    "assert!(self.ready(), \"not {}\", self.name());",
    "let s = format!(\"{}\", Foo::new(1).value());",
    "write!(out, \"{}\", fmt_all(&mut buf, &list[0]))?;",
    "assert_eq!(compute(a)(b), -f(c) as f32 + g(!d));",
    "dbg!(Foo { a: first(1), b: second(2) });",
    "debug_assert!(inner!(deep(1)));",
    "my_macro!(a::b::c(1), self.m(2), x.y.z(3));",
    "assert!(x.get(0).is_some() && y.get(1).map(|q| h(q)).unwrap_or(false));",
    "let _ = (a.f(), (b.g(), h()));",
    "nest!(one!(two!(three!(four(1)))));",
]
# (the text inside the macro: each is written as `wrap!(...)`)
NOT_PLAIN = [
    "let n = collect::<Vec<_>>(items);",  # a turbofish
    "matches(x, Foo::Bar(y) if check(y))",  # a keyword
    "match v { Some(x) => handle(x), None => other() }",  # `=>`
    '{ "key": compute(1), "other": go(2) }',  # a map with string keys
    "Node Children [ @button() Node { width: 1 } ]",  # two operands side by side
    "let x = |a: i32| -> i32 { run(a) };",
    "if ready() { go(); } else { stop(); }",
    "for i in 0..n { step(i); }",
    "<T as Trait>::make(1)",
    "unsafe { raw(1) }",
    "{ label: compute(2) }",  # a block, not a struct literal
    'target: "t", "{}", fetch(1)',  # a label before the arguments
    "read(x), Some(_)",  # `_`
]
PLAIN.append("assert_eq!(0.max(wide(1)), 3);")


def _parse(parser: RustParser, body: str):
    return parser.parse("fn host(self_value: u8) {\n    " + body + "\n}\n").functions[0].calls


def _wrapped(body: str) -> str:
    return f"wrap!({body});"


def _calls(parser: RustParser, body: str) -> list:
    return [(c.receiver, c.name, c.text, c.kind) for c in _parse(parser, body)]


@pytest.mark.parametrize("body", PLAIN + [_wrapped(text) for text in NOT_PLAIN])
def test_the_tokens_and_the_parse_give_the_same_calls(body):
    fast, slow = RustParser(), RustParser()
    slow._fast_macro_calls = lambda tokens, depth: None
    assert _calls(fast, body) == _calls(slow, body)


@pytest.mark.parametrize("body", PLAIN)
def test_a_plain_macro_is_not_parsed_again(body):
    parser = RustParser()
    real = parser._parser.parse
    parses = []
    parser._parser = type("Counting", (), {"parse": lambda self, data, *a, **k: (parses.append(1), real(data, *a, **k))[1]})()
    _parse(parser, body)
    assert len(parses) == 1  # the file itself


@pytest.mark.parametrize("body", NOT_PLAIN)
def test_a_macro_that_is_not_plain_is_still_parsed(body):
    body = _wrapped(body)
    parser = RustParser()
    assert parser._fast_macro_calls is not None
    seen = []
    original = parser._fast_macro_calls
    parser._fast_macro_calls = lambda tokens, depth: seen.append(original(tokens, depth)) or seen[-1]
    _parse(parser, body)
    assert seen and seen[-1] is None  # the tokens are not trusted for it: its text is parsed


def test_the_calls_come_with_their_own_text_and_kind():
    calls = _calls(RustParser(), "assert_eq!(a.len(), foo::bar(1, 2));")
    assert calls == [("a", "len", "a.len()", "method"), ("foo", "bar", "foo::bar(1, 2)", "path")]


def test_a_nested_macro_is_a_call_unless_it_is_a_standard_one_and_stops_at_the_depth_limit():
    calls = _calls(RustParser(), "outer!(inner!(deep!(deeper!(too_far(1)))));")
    assert [c[1] for c in calls] == ["outer!", "inner!", "deep!", "deeper!"]
    calls = _calls(RustParser(), "outer!(vec![mine(1)], println!(\"{}\", yours(2)));")
    assert [c[1] for c in calls] == ["outer!", "mine", "yours"]


def test_a_method_of_a_field_or_of_a_result_has_no_receiver_name():
    calls = _calls(RustParser(), "check!(a.b.c(1), make().d(2), e[0].f(3), g?.h(4));")
    assert [(c[0], c[1]) for c in calls if c[3] != "macro"] == [(None, "make")]
