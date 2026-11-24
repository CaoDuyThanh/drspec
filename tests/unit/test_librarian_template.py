"""The Librarian agent prompt (`agents/librarian.md`): the sections an agent needs to find, the statuses it explains (against the product's list),
the commands it hands the Librarian, and the languages it claims (against what the scanner reads). That every `drspec ...` command in it PARSES is
`test_prompt_commands.py`'s job and is not repeated here."""

import re

import pytest

from drspec.core.resources import get_templates_path
from drspec.core.scanner import LANGUAGE_MAP
from drspec.db.queries import VALID_ARTIFACT_STATUSES

pytestmark = pytest.mark.unit  # reads one packaged text file

#: How the prompt names each language of the scanner (`tsx` is TypeScript, `c` is read by the C++ parser).
LANGUAGE_NAMES = {"python": "Python", "javascript": "JavaScript", "typescript": "TypeScript", "tsx": "TypeScript", "cpp": "C++", "c": "C++", "rust": "Rust"}


@pytest.fixture(scope="module")
def librarian_content() -> str:
    return (get_templates_path() / "librarian.md").read_text()


def _section(text: str, heading: str) -> str:
    """The text under `heading` (a line like `## Workflows`) up to the next heading of the same or a higher level."""
    level = len(heading) - len(heading.lstrip("#"))
    match = re.search(rf"^{re.escape(heading)}\s*$(.*?)(?=^#{{1,{level}}} |\Z)", text, re.S | re.M)
    assert match, f"no section {heading!r} in librarian.md"
    return match.group(1)


class TestLibrarianTemplate:
    """Tests for librarian.md template content."""

    def test_the_sections_an_agent_looks_for_are_there_in_this_order(self, librarian_content):
        headings = [line for line in librarian_content.splitlines() if re.match(r"^#{1,2} ", line)]

        required = ["# Librarian Agent", "## Persona", "## Primary Responsibilities", "## CLI Reference", "## Workflows",
                    "## Finding Functions That Need Contracts", "## Examples", "## Handoff Protocol"]
        positions = [headings.index(heading) for heading in required]  # a missing section is a ValueError
        assert positions == sorted(positions)

    def test_defines_persona(self, librarian_content):
        persona = _section(librarian_content, "## Persona")

        assert "**Name:** Iris" in persona
        assert "### Principles" in persona

    def test_the_cli_reference_gives_the_commands_the_librarian_works_with(self, librarian_content):
        reference = _section(librarian_content, "## CLI Reference")

        for command in ("drspec scan", "drspec status", "drspec search --status PENDING", "drspec search --status STALE"):
            assert command in reference, command

    def test_the_initial_workflow_runs_init_then_scan(self, librarian_content):
        workflow = _section(librarian_content, "### Initial Scan Workflow")

        assert 0 <= workflow.index("drspec init") < workflow.index("drspec scan")

    def test_every_status_the_product_has_is_explained(self, librarian_content):
        explained = _section(librarian_content, "### Understanding Function Status")

        assert set(re.findall(r"^- \*\*([A-Z_]+)\*\*:", explained, re.M)) == set(VALID_ARTIFACT_STATUSES)

    def test_work_to_do_is_pending_or_stale(self, librarian_content):
        finding = _section(librarian_content, "## Finding Functions That Need Contracts")

        assert "drspec search --status PENDING" in finding and "drspec search --status STALE" in finding

    def test_the_handoff_names_the_next_agent_and_when_to_hand_over(self, librarian_content):
        handoff = _section(librarian_content, "## Handoff Protocol")

        assert "### When to Suggest Architect Council" in handoff
        assert "### Handoff Message" in handoff

    def test_the_examples_are_dialogues_with_the_user(self, librarian_content):
        examples = _section(librarian_content, "## Examples")

        assert re.findall(r"^### ", examples, re.M)  # at least one named example
        assert "**User**:" in examples and "**Librarian**:" in examples  # both sides of the dialogue

    def test_the_languages_it_names_are_languages_the_scanner_reads(self, librarian_content):
        line = next(line for line in librarian_content.splitlines() if line.startswith("- Supported languages:"))
        named = set(re.split(r"[,/]\s*", line.split(":", 1)[1].strip()))

        known = {LANGUAGE_NAMES[language] for language in set(LANGUAGE_MAP.values())}
        assert named <= known, f"the prompt promises languages the scanner does not read: {named - known}"


    def test_the_template_is_one_page_not_a_stub_or_a_manual(self, librarian_content):
        # a size band, not a measurement: the prompt is loaded whole into an agent's context
        assert 2000 < len(librarian_content) < 20000
