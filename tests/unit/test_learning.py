"""Tests for the learning module."""

from datetime import datetime
from pathlib import Path

import duckdb
import pytest

from drspec.contracts.schema import Contract, Criticality, Invariant, OnFail
from drspec.learning.diff import (
    CommitDiff,
    DiffHunk,
    FileDiff,
    _detect_bug_fix,
    analyze_commit,
    get_modified_functions,
    parse_unified_diff,
)
from drspec.learning.history import (
    LearningEvent,
    get_learning_history,
    get_learning_stats,
    init_learning_schema,
    insert_learning_event,
)
from drspec.learning.patterns import (
    ExtractedPattern,
    PatternType,
    categorize_pattern,
    extract_patterns_from_diff,
    generate_pattern_description,
)
from drspec.learning.strengthening import (
    match_pattern_to_contract,
    strengthen_contract,
    suggest_invariants,
)
from tests.fakes import FakeGit
from tests.helpers import fid


class TestDiffHunk:
    """Tests for DiffHunk dataclass."""

    def test_removed_lines(self):
        hunk = DiffHunk(
            old_start=1, old_count=3, new_start=1, new_count=4, header=""
        )
        hunk.lines = [
            " context",
            "-removed1",
            "-removed2",
            "+added",
            " context2",
        ]

        assert hunk.removed_lines == ["removed1", "removed2"]

    def test_added_lines(self):
        hunk = DiffHunk(
            old_start=1, old_count=3, new_start=1, new_count=4, header=""
        )
        hunk.lines = [
            " context",
            "-removed",
            "+added1",
            "+added2",
            " context2",
        ]

        assert hunk.added_lines == ["added1", "added2"]

    def test_to_dict(self):
        hunk = DiffHunk(
            old_start=1, old_count=2, new_start=1, new_count=3, header="function"
        )
        hunk.lines = ["-old", "+new"]

        result = hunk.to_dict()

        assert result["old_start"] == 1
        assert result["new_start"] == 1
        assert result["header"] == "function"


class TestFileDiff:
    """Tests for FileDiff dataclass."""

    def test_path_property(self):
        diff = FileDiff(old_path="old.py", new_path="new.py")
        assert diff.path == "new.py"

    def test_path_property_deleted(self):
        diff = FileDiff(old_path="deleted.py", new_path="", is_deleted=True)
        assert diff.path == "deleted.py"

    def test_all_removed_lines(self):
        diff = FileDiff(old_path="test.py", new_path="test.py")
        hunk1 = DiffHunk(1, 1, 1, 1, "")
        hunk1.lines = ["-line1"]
        hunk2 = DiffHunk(10, 1, 10, 1, "")
        hunk2.lines = ["-line2"]
        diff.hunks = [hunk1, hunk2]

        assert diff.all_removed_lines == ["line1", "line2"]


class TestParseUnifiedDiff:
    """Tests for parse_unified_diff function."""

    def test_parse_simple_diff(self):
        diff_text = """diff --git a/test.py b/test.py
--- a/test.py
+++ b/test.py
@@ -1,3 +1,4 @@
 line1
+new line
 line2
 line3
"""
        files = parse_unified_diff(diff_text)

        assert len(files) == 1
        assert files[0].old_path == "test.py"
        assert files[0].new_path == "test.py"
        assert len(files[0].hunks) == 1
        assert files[0].hunks[0].old_start == 1
        assert files[0].hunks[0].new_start == 1

    def test_parse_new_file(self):
        diff_text = """diff --git a/new.py b/new.py
--- /dev/null
+++ b/new.py
@@ -0,0 +1,3 @@
+line1
+line2
+line3
"""
        files = parse_unified_diff(diff_text)

        assert len(files) == 1
        assert files[0].is_new is True

    def test_parse_deleted_file(self):
        diff_text = """diff --git a/old.py b/old.py
--- a/old.py
+++ /dev/null
@@ -1,3 +0,0 @@
-line1
-line2
-line3
"""
        files = parse_unified_diff(diff_text)

        assert len(files) == 1
        assert files[0].is_deleted is True

    def test_parse_multiple_files(self):
        diff_text = """diff --git a/file1.py b/file1.py
--- a/file1.py
+++ b/file1.py
@@ -1,1 +1,1 @@
-old
+new
diff --git a/file2.py b/file2.py
--- a/file2.py
+++ b/file2.py
@@ -1,1 +1,1 @@
-old2
+new2
"""
        files = parse_unified_diff(diff_text)

        assert len(files) == 2
        assert files[0].path == "file1.py"
        assert files[1].path == "file2.py"


