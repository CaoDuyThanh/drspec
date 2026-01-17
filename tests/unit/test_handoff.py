"""Tests for cross-agent handoff messaging (Story 5-6).

These tests verify the handoff messaging API:
- HandoffMessage dataclass
- format_handoff_message: Format for display
- Agent-specific handoff creation functions
- Integration with MissingContractReport
"""

from __future__ import annotations

import pytest

from drspec.core import (
    HandoffMessage,
    format_handoff_message,
    create_debugger_to_architect_handoff,
    create_librarian_to_architect_handoff,
    create_judge_to_vision_handoff,
    create_handoff,
    create_handoff_from_missing_report,
)


# =============================================================================
# HandoffMessage Tests
# =============================================================================


class TestHandoffMessage:
    """Tests for HandoffMessage dataclass."""

    def test_create_handoff_message(self):
        """Should create a handoff message."""
        message = HandoffMessage(
            from_agent="Debugger",
            to_agent="Architect Council",
            reason="Missing contracts needed",
            context={"function_id": "test::func"},
            action_text="Activate Architect Council",
            is_required=True,
        )

        assert message.from_agent == "Debugger"
        assert message.to_agent == "Architect Council"
        assert message.is_required is True

    def test_default_values(self):
        """Should have sensible defaults."""
        message = HandoffMessage(
            from_agent="Test",
            to_agent="Other",
            reason="Testing",
        )

        assert message.context == {}
        assert message.action_text == ""
        assert message.is_required is False

    def test_to_dict(self):
        """Should convert to dictionary."""
        message = HandoffMessage(
            from_agent="Debugger",
            to_agent="Architect Council",
            reason="Need contracts",
            context={"key": "value"},
            action_text="Do something",
            is_required=True,
        )

        d = message.to_dict()

        assert d["from_agent"] == "Debugger"
        assert d["to_agent"] == "Architect Council"
        assert d["reason"] == "Need contracts"
        assert d["context"]["key"] == "value"
        assert d["is_required"] is True


# =============================================================================
# Formatting Tests (AC: 1, 7)
# =============================================================================


class TestFormatHandoffMessage:
    """Tests for format_handoff_message function."""

    def test_formats_basic_message(self):
        """Should format basic handoff message (AC: 1)."""
        message = HandoffMessage(
            from_agent="Debugger",
            to_agent="Architect Council",
            reason="Missing contracts",
            action_text="Activate Architect Council",
        )

        text = format_handoff_message(message)

        assert "AGENT HANDOFF" in text
        assert "Debugger" in text
        assert "Architect Council" in text
        assert "Missing contracts" in text

    def test_formats_required_handoff(self):
        """Should indicate required handoffs clearly."""
        message = HandoffMessage(
            from_agent="Test",
            to_agent="Other",
            reason="Must switch",
            is_required=True,
        )

        text = format_handoff_message(message)

        assert "REQUIRED" in text

    def test_formats_recommended_handoff(self):
        """Should indicate recommended handoffs."""
        message = HandoffMessage(
            from_agent="Test",
            to_agent="Other",
            reason="Should switch",
            is_required=False,
        )

        text = format_handoff_message(message)

        assert "RECOMMENDED" in text

    def test_includes_context(self):
        """Should include context in formatted output (AC: 3)."""
        message = HandoffMessage(
            from_agent="Test",
            to_agent="Other",
            reason="Testing",
            context={"function_id": "test::func", "priority": 1},
        )

        text = format_handoff_message(message)

        assert "Context for Other:" in text
        assert "function_id" in text
        assert "test::func" in text

    def test_truncates_large_lists(self):
        """Should truncate large lists in context."""
        message = HandoffMessage(
            from_agent="Test",
            to_agent="Other",
            reason="Testing",
            context={"items": ["a", "b", "c", "d", "e", "f"]},
        )

        text = format_handoff_message(message)

        assert "[6 items]" in text
        assert "and 3 more" in text

    def test_consistent_format(self):
        """Should have consistent formatting (AC: 7)."""
        messages = [
            HandoffMessage("A", "B", "reason1"),
            HandoffMessage("C", "D", "reason2"),
            HandoffMessage("E", "F", "reason3"),
        ]

        for msg in messages:
            text = format_handoff_message(msg)
            # All should have header/footer
            assert text.count("=") >= 70
            assert "From:" in text
            assert "To:" in text
            assert "Reason:" in text


# =============================================================================
# Debugger Handoff Tests (AC: 4)
# =============================================================================


class TestDebuggerToArchitectHandoff:
    """Tests for Debugger -> Architect Council handoff."""

    def test_creates_handoff_for_missing_contracts(self):
        """Should create handoff for missing contracts (AC: 4)."""
        missing = [
            {"function_id": "test.py::func1", "priority": 1},
            {"function_id": "test.py::func2", "priority": 2},
        ]

        message = create_debugger_to_architect_handoff(
            missing_contracts=missing,
            target_function_id="target::func",
            target_has_contract=True,
        )

        assert message.from_agent == "Debugger"
        assert message.to_agent == "Architect Council"
        assert "2" in message.reason  # 2 functions
        assert "missing_contracts" in message.context

    def test_includes_function_list(self):
        """Should include missing function list."""
        missing = [
            {"function_id": "a::b", "priority": 1},
            {"function_id": "c::d", "priority": 2},
        ]

        message = create_debugger_to_architect_handoff(
            missing_contracts=missing,
            target_function_id="target::func",
            target_has_contract=True,
        )

        assert "a::b" in message.context["missing_contracts"]
        assert "c::d" in message.context["missing_contracts"]

    def test_includes_priorities(self):
        """Should include prioritization."""
        missing = [
            {"function_id": "a::b", "priority": 1},
        ]

        message = create_debugger_to_architect_handoff(
            missing_contracts=missing,
            target_function_id="target::func",
            target_has_contract=True,
        )

        assert message.context["priorities"]["a::b"] == 1

    def test_required_when_target_missing(self):
        """Should be required when target lacks contract."""
        missing = [{"function_id": "target::func", "priority": 1}]

        message = create_debugger_to_architect_handoff(
            missing_contracts=missing,
            target_function_id="target::func",
            target_has_contract=False,
        )

        assert message.is_required is True

    def test_optional_when_target_has_contract(self):
        """Should be optional when target has contract."""
        missing = [{"function_id": "other::func", "priority": 1}]

        message = create_debugger_to_architect_handoff(
            missing_contracts=missing,
            target_function_id="target::func",
            target_has_contract=True,
        )

        assert message.is_required is False


