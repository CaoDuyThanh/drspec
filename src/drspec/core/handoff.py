"""Cross-agent handoff messaging for DrSpec multi-agent system.

This module provides standardized handoff messages that agents use
to communicate when users should switch to another agent.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Optional


# =============================================================================
# Handoff Models
# =============================================================================


@dataclass
class HandoffMessage:
    """Standardized message for cross-agent handoffs.

    Attributes:
        from_agent: Name of the agent initiating the handoff.
        to_agent: Name of the target agent to activate.
        reason: Clear explanation of why the handoff is needed.
        context: Data to pass to the next agent.
        action_text: Instructions for the user.
        is_required: Whether handoff is required to continue.
    """

    from_agent: str
    to_agent: str
    reason: str
    context: dict[str, Any] = field(default_factory=dict)
    action_text: str = ""
    is_required: bool = False

    def to_dict(self) -> dict[str, Any]:
        """Convert to dictionary for JSON serialization."""
        return {
            "from_agent": self.from_agent,
            "to_agent": self.to_agent,
            "reason": self.reason,
            "context": self.context,
            "action_text": self.action_text,
            "is_required": self.is_required,
        }


# =============================================================================
# Handoff Formatting
# =============================================================================


def format_handoff_message(message: HandoffMessage) -> str:
    """Format a handoff message for user display.

    Args:
        message: HandoffMessage to format.

    Returns:
        Formatted text suitable for terminal display.
    """
    required_text = "REQUIRED" if message.is_required else "RECOMMENDED"
    header = f"AGENT HANDOFF {required_text}"

    lines = []
    lines.append("=" * 70)
    lines.append(f"  {header}")
    lines.append("=" * 70)
    lines.append("")
    lines.append(f"From: {message.from_agent}")
    lines.append(f"To:   {message.to_agent}")
    lines.append("")
    lines.append(f"Reason: {message.reason}")
    lines.append("")

    if message.action_text:
        lines.append(f"Action: {message.action_text}")
        lines.append("")

    if message.context:
        lines.append(f"Context for {message.to_agent}:")
        lines.append("-" * 40)
        for key, value in message.context.items():
            if isinstance(value, list) and len(value) > 3:
                lines.append(f"  {key}: [{len(value)} items]")
                for item in value[:3]:
                    lines.append(f"    - {item}")
                lines.append(f"    ... and {len(value) - 3} more")
            elif isinstance(value, dict) and len(value) > 3:
                lines.append(f"  {key}: {{{len(value)} entries}}")
            else:
                lines.append(f"  {key}: {value}")
        lines.append("")

    lines.append("=" * 70)

    return "\n".join(lines)


# =============================================================================
# Debugger Handoffs
# =============================================================================


def create_debugger_to_architect_handoff(
    missing_contracts: list[dict[str, Any]],
    target_function_id: str,
    target_has_contract: bool,
) -> HandoffMessage:
    """Create handoff from Debugger to Architect Council for missing contracts.

    Args:
        missing_contracts: List of missing contract details with function_id, priority.
        target_function_id: The function being debugged.
        target_has_contract: Whether the debug target has a contract.

    Returns:
        HandoffMessage for activating Architect Council.
    """
    total = len(missing_contracts)
    priorities = {m["function_id"]: m["priority"] for m in missing_contracts}

    if not target_has_contract:
        reason = (
            f"The function being debugged ({target_function_id}) lacks a contract. "
            f"Found {total} function(s) in the call chain without contracts."
        )
    else:
        reason = (
            f"Found {total} function(s) in the debug call chain without contracts. "
            f"Complete contracts enable more precise debugging."
        )

    action_text = (
        "Please activate the Architect Council to build contracts for these functions. "
        "The Proposer will analyze each function and generate semantic contracts."
    )

    return HandoffMessage(
        from_agent="Debugger",
        to_agent="Architect Council",
        reason=reason,
        context={
            "missing_contracts": [m["function_id"] for m in missing_contracts],
            "priorities": priorities,
            "debug_target": target_function_id,
        },
        action_text=action_text,
        is_required=not target_has_contract,
    )


# =============================================================================
# Librarian Handoffs
# =============================================================================


def create_librarian_to_architect_handoff(
    pending_count: int,
    highest_priority_id: Optional[str] = None,
    priority_areas: Optional[list[str]] = None,
) -> HandoffMessage:
    """Create handoff from Librarian to Architect Council after scan.

    Args:
        pending_count: Number of functions pending contract generation.
        highest_priority_id: Function ID with highest priority (optional).
        priority_areas: List of file paths or areas with most pending functions.

    Returns:
        HandoffMessage for activating Architect Council.
    """
    reason = f"Scan complete. {pending_count} functions queued for contract analysis."

    action_text = (
        "To start building contracts, activate the Architect Council. "
        "The Proposer will analyze functions and debate contracts with the Critic."
    )

    context: dict[str, Any] = {"queue_size": pending_count}

    if highest_priority_id:
        context["suggested_start"] = highest_priority_id

    if priority_areas:
        context["priority_areas"] = priority_areas

    return HandoffMessage(
        from_agent="Librarian",
        to_agent="Architect Council",
        reason=reason,
        context=context,
        action_text=action_text,
        is_required=False,
    )


# =============================================================================
# Judge Handoffs
# =============================================================================


def create_judge_to_vision_handoff(
    function_id: str,
    reason: str,
    plot_type: str = "data_plot",
    look_for: Optional[list[str]] = None,
) -> HandoffMessage:
    """Create handoff from Judge to Vision Analyst for visualization.

    Args:
        function_id: Function to visualize.
        reason: Why visualization is needed.
        plot_type: Type of plot requested (data_plot, dependency_graph).
        look_for: List of patterns to look for in visualization.

    Returns:
        HandoffMessage for activating Vision Analyst.
    """
    if look_for is None:
        look_for = ["outliers", "discontinuities", "unexpected patterns"]

    action_text = (
        "Please activate the Vision Analyst to analyze the visualization. "
        "The Vision Analyst will look for geometric anomalies that might indicate missing invariants."
    )

    return HandoffMessage(
        from_agent="Judge",
        to_agent="Vision Analyst",
        reason=reason,
        context={
            "function_id": function_id,
            "plot_request": plot_type,
            "look_for": look_for,
        },
        action_text=action_text,
        is_required=False,
    )


# =============================================================================
# Generic Handoff Creation
# =============================================================================


def create_handoff(
    from_agent: str,
    to_agent: str,
    reason: str,
    context: Optional[dict[str, Any]] = None,
    action_text: str = "",
    is_required: bool = False,
) -> HandoffMessage:
    """Create a generic handoff message.

    Args:
        from_agent: Name of originating agent.
        to_agent: Name of target agent.
        reason: Why the handoff is needed.
        context: Data to pass to next agent.
        action_text: Instructions for user.
        is_required: Whether handoff is required.

    Returns:
        HandoffMessage with provided parameters.
    """
    return HandoffMessage(
        from_agent=from_agent,
        to_agent=to_agent,
        reason=reason,
        context=context or {},
        action_text=action_text,
        is_required=is_required,
    )


# =============================================================================
# Handoff Message Integration with MissingContractReport
# =============================================================================


def create_handoff_from_missing_report(
    missing_report: Any,  # MissingContractReport type
) -> Optional[HandoffMessage]:
    """Create handoff message from a MissingContractReport.

    This integrates with the debugging module's missing contract detection.

    Args:
        missing_report: MissingContractReport from detect_missing_contracts.

    Returns:
        HandoffMessage if there are missing contracts, None otherwise.
    """
    if not missing_report.has_missing:
        return None

    # Convert missing contracts to dict format
    missing_list = [
        {
            "function_id": m.function_id,
            "priority": m.priority,
            "relationship": m.relationship,
        }
        for m in missing_report.missing_contracts
    ]

    return create_debugger_to_architect_handoff(
        missing_contracts=missing_list,
        target_function_id=missing_report.target_function_id,
        target_has_contract=missing_report.target_has_contract,
    )
