"""Test macros that C++ test frameworks use to define a test case.

`TEST(CalcSuite, Multiplies) { ... }` is a macro call followed by a body. To the
grammar it is a function called `TEST`, which would give every test in a file
the same name. Here the macros that really name a test are listed, with how to
read their arguments; the list is in this one place and is extended by adding a
line.
"""

from __future__ import annotations

import re
from typing import Dict, List, Optional, Tuple

#: Macro name -> number of leading arguments that name the test. Two arguments are
#: `(suite, test)`, one is `(test)`.
TEST_MACROS: Dict[str, int] = {
    "TEST": 2,
    "TEST_F": 2,
    "TEST_P": 2,
    "TYPED_TEST": 2,
    "TYPED_TEST_P": 2,
    "BOOST_AUTO_TEST_CASE": 1,
    "BOOST_FIXTURE_TEST_CASE": 1,
    "BOOST_AUTO_TEST_CASE_TEMPLATE": 1,
}

_IDENTIFIER = re.compile(r"^[A-Za-z_]\w*$")


def macro_test_case(macro: str, arguments: List[str]) -> Optional[Tuple[Optional[str], str]]:
    """`(suite, test)` for a call of a known test macro with identifier arguments, else `None`."""
    count = TEST_MACROS.get(macro)
    if count is None or len(arguments) < count or not all(_IDENTIFIER.match(arg) for arg in arguments[:count]):
        return None
    if count == 1:
        return None, arguments[0]
    return arguments[0], arguments[1]
