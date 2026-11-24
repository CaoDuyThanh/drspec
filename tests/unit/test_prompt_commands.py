"""Every `drspec ...` command the prompts and docs tell a reader to run must parse.

The agent prompts, the README and the project skill are the only instructions
an agent has for finding and doing work. A command written there that the CLI
rejects (a removed flag, a missing required argument) leaves the agent with no
working way to proceed, and nothing else would notice.
"""

from __future__ import annotations

import contextlib
import io
import re
import shlex
from pathlib import Path
from typing import Iterator, List, Optional, Tuple

import click
import pytest
import typer

from drspec.cli.app import app

ROOT = Path(__file__).resolve().parents[2]
FILES = sorted(ROOT.glob("src/drspec/agents/**/*.md")) + [ROOT / "README.md"] + sorted(ROOT.glob(".claude/skills/**/SKILL.md"))

_FENCE = re.compile(r"^\s*```")
_INLINE = re.compile(r"`([^`\n]+)`")
_OPTIONAL = re.compile(r"\[[^\]]*\]")
_PLACEHOLDER = re.compile(r"<[^>]*>")
_SHELL_OPERATORS = ("<", "<<", ">", ">>", "|")


def command_lines(text: str) -> Iterator[Tuple[int, str]]:
    """`(line number, text)` for each line in a code block, and each inline code
    span, that starts with `drspec `."""
    in_fence = False
    for number, line in enumerate(text.splitlines(), 1):
        if _FENCE.match(line):
            in_fence = not in_fence
            continue
        if in_fence:
            stripped = line.strip()
            if stripped.startswith("$ "):
                stripped = stripped[2:]
            if stripped.startswith("drspec "):
                yield number, stripped
        else:
            for match in _INLINE.finditer(line):
                span = match.group(1).strip()
                if span.startswith("drspec "):
                    yield number, span


def arguments(line: str) -> Optional[List[str]]:
    """The arguments to give the parser, or `None` for a line that cannot be
    checked (a deliberate error example, an alternation, a generic placeholder
    in place of the command)."""
    if re.search(r"#.*\bERROR\b", line):
        return None
    try:
        tokens = shlex.split(_OPTIONAL.sub("", line), comments=True)[1:]
    except ValueError:
        return None
    result: List[str] = []
    for token in tokens:
        if token in _SHELL_OPERATORS:
            break
        if any(mark in token for mark in ("|", "...", "{", "}")):
            return None
        result.append(_PLACEHOLDER.sub("1", token))
    if result and result[0] == "1":  # `drspec <command> --help`
        return None
    return result


def _names_only(root: click.Command, argv: List[str]) -> bool:
    """True when every word is a command name: a mention of the command, not a call."""
    node = root
    for token in argv:
        if isinstance(node, click.Group) and token in node.commands:
            node = node.commands[token]
        else:
            return False
    return True


def parse_error(root: click.Command, argv: List[str]) -> Optional[str]:
    """Why the CLI would reject `argv`, or `None` if it parses."""
    sink = io.StringIO()
    try:
        with contextlib.redirect_stdout(sink), contextlib.redirect_stderr(sink):
            ctx = root.make_context("drspec", list(argv))
            while isinstance(ctx.command, click.Group):
                rest = [*ctx.protected_args, *ctx.args]
                if not rest:
                    break
                name, command, rest = ctx.command.resolve_command(ctx, rest)
                ctx = command.make_context(name, rest, parent=ctx)
    except click.exceptions.Exit:
        return None  # --help and --version are fine
    except click.MissingParameter as error:
        return None if _names_only(root, argv) else f"{type(error).__name__}: {error.format_message()}"
    except click.UsageError as error:
        return f"{type(error).__name__}: {error.format_message()}"
    return None


@pytest.fixture(scope="module")
def root_command() -> click.Command:
    return typer.main.get_command(app)


def _commands():
    for path in FILES:
        for number, line in command_lines(path.read_text(encoding="utf-8")):
            argv = arguments(line)
            if argv is not None:
                yield pytest.param(path, number, line, argv, id=f"{path.relative_to(ROOT)}:{number}")


class TestDocumentedCommandsParse:
    def test_the_documents_are_found(self):
        assert any(path.name == "librarian.md" for path in FILES) and any(path.name == "README.md" for path in FILES)
        assert sum(1 for _ in _commands()) > 100

    @pytest.mark.parametrize("path,number,line,argv", list(_commands()))
    def test_the_command_parses(self, root_command, path, number, line, argv):
        problem = parse_error(root_command, argv)
        assert problem is None, f"{path.relative_to(ROOT)}:{number}: `{line}` does not parse: {problem}"

class TestTheCheckerItself:
    """The guard is worthless if it accepts what it should reject."""

    @pytest.mark.parametrize(
        "line",
        [
            "drspec --pretty search --status PENDING",  # a flag that was removed
            "drspec search --no-such-flag x",
            "drspec queue next",  # a command that was removed
            "drspec contract save 'a.py::f'",  # the required --confidence is missing
        ],
    )
    def test_broken_commands_are_caught(self, root_command, line):
        assert parse_error(root_command, arguments(line)) is not None

    @pytest.mark.parametrize(
        "line",
        [
            "drspec search --status PENDING",
            "drspec search --status STALE --path src/auth/ --limit 5",
            "drspec --json search --status PENDING",
            "drspec deps get",  # names the command, gives no call
            "drspec --help",
            "drspec scan <path> [--embed]",
            "drspec contract save '<function_id>' --confidence <score> << 'EOF'",
        ],
    )
    def test_good_commands_pass(self, root_command, line):
        assert parse_error(root_command, arguments(line)) is None

    def test_lines_that_cannot_be_checked_are_skipped(self):
        assert arguments("drspec status --json         # ERROR: --json must come before the command") is None
        assert arguments("drspec <command> --help    # Command-specific help") is None
        assert arguments("drspec search a|b") is None
