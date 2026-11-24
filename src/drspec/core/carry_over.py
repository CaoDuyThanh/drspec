"""Keeping a function's contract and history when the function is renamed or its
signature changes inside the same file.

A scan sees both sides: the stored functions of a file that are no longer in its
fresh parse, and the functions that appeared. Pairing them lets the data move to
the new identifier instead of being deleted with the old one. The new function is
marked stale and its contract's confidence is lowered below the project's
threshold, so it cannot count as verified until it has been checked again.

A wrong pairing costs a re-verification, but a silent wrong one would be worse
than none, so whenever the choice is not clear the functions are left unpaired
and the old one is deleted, as before. A function moved to another file is never
paired.
"""

from __future__ import annotations

import datetime
import json
import re
from collections import Counter
from dataclasses import dataclass, field
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

import duckdb

from drspec.contracts.confidence import get_confidence_threshold
from drspec.core.identity import qualified_name
from drspec.db.queries import get_config, set_config, update_artifact_status

#: A pair that is not the same qualified name needs at least this much body in common.
RENAME_MIN_SIMILARITY = 0.6
#: ...and both bodies must have at least this many meaningful words, so that two trivial
#: functions (`pass`, `return 1`) are not taken for each other.
RENAME_MIN_WORDS = 3
#: A pair must beat every other candidate of either function by this much.
CLEAR_MARGIN = 0.1

CARRYOVER_KEY_PREFIX = "carryover:"
CARRYOVER_AGENT = "scan"

REASON_SIGNATURE = "signature_changed"
REASON_RENAMED = "renamed"
STALE_REASONS = {REASON_SIGNATURE: "SIGNATURE_CHANGED", REASON_RENAMED: "RENAMED"}

_WORD = re.compile(r"[A-Za-z_]\w*|\d+(?:\.\d+)?")
_SELF = "\x00self"
#: Words every function has whatever it does; counting them makes unrelated bodies look alike.
_NOISE = frozenset(
    "def return if else elif for while in not and or is None True False pass self cls this function const let var "
    "int double float char void auto bool long short unsigned class public private protected static async await "
    "try except catch finally raise throw new import from as with lambda yield true false null nullptr undefined "
    "break continue do switch case default".split()
)


@dataclass(frozen=True)
class Candidate:
    """One side of a possible pair."""

    function_id: str
    name: str  # the short name, which is what the body mentions
    body: str
    signature: str = ""


@dataclass(frozen=True)
class Pair:
    old_id: str
    new_id: str
    reason: str
    similarity: float


def body_tokens(candidate: Candidate) -> Counter:
    """The meaningful words of what a function does: identifiers, names and numbers in its body,
    without the signature (so a changed parameter list does not count), without the keywords
    and punctuation every function has, and with the function's own name made anonymous so a
    pure rename compares as identical."""

    def words(text: str) -> Counter:
        return Counter(_SELF if word == candidate.name else word for word in _WORD.findall(text) if word not in _NOISE)

    return words(candidate.body) - words(candidate.signature)


def similarity(a: Counter, b: Counter) -> float:
    """Dice overlap of two token multisets: 1.0 for the same tokens, 0.0 for none shared."""
    total = sum(a.values()) + sum(b.values())
    if not total:
        return 0.0
    shared = sum((a & b).values())
    return 2.0 * shared / total


def _clear_pairs(
    gone: Sequence[Candidate],
    appeared: Sequence[Candidate],
    scores: Dict[Tuple[str, str], float],
    minimum: float,
) -> List[Tuple[Candidate, Candidate, float]]:
    """One-to-one pairs in which each side's best partner is the other, strictly better
    than its next-best candidate by the margin, and at least `minimum`."""
    pairs: List[Tuple[Candidate, Candidate, float]] = []
    for old in gone:
        best = max(appeared, key=lambda new: scores[(old.function_id, new.function_id)], default=None)
        if best is None:
            continue
        score = scores[(old.function_id, best.function_id)]
        if score < minimum:
            continue
        rivals_of_old = [scores[(old.function_id, new.function_id)] for new in appeared if new is not best]
        rivals_of_new = [scores[(other.function_id, best.function_id)] for other in gone if other is not old]
        if any(score - rival < CLEAR_MARGIN for rival in rivals_of_old + rivals_of_new):
            continue
        pairs.append((old, best, score))
    return pairs


