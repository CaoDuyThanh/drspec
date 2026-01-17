"""Tests for the Librarian agent prompt template."""


import pytest

from drspec.core.resources import get_templates_path


class TestLibrarianTemplate:
    """Tests for librarian.md template content."""

    @pytest.fixture
    def librarian_content(self) -> str:
        """Load the librarian template content."""
        path = get_templates_path() / "librarian.md"
        return path.read_text()

    def test_template_exists(self):
        """Test that librarian.md exists."""
        path = get_templates_path() / "librarian.md"
        assert path.exists()

    def test_defines_persona(self, librarian_content):
        """Test that template defines enhanced persona section."""
        assert "## Persona" in librarian_content
        assert "Librarian" in librarian_content
        # Enhanced persona includes name and principles
        assert "**Name:**" in librarian_content
        assert "Iris" in librarian_content
        assert "### Principles" in librarian_content

    def test_defines_responsibilities(self, librarian_content):
        """Test that template defines primary responsibilities."""
        assert "Scanning" in librarian_content or "scanning" in librarian_content
        assert "Queue" in librarian_content or "queue" in librarian_content
        assert "Status" in librarian_content or "status" in librarian_content

    def test_documents_scan_command(self, librarian_content):
        """Test that drspec scan command is documented."""
        assert "drspec scan" in librarian_content
        # Should have usage examples
        assert "drspec scan ./src" in librarian_content or "drspec scan [path]" in librarian_content

    def test_documents_status_command(self, librarian_content):
        """Test that drspec status command is documented."""
        assert "drspec status" in librarian_content

    def test_documents_queue_commands(self, librarian_content):
        """Test that queue commands are documented."""
        assert "queue peek" in librarian_content
        assert "queue prioritize" in librarian_content or "prioritize" in librarian_content

    def test_includes_workflow_guidance(self, librarian_content):
        """Test that template includes workflow guidance."""
        assert "Workflow" in librarian_content or "workflow" in librarian_content
        # Should guide through initial scan
        assert "init" in librarian_content
        assert "scan" in librarian_content

    def test_includes_examples(self, librarian_content):
        """Test that template includes example interactions."""
        assert "Example" in librarian_content or "## Example" in librarian_content
        # Should have at least one user/librarian exchange
        assert "User" in librarian_content

    def test_includes_handoff_protocol(self, librarian_content):
        """Test that template includes handoff protocol."""
        assert "Handoff" in librarian_content or "handoff" in librarian_content
        # Should mention Architect Council
        assert "Architect" in librarian_content or "Council" in librarian_content

    def test_explains_queue_statuses(self, librarian_content):
        """Test that queue statuses are explained."""
        assert "PENDING" in librarian_content
        assert "PROCESSING" in librarian_content or "Processing" in librarian_content

    def test_mentions_supported_languages(self, librarian_content):
        """Test that supported languages are mentioned."""
        assert "Python" in librarian_content
        assert "JavaScript" in librarian_content or "TypeScript" in librarian_content

    def test_reasonable_length(self, librarian_content):
        """Test that template is substantial enough to be useful."""
        # Should be more than just a stub
        assert len(librarian_content) > 2000
        # But not unreasonably long
        assert len(librarian_content) < 20000
