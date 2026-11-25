"""Git diff analysis for bug-driven learning."""

from __future__ import annotations

import re
import subprocess
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Dict, Iterable, List, Optional, Set, Tuple

BUG_FIX_KEYWORDS = frozenset([
    "fix", "fixed", "fixes", "fixing",
    "bug", "bugfix", "hotfix",
    "patch", "patched",
    "repair", "repaired",
    "resolve", "resolved", "resolves",
    "issue", "closes", "closed",
    "error", "crash", "failure",
    "broken", "broke",
])

# Issue reference patterns (GitHub, GitLab, Jira, etc.)
ISSUE_PATTERNS = [
    r"#\d+",                           # GitHub/GitLab: #123
    r"GH-\d+",                         # GitHub: GH-123
    r"[A-Z]{2,}-\d+",                  # Jira: PROJ-123
    r"fixes?\s+#\d+",                  # fixes #123
    r"closes?\s+#\d+",                 # closes #123
    r"resolves?\s+#\d+",               # resolves #123
]


@dataclass
class DiffHunk:
    """A single hunk (change block) in a diff.

    Attributes:
        old_start: Starting line in old file.
        old_count: Number of lines in old file.
        new_start: Starting line in new file.
        new_count: Number of lines in new file.
        header: The hunk header line.
        lines: List of diff lines (with +/- prefix).
        removed_lines: Lines that were removed.
        added_lines: Lines that were added.
        context_lines: Unchanged context lines.
    """

    old_start: int
    old_count: int
    new_start: int
    new_count: int
    header: str
    lines: List[str] = field(default_factory=list)

    @property
    def removed_lines(self) -> List[str]:
        """Get lines that were removed (- prefix)."""
        return [line[1:] for line in self.lines if line.startswith("-")]

    @property
    def added_lines(self) -> List[str]:
        """Get lines that were added (+ prefix)."""
        return [line[1:] for line in self.lines if line.startswith("+")]

    @property
    def context_lines(self) -> List[str]:
        """Get unchanged context lines (space prefix)."""
        return [line[1:] for line in self.lines if line.startswith(" ")]

    def to_dict(self) -> Dict[str, Any]:
        """Convert to dictionary."""
        return {
            "old_start": self.old_start,
            "old_count": self.old_count,
            "new_start": self.new_start,
            "new_count": self.new_count,
            "header": self.header,
            "removed_lines": self.removed_lines,
            "added_lines": self.added_lines,
        }


@dataclass
class FileDiff:
    """Diff for a single file.

    Attributes:
        old_path: Path in old version (or /dev/null for new files).
        new_path: Path in new version (or /dev/null for deleted files).
        hunks: List of change hunks.
        is_new: True if file was created.
        is_deleted: True if file was deleted.
        is_renamed: True if file was renamed.
    """

    old_path: str
    new_path: str
    hunks: List[DiffHunk] = field(default_factory=list)
    is_new: bool = False
    is_deleted: bool = False
    is_renamed: bool = False

    @property
    def path(self) -> str:
        """Get the effective file path."""
        if self.is_deleted:
            return self.old_path
        return self.new_path

    @property
    def all_removed_lines(self) -> List[str]:
        """Get all removed lines across all hunks."""
        result = []
        for hunk in self.hunks:
            result.extend(hunk.removed_lines)
        return result

    @property
    def all_added_lines(self) -> List[str]:
        """Get all added lines across all hunks."""
        result = []
        for hunk in self.hunks:
            result.extend(hunk.added_lines)
        return result

    def to_dict(self) -> Dict[str, Any]:
        """Convert to dictionary."""
        return {
            "old_path": self.old_path,
            "new_path": self.new_path,
            "path": self.path,
            "is_new": self.is_new,
            "is_deleted": self.is_deleted,
            "is_renamed": self.is_renamed,
            "hunks": [h.to_dict() for h in self.hunks],
        }


