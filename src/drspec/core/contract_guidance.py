"""When an assistant should run the contract workflow in a sub-agent, and how much per run.

drspec starts nothing by itself: the background worker calls no model, and the agent launchers `init` installs only load a prompt
when the user calls them. This text is what tells the user's assistant when to start the proposer and the critic on its own and
where to stop. It is written into the skill `init --ide claude-code` installs and into the MCP server's instructions, from the
numbers below, so the text and the cap cannot disagree.
"""

from __future__ import annotations

from drspec.core import compact_format

#: Functions the assistant may run the workflow for in one run without asking the user.
MAX_FUNCTIONS_PER_RUN = 5
#: Functions in one run after the user said yes to more.
MAX_FUNCTIONS_WITH_CONSENT = 20
#: Functions one sub-agent takes (one function, or a small group).
MAX_FUNCTIONS_PER_SUB_AGENT = 5

SKILL_NAME = "drspec-contracts"
SKILL_DESCRIPTION = (
    "When to run drspec's contract workflow (proposer, then critic) in a sub-agent, when not to, how, and how many functions per run."
)

#: The marks around the section in a file the user may also write in: a second `init` replaces what is between them.
BEGIN_MARK = "<!-- drspec:contract-guidance:begin -->"
END_MARK = "<!-- drspec:contract-guidance:end -->"


def guidance_section() -> str:
    """The guidance, as Markdown, with the numbers above."""
    return f"""## Contracts: when to run the workflow in a sub-agent

A first scan leaves thousands of functions PENDING. Do not write contracts for every function you meet: do it when it helps the work
at hand, a few at a time.

**Start it when** (the status is in `search`/`status`, the callers in `deps_get`):
- you are about to change a function whose contract status is PENDING or STALE and that has callers (`deps_get` callers not empty);
- the user asks why or how a function behaves;
- a scan result lists changed functions that have callers.

**Do not start it for:** a function with a VERIFIED contract and an unchanged hash, tests, one-line wrappers, generated files, or any
request that is not about those functions.

**How:** one sub-agent per function (or a group of at most {MAX_FUNCTIONS_PER_SUB_AGENT}) that runs the proposer, then the critic,
with the agent prompts `drspec init` installed (`/drspec-proposer`, `/drspec-critic`). The sub-agent returns the saved contract id
and its confidence, not the transcript. Do not read the whole function bodies again in the main conversation.

**How much:** at most {MAX_FUNCTIONS_PER_RUN} functions per run without asking the user, at most {MAX_FUNCTIONS_WITH_CONSENT} after
the user says yes. Then stop and say how many PENDING and STALE functions remain (`status`, or `search` with `status`) instead of
continuing.
"""


def skill_file() -> str:
    """The whole file `init --ide claude-code` writes as `.claude/skills/<SKILL_NAME>/SKILL.md`."""
    return (
        f"---\nname: {SKILL_NAME}\ndescription: {SKILL_DESCRIPTION}\n---\n\n"
        f"{BEGIN_MARK}\n{guidance_section()}{END_MARK}\n"
    )


def mcp_instructions() -> str:
    """The short form the MCP server sends with its `initialize` answer, to a client that shows it to the model."""
    return (
        "Codebase navigation: drspec first. Do not use grep, rg, find or other shell text search for code when a drspec tool answers "
        "the question.\n"
        "- callers, callees, dependencies: `deps_get`\n"
        "- where a function is defined; find by name, text or meaning: `search` (`semantic: true` for meaning)\n"
        "- text of live lines, constants included: `search` with `lines: true` (the grep replacement)\n"
        "- who reads or writes a member (C++): `refs_get`\n"
        "- a yaml/config key set and read in code: `strings_get`\n"
        "- unused functions: `unused`; one function's code: `source_get`; what is indexed or stale: `status`\n"
        "Shell search is for what drspec does not index (logs, build output, files outside the project) or when drspec is unavailable; "
        "an incomplete index is no reason to skip drspec. A claim that nothing calls, uses or sets something (\"no callers\", \"never "
        "used\", \"not set\") is checked with `search` in `lines` mode, not shell grep: unresolved calls and changed files can hide a use.\n"
        "Contracts: do not write them for every function (a first scan leaves thousands PENDING). Run the contract workflow "
        "(proposer, then critic) in a sub-agent only before changing a PENDING or STALE function that has callers, when the user "
        "asks why or how a function behaves, or when a scan lists changed functions; not for VERIFIED functions with an unchanged hash, "
        "tests, one-line wrappers or generated files. One sub-agent per function (a group of at most "
        f"{MAX_FUNCTIONS_PER_SUB_AGENT}), which returns the saved contract id and confidence, not the transcript. At most "
        f"{MAX_FUNCTIONS_PER_RUN} functions per run without asking the user, at most {MAX_FUNCTIONS_WITH_CONSENT} after the user says "
        "yes; then say how many PENDING and STALE functions remain instead of continuing.\n"
        + compact_format.legend()
    )