class TestGetModifiedFunctions:
    """Had zero test coverage, which is how a real bug (calling `parser.extract_functions()`, which doesn't exist — the
    real method is `.parse().functions`) went unnoticed. The repository is data (`tests.fakes.FakeGit`): it answers
    the `git log`, `git diff` and `git show` that `analyze_commit` and `get_modified_functions` run, with the hunks
    and context lines git prints, and no `git` is started."""

    REPO = "/fake/repo"

    def _init_repo_with_one_commit(self, monkeypatch):
        from drspec.learning import diff as diff_module

        git = FakeGit().install(monkeypatch, diff_module)
        sha = git.commit({"a.py": "def foo():\n    return 1\n\n\ndef bar():\n    return 2\n"}, message="initial")
        return git, sha

    def _commit_change(self, monkeypatch, before, after):
        """A repository with `before` committed, then `after`; returns the second commit as `analyze_commit` parses it
        (the hunks and context lines of a unified diff)."""
        git, _ = self._init_repo_with_one_commit(monkeypatch)
        git.commit({"a.py": before}, message="before")
        sha = git.commit({"a.py": after}, message="after")
        return self.REPO, analyze_commit(sha, self.REPO)

    ADD_AND_MUL = "def add(a, b):\n    return a + b\n\n\ndef mul(a, b):\n    return a * b\n"

    def test_only_the_function_with_a_changed_line_is_credited(self, monkeypatch):
        """`mul` sits inside `add`'s three-line context window; unchanged
        context lines must not count as a modification."""
        repo, commit = self._commit_change(
            monkeypatch, self.ADD_AND_MUL, self.ADD_AND_MUL.replace("a + b", "a + b + 0"),
        )

        assert get_modified_functions(commit, str(repo)) == [(fid("a.py", "add", "a", "b"), "a.py", "add")]

    def test_a_change_on_the_line_after_a_function_is_not_credited_to_it(self, monkeypatch):
        repo, commit = self._commit_change(
            monkeypatch, self.ADD_AND_MUL, self.ADD_AND_MUL.replace("def mul(a, b):", "def mul(a, b, c=1):"),
        )

        assert [name for _, _, name in get_modified_functions(commit, str(repo))] == ["mul"]

    def test_a_pure_deletion_inside_a_function_credits_that_function(self, monkeypatch):
        before = "def foo():\n    x = 1\n    y = 2\n    return x + y\n\n\ndef bar():\n    return 2\n"
        repo, commit = self._commit_change(monkeypatch, before, before.replace("    y = 2\n", ""))

        assert [name for _, _, name in get_modified_functions(commit, str(repo))] == ["foo"]

    def test_deleting_a_whole_function_credits_no_surviving_function(self, monkeypatch):
        before = self.ADD_AND_MUL
        repo, commit = self._commit_change(monkeypatch, before, before.split("\n\n\n")[0] + "\n")

        assert get_modified_functions(commit, str(repo)) == []

    def test_non_python_files_are_skipped(self, monkeypatch):
        _, sha = self._init_repo_with_one_commit(monkeypatch)
        commit = CommitDiff(
            commit_sha=sha, author="Test", author_email="t@example.com",
            date=datetime.now(), message="initial",
            files=[FileDiff(old_path="README.md", new_path="README.md", hunks=[
                DiffHunk(old_start=1, old_count=1, new_start=1, new_count=1, header="")
            ])],
        )

        assert get_modified_functions(commit, repo_path=self.REPO) == []