@dataclass
class CommitDiff:
    """Complete diff for a git commit.

    Attributes:
        commit_sha: Full commit SHA.
        short_sha: Short commit SHA (7 chars).
        author: Commit author name.
        author_email: Commit author email.
        date: Commit date.
        message: Commit message.
        files: List of file diffs.
        is_bug_fix: Whether this appears to be a bug-fix commit.
        issue_refs: Referenced issue numbers.
    """

    commit_sha: str
    author: str
    author_email: str
    date: datetime
    message: str
    files: List[FileDiff] = field(default_factory=list)
    is_bug_fix: bool = False
    issue_refs: List[str] = field(default_factory=list)

    @property
    def short_sha(self) -> str:
        """Get short commit SHA."""
        return self.commit_sha[:7]

    def to_dict(self) -> Dict[str, Any]:
        """Convert to dictionary."""
        return {
            "commit_sha": self.commit_sha,
            "short_sha": self.short_sha,
            "author": self.author,
            "author_email": self.author_email,
            "date": self.date.isoformat(),
            "message": self.message,
            "is_bug_fix": self.is_bug_fix,
            "issue_refs": self.issue_refs,
            "files": [f.to_dict() for f in self.files],
        }


@dataclass
class DiffAnalysis:
    """Analysis result for a diff.

    Attributes:
        commit: The analyzed commit.
        modified_functions: List of (function_id, file_path, function_name) tuples.
        affected_lines: Map of function_id to affected line ranges.
        bug_fix_confidence: Confidence that this is a bug fix (0-1).
    """

    commit: CommitDiff
    modified_functions: List[Tuple[str, str, str]] = field(default_factory=list)
    affected_lines: Dict[str, List[Tuple[int, int]]] = field(default_factory=dict)
    bug_fix_confidence: float = 0.0

    def to_dict(self) -> Dict[str, Any]:
        """Convert to dictionary."""
        return {
            "commit": self.commit.to_dict(),
            "modified_functions": [
                {"function_id": fid, "file_path": fp, "function_name": fn}
                for fid, fp, fn in self.modified_functions
            ],
            "affected_lines": self.affected_lines,
            "bug_fix_confidence": self.bug_fix_confidence,
        }


def parse_unified_diff(diff_text: str) -> List[FileDiff]:
    """Parse unified diff format into FileDiff objects.

    Args:
        diff_text: Raw unified diff text.

    Returns:
        List of FileDiff objects.

    Example:
        >>> diffs = parse_unified_diff('''
        ... diff --git a/foo.py b/foo.py
        ... --- a/foo.py
        ... +++ b/foo.py
        ... @@ -1,3 +1,4 @@
        ...  line1
        ... +new line
        ...  line2
        ... ''')
        >>> len(diffs)
        1
    """
    files: List[FileDiff] = []
    current_file: Optional[FileDiff] = None
    current_hunk: Optional[DiffHunk] = None

    lines = diff_text.split("\n")
    i = 0

    while i < len(lines):
        line = lines[i]

        if line.startswith("diff --git"):
            if current_file is not None:
                if current_hunk is not None:
                    current_file.hunks.append(current_hunk)
                files.append(current_file)

            match = re.match(r"diff --git a/(.*) b/(.*)", line)
            if match:
                old_path = match.group(1)
                new_path = match.group(2)
                current_file = FileDiff(old_path=old_path, new_path=new_path)
            else:
                current_file = FileDiff(old_path="", new_path="")
            current_hunk = None
            i += 1
            continue

        if line.startswith("--- ") and current_file is not None:
            path = line[4:]
            if path.startswith("a/"):
                current_file.old_path = path[2:]
            elif path == "/dev/null":
                current_file.is_new = True
            i += 1
            continue

        if line.startswith("+++ ") and current_file is not None:
            path = line[4:]
            if path.startswith("b/"):
                current_file.new_path = path[2:]
            elif path == "/dev/null":
                current_file.is_deleted = True
            i += 1
            continue

        if line.startswith("@@") and current_file is not None:
            if current_hunk is not None:
                current_file.hunks.append(current_hunk)

            # Parse "@@ -old_start,old_count +new_start,new_count @@ context"
            match = re.match(r"@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@(.*)", line)
            if match:
                old_start = int(match.group(1))
                old_count = int(match.group(2) or "1")
                new_start = int(match.group(3))
                new_count = int(match.group(4) or "1")
                header = match.group(5).strip()
                current_hunk = DiffHunk(
                    old_start=old_start,
                    old_count=old_count,
                    new_start=new_start,
                    new_count=new_count,
                    header=header,
                )
            i += 1
            continue

        if current_hunk is not None and (
            line.startswith("+") or line.startswith("-") or line.startswith(" ")
        ):
            current_hunk.lines.append(line)
            i += 1
            continue

        i += 1

    if current_file is not None:
        if current_hunk is not None:
            current_file.hunks.append(current_hunk)
        files.append(current_file)

    return files