def pair_functions(gone: Sequence[Candidate], appeared: Sequence[Candidate]) -> List[Pair]:
    """Pair the functions of one file that disappeared with the ones that appeared."""
    if not gone or not appeared:
        return []
    tokens = {c.function_id: body_tokens(c) for c in list(gone) + list(appeared)}
    scores = {(old.function_id, new.function_id): similarity(tokens[old.function_id], tokens[new.function_id]) for old in gone for new in appeared}
    pairs: List[Pair] = []
    left_gone = list(gone)
    left_new = list(appeared)

    # Rule 1: the same qualified name with other parameters.
    by_name_old: Dict[str, List[Candidate]] = {}
    by_name_new: Dict[str, List[Candidate]] = {}
    for candidate in gone:
        by_name_old.setdefault(qualified_name(candidate.function_id), []).append(candidate)
    for candidate in appeared:
        by_name_new.setdefault(qualified_name(candidate.function_id), []).append(candidate)
    for name, olds in by_name_old.items():
        news = by_name_new.get(name)
        if not news:
            continue
        if len(olds) == 1 and len(news) == 1:
            chosen = [(olds[0], news[0], scores[(olds[0].function_id, news[0].function_id)])]
        else:
            chosen = _clear_pairs(olds, news, scores, minimum=0.0)
        for old, new, score in chosen:
            pairs.append(Pair(old.function_id, new.function_id, REASON_SIGNATURE, score))
            left_gone.remove(old)
            left_new.remove(new)

    # Rule 2: a rename inside the file, judged by the body alone.
    substantial_gone = [c for c in left_gone if sum(tokens[c.function_id].values()) >= RENAME_MIN_WORDS]
    substantial_new = [c for c in left_new if sum(tokens[c.function_id].values()) >= RENAME_MIN_WORDS]
    for old, new, score in _clear_pairs(substantial_gone, substantial_new, scores, minimum=RENAME_MIN_SIMILARITY):
        pairs.append(Pair(old.function_id, new.function_id, REASON_RENAMED, score))
    return pairs


# ---------------------------------------------------------------------------
# Reading what a scan is about to delete, and writing it under the new identifier
# ---------------------------------------------------------------------------


@dataclass
class Snapshot:
    """Everything stored for the old function of a pair, taken before it is deleted."""

    pair: Pair
    contract: Optional[Tuple[Any, ...]] = None  # contract_json, confidence_score, verification_script, created_at
    config: Dict[str, str] = field(default_factory=dict)
    previous: Optional[Dict[str, Any]] = None  # the old function's own carry-over record, if it was itself carried
    traces: List[Tuple[Any, ...]] = field(default_factory=list)  # agent, trace_json, created_at
    findings: List[Tuple[Any, ...]] = field(default_factory=list)

    def has_data(self) -> bool:
        return bool(self.contract or self.traces or self.findings)


_CONFIG_PREFIXES = ("contract:source:", "contract:context:", "verification:")
_FINDING_COLUMNS = ("finding_type", "significance", "description", "location", "invariant_implication", "status", "resolution_note", "plot_path", "created_at")


def plan_pairs(conn: duckdb.DuckDBPyConnection, scanned_file_paths: Iterable[str], functions_by_file: Dict[str, list]) -> List[Pair]:
    """The pairs to carry over in this scan: per scanned file, stored functions missing
    from the fresh parse matched with fresh functions not stored yet."""
    paths = sorted(set(scanned_file_paths))
    stored_ids: Dict[str, set] = {path: set() for path in paths}
    for start in range(0, len(paths), 500):
        chunk = paths[start : start + 500]
        marks = ", ".join("?" for _ in chunk)
        for function_id, file_path in conn.execute(f"SELECT function_id, file_path FROM artifacts WHERE file_path IN ({marks})", chunk).fetchall():
            stored_ids[file_path].add(function_id)

    pairs: List[Pair] = []
    for path in paths:
        fresh = {f.function_id: f for f in functions_by_file.get(path, [])}
        gone_ids = sorted(stored_ids[path] - fresh.keys())
        appeared = [f for function_id, f in fresh.items() if function_id not in stored_ids[path]]
        if not gone_ids or not appeared:
            continue
        marks = ", ".join("?" for _ in gone_ids)
        rows = conn.execute(f"SELECT function_id, function_name, body, signature FROM artifacts WHERE function_id IN ({marks})", gone_ids).fetchall()
        gone = [Candidate(function_id, name, body, signature) for function_id, name, body, signature in rows]
        new = [Candidate(f.function_id, f.name, f.body, f.signature) for f in appeared]
        pairs.extend(pair_functions(gone, new))
    return pairs


