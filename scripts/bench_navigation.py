"""One-off measurement (T-209): does the new MCP instruction change which tools a sub-agent reaches for?

The 20 questions are in scripts/bench_navigation_questions.json, each with its checked answer (`key`: regexes the answer must all match).
Every question is asked in two variants, A (the instruction as it was before T-208) and B (the instruction now), each in a fresh
sub-agent: 40 runs.

The script does not start the runs: a sub-agent is started by the session that runs this (the Agent tool), because this
repository has no model API key or CLI for the script to call. The script does the two halves around that:

    python scripts/bench_navigation.py plan --out runs_plan.json
        writes the 40 prompts (run id, variant, question id, prompt text); the session starts one sub-agent per prompt and
        records the agent id of each run in a file shaped like {"A-Q01": "<agent id>", ...}

    python scripts/bench_navigation.py table --session <session id> --runs runs_ids.json
        reads each sub-agent's transcript (under ~/.claude/projects/<project>/<session>/subagents/agent-<id>.jsonl),
        counts its tool calls and checks its final answer, and prints the table

Counts per run:
    drspec      calls to tools named mcp__drspec__* (ToolSearch loads are not counted)
    grep-like   Bash calls whose command, or one of its pipe / `&&` / `;` parts, starts with grep, rg, find, ag or ack
    builtin     calls to the built-in Grep or Glob tools (reported separately, not in grep-like)
    correct     the final answer matches every regex in the question's `key`
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
QUESTIONS = ROOT / "scripts" / "bench_navigation_questions.json"
PROJECT_DIR = Path.home() / ".claude" / "projects" / "-media-thanh-data-Projects-drspec"
GREP_FAMILY = {"grep", "rg", "find", "ag", "ack"}
DRSPEC_PREFIX = "mcp__drspec__"

PROMPT = """You answer one question about the code of this repository: {root}. It is a Python project that drspec has indexed.

Tools: you have the drspec MCP tools (their names start with mcp__drspec__; if they are not loaded, load them first with ToolSearch) and the Bash tool.
Do not change any file. Do not run scan, init, config_set, worker_stop or any other tool that writes.

Guidance for this session (follow it):
<<<
{guidance}
>>>

Question: {question}