# =============================================================================
# Librarian Handoff Tests (AC: 5)
# =============================================================================


class TestLibrarianToArchitectHandoff:
    """Tests for Librarian -> Architect Council handoff."""

    def test_creates_handoff_after_scan(self):
        """Should create handoff after scan (AC: 5)."""
        message = create_librarian_to_architect_handoff(
            pending_count=42,
            highest_priority_id="important::func",
            priority_areas=["src/core/", "src/utils/"],
        )

        assert message.from_agent == "Librarian"
        assert message.to_agent == "Architect Council"
        assert "42" in message.reason

    def test_includes_queue_summary(self):
        """Should include queue summary."""
        message = create_librarian_to_architect_handoff(
            pending_count=10,
        )

        assert message.context["queue_size"] == 10

    def test_includes_suggested_start(self):
        """Should include suggested start point."""
        message = create_librarian_to_architect_handoff(
            pending_count=10,
            highest_priority_id="start::here",
        )

        assert message.context["suggested_start"] == "start::here"

    def test_includes_priority_areas(self):
        """Should include priority areas."""
        message = create_librarian_to_architect_handoff(
            pending_count=10,
            priority_areas=["src/core/"],
        )

        assert "src/core/" in message.context["priority_areas"]

    def test_always_optional(self):
        """Should always be optional (user chooses when to build)."""
        message = create_librarian_to_architect_handoff(pending_count=100)

        assert message.is_required is False


# =============================================================================
# Judge Handoff Tests (AC: 6)
# =============================================================================


class TestJudgeToVisionHandoff:
    """Tests for Judge -> Vision Analyst handoff."""

    def test_creates_handoff_for_visualization(self):
        """Should create handoff for visualization (AC: 6)."""
        message = create_judge_to_vision_handoff(
            function_id="complex::func",
            reason="Need visual analysis of edge cases",
        )

        assert message.from_agent == "Judge"
        assert message.to_agent == "Vision Analyst"
        assert "complex::func" in message.context["function_id"]

    def test_includes_function_to_visualize(self):
        """Should include function to visualize."""
        message = create_judge_to_vision_handoff(
            function_id="target::func",
            reason="test",
        )

        assert message.context["function_id"] == "target::func"

    def test_includes_what_to_look_for(self):
        """Should include what to look for."""
        message = create_judge_to_vision_handoff(
            function_id="test::func",
            reason="test",
            look_for=["boundaries", "clusters"],
        )

        assert "boundaries" in message.context["look_for"]
        assert "clusters" in message.context["look_for"]

    def test_default_look_for(self):
        """Should have default patterns to look for."""
        message = create_judge_to_vision_handoff(
            function_id="test::func",
            reason="test",
        )

        assert len(message.context["look_for"]) > 0

    def test_always_optional(self):
        """Should always be optional (visualization is supplementary)."""
        message = create_judge_to_vision_handoff(
            function_id="test::func",
            reason="test",
        )

        assert message.is_required is False


# =============================================================================
# Generic Handoff Tests
# =============================================================================


class TestCreateHandoff:
    """Tests for generic handoff creation."""

    def test_creates_custom_handoff(self):
        """Should create custom handoff between any agents."""
        message = create_handoff(
            from_agent="Custom Agent",
            to_agent="Another Agent",
            reason="Custom reason",
            context={"custom": "data"},
            action_text="Do custom action",
            is_required=True,
        )

        assert message.from_agent == "Custom Agent"
        assert message.to_agent == "Another Agent"
        assert message.context["custom"] == "data"
        assert message.is_required is True

    def test_default_context(self):
        """Should default context to empty dict."""
        message = create_handoff(
            from_agent="A",
            to_agent="B",
            reason="test",
        )

        assert message.context == {}


# =============================================================================
# Integration Tests
# =============================================================================


class TestMissingContractReportIntegration:
    """Tests for integration with MissingContractReport."""

    def test_creates_handoff_from_mock_report(self):
        """Should create handoff from MissingContractReport-like object."""
        # Create a mock MissingContractReport
        class MockMissingContract:
            def __init__(self, function_id, priority, relationship):
                self.function_id = function_id
                self.priority = priority
                self.relationship = relationship

        class MockReport:
            def __init__(self):
                self.target_function_id = "target::func"
                self.target_has_contract = False
                self.missing_contracts = [
                    MockMissingContract("a::b", 1, "direct"),
                    MockMissingContract("c::d", 2, "callee"),
                ]

            @property
            def has_missing(self):
                return len(self.missing_contracts) > 0

        report = MockReport()
        message = create_handoff_from_missing_report(report)

        assert message is not None
        assert message.from_agent == "Debugger"
        assert message.to_agent == "Architect Council"
        assert message.is_required is True  # target lacks contract

    def test_returns_none_when_no_missing(self):
        """Should return None when no missing contracts."""
        class MockReport:
            @property
            def has_missing(self):
                return False

        report = MockReport()
        message = create_handoff_from_missing_report(report)

        assert message is None