def _detect_bug_fix(message: str) -> Tuple[bool, float, List[str]]:
    """Detect if a commit message indicates a bug fix.

    Args:
        message: Commit message.

    Returns:
        Tuple of (is_bug_fix, confidence, issue_refs).
    """
    message_lower = message.lower()
    confidence = 0.0
    issue_refs: List[str] = []

    keywords_found = 0
    for keyword in BUG_FIX_KEYWORDS:
        if keyword in message_lower:
            keywords_found += 1

    if keywords_found > 0:
        confidence += min(0.5, keywords_found * 0.2)

    for pattern in ISSUE_PATTERNS:
        matches = re.findall(pattern, message, re.IGNORECASE)
        issue_refs.extend(matches)

    if issue_refs:
        confidence += 0.3

    if message_lower.startswith("fix"):
        confidence += 0.2

    # "bug"/"error" are stronger bug-fix signals than the generic keyword list
    if "bug" in message_lower or "error" in message_lower:
        confidence += 0.15

    is_bug_fix = confidence >= 0.3
    confidence = min(1.0, confidence)

    return is_bug_fix, confidence, list(set(issue_refs))


def analyze_commit(
    commit_sha: str,
    repo_path: str = ".",
) -> CommitDiff:
    """Analyze a single git commit.

    Args:
        commit_sha: Commit SHA to analyze.
        repo_path: Path to git repository.

    Returns:
        CommitDiff with parsed information.

    Raises:
        subprocess.CalledProcessError: If git command fails.
    """
    result = subprocess.run(
        [
            "git", "-C", repo_path, "log", "-1",
            "--format=%H%n%an%n%ae%n%aI%n%B",
            commit_sha,
        ],
        capture_output=True,
        text=True,
        check=True,
    )

    lines = result.stdout.strip().split("\n")
    sha = lines[0]
    author = lines[1]
    email = lines[2]
    date_str = lines[3]
    message = "\n".join(lines[4:])

    date = datetime.fromisoformat(date_str.replace("Z", "+00:00"))

    diff_result = subprocess.run(
        ["git", "-C", repo_path, "diff", f"{commit_sha}^..{commit_sha}"],
        capture_output=True,
        text=True,
        check=True,
    )

    files = parse_unified_diff(diff_result.stdout)

    is_bug_fix, confidence, issue_refs = _detect_bug_fix(message)

    return CommitDiff(
        commit_sha=sha,
        author=author,
        author_email=email,
        date=date,
        message=message,
        files=files,
        is_bug_fix=is_bug_fix,
        issue_refs=issue_refs,
    )