def take_snapshots(conn: duckdb.DuckDBPyConnection, pairs: Sequence[Pair]) -> List[Snapshot]:
    """Read what is stored for each pair's old function; pairs with nothing to carry are dropped."""
    snapshots: List[Snapshot] = []
    for pair in pairs:
        snapshot = Snapshot(pair)
        snapshot.contract = conn.execute(
            "SELECT contract_json, confidence_score, verification_script, created_at FROM contracts WHERE function_id = ?", [pair.old_id]
        ).fetchone()
        snapshot.traces = conn.execute(
            "SELECT agent, trace_json, created_at FROM reasoning_traces WHERE function_id = ? ORDER BY id", [pair.old_id]
        ).fetchall()
        snapshot.findings = conn.execute(
            f"SELECT {', '.join(_FINDING_COLUMNS)} FROM vision_findings WHERE function_id = ? ORDER BY id", [pair.old_id]
        ).fetchall()
        snapshot.previous = carry_over_record(conn, pair.old_id)
        for prefix in _CONFIG_PREFIXES:
            value = get_config(conn, prefix + pair.old_id)
            if value is not None:
                snapshot.config[prefix] = value
        if snapshot.has_data():
            snapshots.append(snapshot)
    return snapshots


def _repoint_unconstrained_rows(conn: duckdb.DuckDBPyConnection, pair: Pair) -> None:
    """Tables nothing references move with a plain update: the learning history and the usage score."""
    try:
        conn.execute("UPDATE learning_history SET function_id = ? WHERE function_id = ?", [pair.new_id, pair.old_id])
    except duckdb.CatalogException:
        pass  # the history is created when first used
    conn.execute("DELETE FROM usage_stats WHERE kind = 'node' AND item_id = ? AND related_id = ''", [pair.new_id])
    conn.execute("UPDATE usage_stats SET item_id = ? WHERE kind = 'node' AND item_id = ? AND related_id = ''", [pair.new_id, pair.old_id])


def restore_snapshots(conn: duckdb.DuckDBPyConnection, snapshots: Sequence[Snapshot]) -> Dict[str, Pair]:
    """Write each snapshot under the new identifier. The new function must already be stored
    and the old one deleted. Returns the new identifiers carried to, with their pairs."""
    if not snapshots:
        return {}
    threshold = get_confidence_threshold(conn)
    ceiling = (threshold - 1) / 100.0
    carried: Dict[str, Pair] = {}
    for snapshot in snapshots:
        pair = snapshot.pair
        original: Optional[int] = None
        if snapshot.contract is not None:
            contract_json, score, script, created_at = snapshot.contract
            score = float(score or 0.0)
            original = int(round(score if score > 1.0 else score * 100))
            if snapshot.previous and snapshot.previous.get("original_confidence") is not None:
                # Carried again before anyone re-checked it: the stored value is already the lowered
                # one, so the confidence it had before the first move is the one to remember.
                original = int(snapshot.previous["original_confidence"])
            lowered = min(score, ceiling) if score <= 1.0 else min(score, threshold - 1)
            conn.execute(
                "INSERT INTO contracts (function_id, contract_json, confidence_score, verification_script, created_at, updated_at) VALUES (?, ?, ?, ?, ?, now())",
                [pair.new_id, contract_json, lowered, script, created_at],
            )
            for prefix, value in snapshot.config.items():
                set_config(conn, prefix + pair.new_id, value)
        for agent, trace_json, created_at in snapshot.traces:
            conn.execute("INSERT INTO reasoning_traces (function_id, agent, trace_json, created_at) VALUES (?, ?, ?, ?)", [pair.new_id, agent, trace_json, created_at])
        for finding in snapshot.findings:
            conn.execute(
                f"INSERT INTO vision_findings (function_id, {', '.join(_FINDING_COLUMNS)}) VALUES (?, {', '.join('?' for _ in _FINDING_COLUMNS)})",
                [pair.new_id, *finding],
            )
        _repoint_unconstrained_rows(conn, pair)
        record = {
            "from": pair.old_id,
            "reason": pair.reason,
            "original_confidence": original,
            "lowered_confidence": None if snapshot.contract is None else int(round(lowered * 100 if lowered <= 1.0 else lowered)),
            "similarity": round(pair.similarity, 3),
            "earlier": ((snapshot.previous or {}).get("earlier") or []) + ([snapshot.previous["from"]] if snapshot.previous else []),
            "at": datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds"),
        }
        set_config(conn, CARRYOVER_KEY_PREFIX + pair.new_id, json.dumps(record))
        conn.execute(
            "INSERT INTO reasoning_traces (function_id, agent, trace_json) VALUES (?, ?, ?)",
            [pair.new_id, CARRYOVER_AGENT, json.dumps({"event": "carried_over", **record})],
        )
        update_artifact_status(conn, pair.new_id, "STALE")
        set_config(conn, "artifact:stale_reason:" + pair.new_id, STALE_REASONS[pair.reason])
        carried[pair.new_id] = pair
    return carried


def carry_over_record(conn: duckdb.DuckDBPyConnection, function_id: str) -> Optional[Dict[str, Any]]:
    """What was recorded when this function received another one's contract, if it did."""
    raw = get_config(conn, CARRYOVER_KEY_PREFIX + function_id)
    return json.loads(raw) if raw else None
