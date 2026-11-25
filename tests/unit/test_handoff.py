"""Tests for cross-agent handoff messaging: the message an agent leaves for the next one, and the text it is shown as."""

from __future__ import annotations

import pytest

from drspec.core import (
    HandoffMessage,
    create_debugger_to_architect_handoff,
    create_handoff,
    create_handoff_from_missing_report,
    create_judge_to_vision_handoff,
    create_librarian_to_architect_handoff,
    format_handoff_message,
)
from drspec.debugging.missing import MissingContract, MissingContractReport

pytestmark = pytest.mark.unit  # dataclasses and strings

RULE = "=" * 70


# =============================================================================
# HandoffMessage Tests
# =============================================================================


class TestHandoffMessage:
    """Tests for HandoffMessage dataclass."""

    def test_create_handoff_message(self):
        message = HandoffMessage(
            from_agent="Debugger",
            to_agent="Architect Council",
            reason="Missing contracts needed",
            context={"function_id": "test::func"},
            action_text="Activate Architect Council",
            is_required=True,
        )

        assert (message.from_agent, message.to_agent, message.reason) == ("Debugger", "Architect Council", "Missing contracts needed")
        assert message.context == {"function_id": "test::func"}
        assert message.action_text == "Activate Architect Council"
        assert message.is_required is True

    def test_default_values(self):
        message = HandoffMessage(
            from_agent="Test",
            to_agent="Other",
            reason="Testing",
        )

        assert message.context == {}
        assert message.action_text == ""
        assert message.is_required is False


    def test_to_dict(self):
        message = HandoffMessage(
            from_agent="Debugger",
            to_agent="Architect Council",
            reason="Need contracts",
            context={"key": "value"},
            action_text="Do something",
            is_required=True,
        )

        assert message.to_dict() == {
            "from_agent": "Debugger",
            "to_agent": "Architect Council",
            "reason": "Need contracts",
            "context": {"key": "value"},
            "action_text": "Do something",
            "is_required": True,
        }


# =============================================================================
# Formatting Tests
# =============================================================================


class TestFormatHandoffMessage:
    """Tests for format_handoff_message function: the exact text an agent is shown."""

    def test_formats_basic_message(self):
        message = HandoffMessage(
            from_agent="Debugger",
            to_agent="Architect Council",
            reason="Missing contracts",
            action_text="Activate Architect Council",
        )

        assert format_handoff_message(message) == (
            f"{RULE}\n  AGENT HANDOFF RECOMMENDED\n{RULE}\n\nFrom: Debugger\nTo:   Architect Council\n\nReason: Missing contracts\n\n"
            f"Action: Activate Architect Council\n\n{RULE}"
        )

    def test_formats_required_handoff(self):
        message = HandoffMessage(
            from_agent="Test",
            to_agent="Other",
            reason="Must switch",
            is_required=True,
        )

        assert format_handoff_message(message) == (
            f"{RULE}\n  AGENT HANDOFF REQUIRED\n{RULE}\n\nFrom: Test\nTo:   Other\n\nReason: Must switch\n\n{RULE}"  # no action line without an action text
        )

    def test_formats_recommended_handoff(self):
        message = HandoffMessage(
            from_agent="Test",
            to_agent="Other",
            reason="Should switch",
            is_required=False,
        )

        text = format_handoff_message(message)

        assert text.splitlines()[1] == "  AGENT HANDOFF RECOMMENDED"
        assert "REQUIRED" not in text

    def test_includes_context(self):
        message = HandoffMessage(
            from_agent="Test",
            to_agent="Other",
            reason="Testing",
            context={"function_id": "test::func", "priority": 1},
        )

        text = format_handoff_message(message)

        assert text == (
            f"{RULE}\n  AGENT HANDOFF RECOMMENDED\n{RULE}\n\nFrom: Test\nTo:   Other\n\nReason: Testing\n\n"
            f"Context for Other:\n{'-' * 40}\n  function_id: test::func\n  priority: 1\n\n{RULE}"
        )

    def test_truncates_large_lists(self):
        message = HandoffMessage(
            from_agent="Test",
            to_agent="Other",
            reason="Testing",
            context={"items": ["a", "b", "c", "d", "e", "f"]},
        )

        text = format_handoff_message(message)

        assert "  items: [6 items]\n    - a\n    - b\n    - c\n    ... and 3 more\n" in text  # three shown, the rest counted
        assert "- d" not in text

    @pytest.mark.parametrize("sender, receiver, reason", [("A", "B", "reason1"), ("C", "D", "reason2"), ("E", "F", "reason3")])
    def test_consistent_format(self, sender, receiver, reason):
        lines = format_handoff_message(HandoffMessage(sender, receiver, reason)).splitlines()

        assert lines[0] == lines[2] == lines[-1] == RULE  # the same frame for every message
        assert (lines[4], lines[5], lines[7]) == (f"From: {sender}", f"To:   {receiver}", f"Reason: {reason}")


# =============================================================================
# Debugger Handoff Tests
# =============================================================================


