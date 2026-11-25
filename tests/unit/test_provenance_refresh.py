"""`refresh_contexts` on a publish that changes nothing: the stored fingerprints are returned as they are, none is worked out
again and none is rewritten (a forced re-scan of unchanged files used to cost time in proportion to the whole project)."""

from __future__ import annotations

import duckdb
import pytest

from drspec.core import provenance
from drspec.core.provenance import CONTEXT_KEY_PREFIX, refresh_contexts
from drspec.core.scanner import Scanner
from drspec.db import init_schema
from tests.helpers import publish

pytestmark = pytest.mark.unit


@pytest.fixture
def published(tmp_path, monkeypatch):
    """An in-memory project database with one scanned file (one small real file: the scanner reads files)."""
    monkeypatch.chdir(tmp_path)
    (tmp_path / "a.py").write_text("def foo():\n    return bar()\n\n\ndef bar():\n    return 1\n")
    conn = duckdb.connect(":memory:")
    init_schema(conn)
    result = Scanner().scan_file("a.py", relative_to=tmp_path)
    publish(conn, functions=result.functions, scanned_file_paths={"a.py"}, file_imports=result.file_imports)
    yield conn
    conn.close()


def _stored(conn):
    return dict(conn.execute("SELECT key, value FROM config WHERE starts_with(key, ?)", [CONTEXT_KEY_PREFIX]).fetchall())


def test_nothing_changed_returns_the_stored_fingerprints_without_working_any_out(published, monkeypatch):
    before = _stored(published)
    assert len(before) == 2

    def worked_out(*args, **kwargs):
        raise AssertionError("a fingerprint was worked out again")

    monkeypatch.setattr(provenance, "_contexts_of", worked_out)
    contexts = refresh_contexts(published)

    assert {CONTEXT_KEY_PREFIX + function_id: value for function_id, value in contexts.items()} == before
    assert _stored(published) == before