class TestDetectBugFix:
    """Tests for bug fix detection."""

    def test_detect_fix_keyword(self):
        is_bug, conf, refs = _detect_bug_fix("Fix null pointer exception")
        assert is_bug is True
        assert conf > 0.3

    def test_detect_bug_keyword(self):
        is_bug, conf, refs = _detect_bug_fix("Bug: handle empty input")
        assert is_bug is True

    def test_detect_issue_reference(self):
        is_bug, conf, refs = _detect_bug_fix("Fixes #123")
        assert is_bug is True
        assert "#123" in refs

    def test_detect_jira_reference(self):
        is_bug, conf, refs = _detect_bug_fix("PROJ-456: Fix login issue")
        assert is_bug is True
        assert "PROJ-456" in refs

    def test_not_bug_fix(self):
        is_bug, conf, refs = _detect_bug_fix("Add new feature")
        assert is_bug is False
        assert conf < 0.3


class TestPatternCategorization:
    """Tests for pattern categorization."""

    def test_null_check_pattern(self):
        removed = ["return data"]
        added = ["if data is None:", "    return None", "return data"]

        pattern_type, conf = categorize_pattern(removed, added)

        assert pattern_type == PatternType.NULL_CHECK

    def test_bounds_check_pattern(self):
        removed = ["item = items[index]"]
        added = ["if index < len(items):", "    item = items[index]"]

        pattern_type, conf = categorize_pattern(removed, added)

        assert pattern_type == PatternType.BOUNDS_CHECK

    def test_type_check_pattern(self):
        removed = ["process(data)"]
        added = ["if isinstance(data, dict):", "    process(data)"]

        pattern_type, conf = categorize_pattern(removed, added)

        assert pattern_type == PatternType.TYPE_CHECK

    def test_unknown_pattern(self):
        removed = ["x = 1"]
        added = ["x = 2"]

        pattern_type, conf = categorize_pattern(removed, added)

        assert pattern_type == PatternType.UNKNOWN


class TestPatternDescription:
    """Tests for pattern description generation."""

    def test_null_check_description(self):
        desc = generate_pattern_description(
            PatternType.NULL_CHECK,
            ["return data"],
            ["if data is None:", "return None"],
        )

        assert "null" in desc.lower() or "none" in desc.lower()

    def test_bounds_check_description(self):
        desc = generate_pattern_description(
            PatternType.BOUNDS_CHECK,
            ["item = items[i]"],
            ["if len(items) > 0:", "item = items[i]"],
        )

        assert "bounds" in desc.lower() or "length" in desc.lower()


class TestExtractPatternsFromDiff:
    """Tests for pattern extraction from diffs."""

    def test_extract_single_pattern(self):
        diff = FileDiff(old_path="test.py", new_path="test.py")
        hunk = DiffHunk(1, 2, 1, 4, "def process()")
        hunk.lines = [
            " def process(data):",
            "-    return data.value",
            "+    if data is None:",
            "+        return None",
            "+    return data.value",
        ]
        diff.hunks = [hunk]

        patterns = extract_patterns_from_diff(diff, "process")

        assert len(patterns) == 1
        assert patterns[0].pattern_type == PatternType.NULL_CHECK
        assert patterns[0].function_name == "process"