def analyze_commit_range(
    start_ref: str,
    end_ref: str = "HEAD",
    repo_path: str = ".",
    bug_fixes_only: bool = False,
) -> List[DiffAnalysis]:
    """Analyze a range of git commits.

    Args:
        start_ref: Starting commit reference (exclusive).
        end_ref: Ending commit reference (inclusive).
        repo_path: Path to git repository.
        bug_fixes_only: If True, only analyze bug-fix commits.

    Returns:
        List of DiffAnalysis objects.

    Example:
        >>> analyses = analyze_commit_range("HEAD~10", "HEAD")
        >>> bug_fixes = [a for a in analyses if a.commit.is_bug_fix]
    """
    result = subprocess.run(
        [
            "git", "-C", repo_path, "log",
            "--format=%H",
            f"{start_ref}..{end_ref}",
        ],
        capture_output=True,
        text=True,
        check=True,
    )

    commit_shas = result.stdout.strip().split("\n")
    commit_shas = [sha for sha in commit_shas if sha]

    analyses: List[DiffAnalysis] = []

    for sha in commit_shas:
        try:
            commit = analyze_commit(sha, repo_path)

            if bug_fixes_only and not commit.is_bug_fix:
                continue

            is_bug_fix, confidence, _ = _detect_bug_fix(commit.message)

            analysis = DiffAnalysis(
                commit=commit,
                bug_fix_confidence=confidence,
            )

            modified = get_modified_functions(commit, repo_path)
            analysis.modified_functions = modified

            analyses.append(analysis)

        except subprocess.CalledProcessError:
            # Skip commits that can't be analyzed (e.g., initial commit)
            continue

    return analyses


def _git_show(repo_path: str, ref: str, path: str) -> Optional[str]:
    """File content at `ref`, or `None` if it doesn't exist there."""
    try:
        result = subprocess.run(
            ["git", "-C", repo_path, "show", f"{ref}:{path}"],
            capture_output=True,
            text=True,
            check=True,
        )
    except subprocess.CalledProcessError:
        return None
    return result.stdout


def _changed_lines(hunks: Iterable[DiffHunk]) -> Tuple[Set[int], Set[int]]:
    """(added line numbers in the new file, removed line numbers in the
    old file). A hunk's own range can't be used for this: it includes the
    unchanged context lines around the change."""
    added: Set[int] = set()
    removed: Set[int] = set()
    for hunk in hunks:
        old_line, new_line = hunk.old_start, hunk.new_start
        for line in hunk.lines:
            if line.startswith("+"):
                added.add(new_line)
                new_line += 1
            elif line.startswith("-"):
                removed.add(old_line)
                old_line += 1
            else:
                old_line += 1
                new_line += 1
    return added, removed


def _touches(start_line: int, end_line: int, lines: Set[int]) -> bool:
    return any(start_line <= n <= end_line for n in lines)


def get_modified_functions(
    commit: CommitDiff,
    repo_path: str = ".",
) -> List[Tuple[str, str, str]]:
    """Identify functions modified in a commit.

    A function counts as modified if the commit added a line inside it,
    or removed a line from the same-named function in the parent version.
    Unchanged context lines next to a change don't count.

    Args:
        commit: Commit diff to analyze.
        repo_path: Path to git repository.

    Returns:
        List of (function_id, file_path, function_name) tuples.
    """
    from drspec.core.identity import function_identity
    from drspec.parsers import PythonParser  # local import avoids a circular import

    modified: List[Tuple[str, str, str]] = []

    for file_diff in commit.files:
        if not file_diff.path.endswith(".py"):  # only Python is supported so far
            continue

        content = _git_show(repo_path, commit.commit_sha, file_diff.path)
        if content is None:
            continue

        parser = PythonParser()
        added, removed = _changed_lines(file_diff.hunks)

        names_with_removals: Set[str] = set()
        if removed:
            old_content = _git_show(repo_path, f"{commit.commit_sha}^", file_diff.path)
            if old_content is not None:
                names_with_removals = {
                    f.qualified_name for f in parser.parse(old_content).functions
                    if _touches(f.start_line, f.end_line, removed)
                }

        for func in parser.parse(content).functions:
            if _touches(func.start_line, func.end_line, added) or func.qualified_name in names_with_removals:
                function_id = function_identity(file_diff.path, func.qualified_name, func.parameter_tokens)
                modified.append((function_id, file_diff.path, func.name))

    return modified