class TestDebuggerToArchitectHandoff:
    """Tests for Debugger -> Architect Council handoff."""

    def test_creates_handoff_for_missing_contracts(self):
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
        assert message.reason == "Found 2 function(s) in the debug call chain without contracts. Complete contracts enable more precise debugging."
        assert message.action_text.startswith("Please activate the Architect Council")

    def test_includes_function_list(self):
        missing = [
            {"function_id": "a::b", "priority": 1},
            {"function_id": "c::d", "priority": 2},
        ]

        message = create_debugger_to_architect_handoff(
            missing_contracts=missing,
            target_function_id="target::func",
            target_has_contract=True,
        )

        assert message.context == {
            "missing_contracts": ["a::b", "c::d"],
            "priorities": {"a::b": 1, "c::d": 2},
            "debug_target": "target::func",
        }

    def test_required_when_target_missing(self):
        missing = [{"function_id": "target::func", "priority": 1}]

        message = create_debugger_to_architect_handoff(
            missing_contracts=missing,
            target_function_id="target::func",
            target_has_contract=False,
        )

        assert message.is_required is True
        assert message.reason == (
            "The function being debugged (target::func) lacks a contract. Found 1 function(s) in the call chain without contracts."
        )

    def test_optional_when_target_has_contract(self):
        missing = [{"function_id": "other::func", "priority": 1}]

        message = create_debugger_to_architect_handoff(
            missing_contracts=missing,
            target_function_id="target::func",
            target_has_contract=True,
        )

        assert message.is_required is False


# =============================================================================
# Librarian Handoff Tests
# =============================================================================


class TestLibrarianToArchitectHandoff:
    """Tests for Librarian -> Architect Council handoff."""

    def test_creates_handoff_after_scan(self):
        message = create_librarian_to_architect_handoff(
            pending_count=42,
            highest_priority_id="important::func",
            priority_areas=["src/core/", "src/utils/"],
        )

        assert message.from_agent == "Librarian"
        assert message.to_agent == "Architect Council"
        assert message.reason == "Scan complete. 42 functions are waiting for a contract."
        assert message.context == {"pending_count": 42, "suggested_start": "important::func", "priority_areas": ["src/core/", "src/utils/"]}


    def test_includes_suggested_start(self):
        message = create_librarian_to_architect_handoff(
            pending_count=10,
            highest_priority_id="start::here",
        )

        assert message.context == {"pending_count": 10, "suggested_start": "start::here"}

    def test_includes_priority_areas(self):
        message = create_librarian_to_architect_handoff(
            pending_count=10,
            priority_areas=["src/core/"],
        )

        assert message.context == {"pending_count": 10, "priority_areas": ["src/core/"]}

    def test_always_optional(self):
        """User chooses when to build; this handoff is never mandatory."""
        message = create_librarian_to_architect_handoff(pending_count=100)

        assert message.is_required is False


# =============================================================================
# Judge Handoff Tests
# =============================================================================


class TestJudgeToVisionHandoff:
    """Tests for Judge -> Vision Analyst handoff."""

    def test_creates_handoff_for_visualization(self):
        message = create_judge_to_vision_handoff(
            function_id="complex::func",
            reason="Need visual analysis of edge cases",
        )

        assert message.from_agent == "Judge"
        assert message.to_agent == "Vision Analyst"
        assert message.reason == "Need visual analysis of edge cases"
        assert message.context["function_id"] == "complex::func"

    def test_includes_function_to_visualize(self):
        message = create_judge_to_vision_handoff(
            function_id="target::func",
            reason="test",
        )

        assert message.context["function_id"] == "target::func"

    def test_includes_what_to_look_for(self):
        message = create_judge_to_vision_handoff(
            function_id="test::func",
            reason="test",
            look_for=["boundaries", "clusters"],
        )

        assert message.context["look_for"] == ["boundaries", "clusters"]  # instead of the default list

    def test_default_look_for(self):
        message = create_judge_to_vision_handoff(
            function_id="test::func",
            reason="test",
        )

        assert message.context == {
            "function_id": "test::func",
            "plot_request": "data_plot",
            "look_for": ["outliers", "discontinuities", "unexpected patterns"],
        }

    def test_always_optional(self):
        """Visualization is supplementary, never mandatory."""
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
        message = create_handoff(
            from_agent="Custom Agent",
            to_agent="Another Agent",
            reason="Custom reason",
            context={"custom": "data"},
            action_text="Do custom action",
            is_required=True,
        )

        assert message == HandoffMessage("Custom Agent", "Another Agent", "Custom reason", {"custom": "data"}, "Do custom action", True)

    def test_default_context(self):
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
    """Tests for integration with the product's MissingContractReport (the real class, not a stand-in)."""

    def _report(self, target_has_contract):
        return MissingContractReport(
            target_function_id="target::func",
            target_has_contract=target_has_contract,
            missing_contracts=[
                MissingContract("a::b", "a.py", "b", "direct", 0, 1, "called by the target"),
                MissingContract("c::d", "c.py", "d", "callee", 1, 2, "called by a::b"),
            ],
            total_missing=2,
        )

    def test_creates_handoff_from_a_report(self):
        message = create_handoff_from_missing_report(self._report(target_has_contract=False))

        assert message.from_agent == "Debugger" and message.to_agent == "Architect Council"
        assert message.is_required is True  # target lacks contract
        assert message.context == {"missing_contracts": ["a::b", "c::d"], "priorities": {"a::b": 1, "c::d": 2}, "debug_target": "target::func"}


    def test_returns_none_when_no_missing(self):
        report = MissingContractReport(target_function_id="target::func", target_has_contract=True)

        assert create_handoff_from_missing_report(report) is None