class TestContractStrengthening:
    """Tests for contract strengthening."""

    def test_match_pattern_to_contract(self):
        pattern = ExtractedPattern(
            pattern_type=PatternType.NULL_CHECK,
            description="Added null check",
            code_before="return data",
            code_after="if data is None: return None",
            file_path="test.py",
        )

        contract = Contract(
            function_signature="def process(data):",
            intent_summary="Process the data",
            invariants=[
                Invariant(
                    name="not_none",
                    logic="Output is not None when input is valid",
                    criticality=Criticality.HIGH,
                    on_fail=OnFail.ERROR,
                ),
            ],
        )

        matches = match_pattern_to_contract(pattern, contract)

        assert "not_none" in matches

    def test_suggest_invariants(self):
        pattern = ExtractedPattern(
            pattern_type=PatternType.BOUNDS_CHECK,
            description="Added bounds check",
            code_before="item = items[i]",
            code_after="if i < len(items): item = items[i]",
            file_path="test.py",
            invariant_suggestions=["Index must be within bounds"],
        )

        suggestions = suggest_invariants(pattern)

        assert len(suggestions) >= 1
        assert suggestions[0].source_pattern == PatternType.BOUNDS_CHECK

    def test_strengthen_contract(self):
        patterns = [
            ExtractedPattern(
                pattern_type=PatternType.NULL_CHECK,
                description="Added null check",
                code_before="return x",
                code_after="if x is None: return None",
                file_path="test.py",
                confidence=0.8,
                invariant_suggestions=["Value must not be None"],
            ),
        ]

        result = strengthen_contract(
            function_id="test.py::foo",
            patterns=patterns,
            existing_contract=None,
        )

        assert result.function_id == "test.py::foo"
        assert len(result.new_invariants) >= 1
        assert len(result.recommendations) > 0


class TestLearningHistory:
    """Tests for learning history storage."""

    @pytest.fixture
    def db_conn(self, tmp_path):
        tmpdir = str(tmp_path)
        db_path = Path(tmpdir) / "test.db"
        conn = duckdb.connect(str(db_path))
        init_learning_schema(conn)
        yield conn
        conn.close()

    def test_insert_and_get_event(self, db_conn):
        event = LearningEvent(
            commit_sha="abc123def456",
            function_id="src/test.py::foo",
            pattern_type=PatternType.NULL_CHECK,
            pattern_description="Added null check",
            commit_message="Fix null pointer",
            invariants_validated=1,
        )

        event_id = insert_learning_event(db_conn, event)
        assert event_id > 0

        events = get_learning_history(db_conn, function_id="src/test.py::foo")
        assert len(events) == 1
        assert events[0].pattern_type == PatternType.NULL_CHECK

    def test_get_learning_stats(self, db_conn):
        for i in range(3):
            event = LearningEvent(
                commit_sha=f"commit{i}",
                function_id=f"test.py::func{i}",
                pattern_type=PatternType.NULL_CHECK,
                invariants_validated=1,
            )
            insert_learning_event(db_conn, event)

        stats = get_learning_stats(db_conn)

        assert stats["total_events"] == 3
        assert stats["unique_commits"] == 3
        assert stats["unique_functions"] == 3

    def test_event_to_dict(self):
        event = LearningEvent(
            commit_sha="abc123",
            function_id="test.py::foo",
            pattern_type=PatternType.BOUNDS_CHECK,
            pattern_description="Added bounds check",
            created_at=datetime.now(),
        )

        result = event.to_dict()

        assert result["commit_sha"] == "abc123"
        assert result["pattern_type"] == "bounds_check"


class TestModuleImports:
    """Tests for module exports."""

    def test_all_exports_from_learning(self):
        from drspec import learning

        # Diff
        assert hasattr(learning, "DiffHunk")
        assert hasattr(learning, "FileDiff")
        assert hasattr(learning, "CommitDiff")
        assert hasattr(learning, "parse_unified_diff")
        assert hasattr(learning, "analyze_commit_range")

        # Patterns
        assert hasattr(learning, "PatternType")
        assert hasattr(learning, "ExtractedPattern")
        assert hasattr(learning, "extract_patterns_from_diff")

        # Strengthening
        assert hasattr(learning, "ContractStrengthening")
        assert hasattr(learning, "strengthen_contract")

        # History
        assert hasattr(learning, "LearningEvent")
        assert hasattr(learning, "insert_learning_event")
        assert hasattr(learning, "get_learning_history")