Answer in one short paragraph (at most four sentences). Name the functions or files the answer needs."""


def load() -> dict:
    return json.loads(QUESTIONS.read_text(encoding="utf-8"))


def plan_runs(document: dict) -> list:
    """The 40 runs, in question order, A then B for each question."""
    runs = []
    for question in document["questions"]:
        for variant in ("A", "B"):
            prompt = PROMPT.format(root=str(ROOT), guidance=document["instructions"][variant], question=question["question"])
            runs.append({"run": f"{variant}-{question['id']}", "variant": variant, "question": question["id"], "kind": question["kind"], "prompt": prompt})
    return runs


def _command_parts(command: str) -> list:
    """The commands in a Bash line: its pipe, `&&` and `;` parts, each with its first word."""
    words = []
    for part in re.split(r"\|(?!\|)|&&|;", command):
        tokens = part.strip().split()
        while tokens and "=" in tokens[0] and not tokens[0].startswith(("-", "/")):  # VAR=value prefixes
            tokens = tokens[1:]
        if tokens:
            words.append(Path(tokens[0]).name)
    return words


def is_grep_like(command: str) -> bool:
    return any(word in GREP_FAMILY for word in _command_parts(command))


def read_run(path: Path, key: list) -> dict:
    """Count the calls of one sub-agent transcript and check its final answer."""
    counts = {"drspec": 0, "grep_like": 0, "builtin": 0, "bash": 0}
    last_text = ""
    handed_back = ""
    tools = []
    with path.open(encoding="utf-8", errors="ignore") as handle:
        for line in handle:
            try:
                entry = json.loads(line)
            except ValueError:
                continue
            message = entry.get("message") or {}
            if message.get("role") != "assistant" or not isinstance(message.get("content"), list):
                continue
            texts = [block.get("text", "") for block in message["content"] if isinstance(block, dict) and block.get("type") == "text"]
            if any(texts):
                last_text = "\n".join(t for t in texts if t)
            for block in message["content"]:
                if not isinstance(block, dict) or block.get("type") != "tool_use":
                    continue
                name = str(block.get("name", ""))
                tools.append(name)
                if name == "SubagentHandback":  # the sub-agent hands its final report back in this call, not in a text block
                    handed_back = str((block.get("input") or {}).get("message", ""))
                if name.startswith(DRSPEC_PREFIX):
                    counts["drspec"] += 1
                elif name == "Bash":
                    counts["bash"] += 1
                    if is_grep_like(str((block.get("input") or {}).get("command", ""))):
                        counts["grep_like"] += 1
                elif name in ("Grep", "Glob"):
                    counts["builtin"] += 1
    answer = handed_back or last_text
    correct = all(re.search(pattern, answer, re.IGNORECASE) for pattern in key)
    return {**counts, "correct": correct, "answer": answer, "tools": tools}


def table(document: dict, session: str, runs_file: Path) -> str:
    ids = json.loads(runs_file.read_text(encoding="utf-8"))
    questions = {q["id"]: q for q in document["questions"]}
    results = {}
    for run in plan_runs(document):
        agent = ids.get(run["run"])
        if not agent:
            raise SystemExit(f"no agent id for run {run['run']} in {runs_file}")
        transcript = PROJECT_DIR / session / "subagents" / f"agent-{agent}.jsonl"
        if not transcript.exists():
            raise SystemExit(f"no transcript for {run['run']}: {transcript}")
        results[run["run"]] = {**read_run(transcript, questions[run["question"]]["key"]), "kind": run["kind"], "variant": run["variant"], "question": run["question"]}
    lines = []
    for variant in ("A", "B"):
        rows = [r for r in results.values() if r["variant"] == variant]
        lines.append(f"Variant {variant}: runs {len(rows)}, grep-like calls {sum(r['grep_like'] for r in rows)}, drspec calls {sum(r['drspec'] for r in rows)}, "
                     f"built-in Grep/Glob calls {sum(r['builtin'] for r in rows)}, runs with zero grep-like calls {sum(1 for r in rows if r['grep_like'] == 0)}, "
                     f"correct answers {sum(1 for r in rows if r['correct'])}")
    lines.append("")
    lines.append(f"{'kind':<12}{'variant':<9}{'runs':>5}{'grep-like':>11}{'drspec':>8}{'correct':>9}")
    for kind in ("callers", "definitions", "live_text", "absence"):
        for variant in ("A", "B"):
            rows = [r for r in results.values() if r["kind"] == kind and r["variant"] == variant]
            lines.append(f"{kind:<12}{variant:<9}{len(rows):>5}{sum(r['grep_like'] for r in rows):>11}{sum(r['drspec'] for r in rows):>8}{sum(1 for r in rows if r['correct']):>9}")
    lines.append("")
    lines.append(f"{'run':<10}{'grep':>6}{'drspec':>8}{'builtin':>9}{'correct':>9}")
    for run_id, r in results.items():
        lines.append(f"{run_id:<10}{r['grep_like']:>6}{r['drspec']:>8}{r['builtin']:>9}{str(r['correct']):>9}")
    return "\n".join(lines)


def main(argv: list) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True)
    plan = sub.add_parser("plan", help="write the 40 run prompts")
    plan.add_argument("--out", type=Path, required=True)
    show = sub.add_parser("table", help="count the runs from their transcripts and print the table")
    show.add_argument("--session", required=True, help="the session id of the run's parent session (the folder under the project's .claude/projects)")
    show.add_argument("--runs", type=Path, required=True, help="JSON of run id -> sub-agent id")
    args = parser.parse_args(argv)
    document = load()
    if args.command == "plan":
        args.out.write_text(json.dumps(plan_runs(document), indent=1, ensure_ascii=False) + "\n", encoding="utf-8")
        print(f"{len(plan_runs(document))} runs written to {args.out}")
        return 0
    print(table(document, args.session, args.runs))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
