"""Regression coverage for follow-up findings."""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import patch

import pytest
from typer.testing import CliRunner

from drspec.cli.app import app
from drspec.core.hasher import compute_hash
from drspec.db import get_connection
from tests.helpers import fid


@pytest.fixture
def project(kept_project, in_process_python):
    """A project whose database is one in-memory DuckDB kept between the commands (these tests check what the commands do to the index,
    not that `init` runs or that the index reaches a file)."""
    return CliRunner()


def run(runner, *args, **kwargs):
    result = runner.invoke(app, ['--json', *args], **kwargs)
    assert result.exit_code == 0, result.output
    return json.loads(result.output)['data']


def save_verified(runner, function_id):
    body = {'function_signature': 'def f():', 'intent_summary': 'Return a positive result',
                'invariants': [{'name': 'positive', 'logic': 'Output is positive', 'criticality': 'HIGH', 'on_fail': 'error'}]}
    run(runner, 'contract', 'save', function_id, '--confidence', '90', input=json.dumps(body))
    run(runner, 'verify', 'run', function_id, input='{"input":{},"output":1}')


def snapshot():
    """Every table of the index. The scan's own bookkeeping (its request rows, its queue rows, its progress record)
    is left out: a failed scan is expected to leave a trace of having been asked for, never a trace in the index."""
    conn = get_connection()
    try:
        return {table: conn.execute(f'SELECT * FROM "{table}"' + (" WHERE key NOT LIKE 'scan:run:%'" if table == 'config' else '') + ' ORDER BY ALL').fetchall()
                for (table,) in conn.execute('SHOW TABLES').fetchall() if table not in ('staleness_queue', 'scan_requests')}
    finally:
        conn.close()


def assert_stale(runner, function_id, reason):
    data = run(runner, 'contract', 'get', function_id)
    assert data['status'] == 'STALE'
    assert not data['freshness']['current']
    assert not data['verification']['current']
    conn = get_connection()
    if reason != 'HASH_MISMATCH':  # a source edit has no separate stale reason
        assert conn.execute('SELECT value FROM config WHERE key=?', ['artifact:stale_reason:' + function_id]).fetchone() == (reason,)
    conn.close()
    return data


@pytest.mark.parametrize('change', ['import', 'callee', 'transitive', 'property', 'cycle'])
def test_context_invalidates_caller_evidence(project, change):
    Path('a.py').write_text('from b import helper\ndef f():\n    return helper()\n')
    Path('b.py').write_text('def helper():\n    return 1\n')
    Path('c.py').write_text('def helper():\n    return -1\n')
    if change == 'transitive':
        Path('b.py').write_text('from d import leaf\ndef helper():\n    return leaf()\n')
        Path('d.py').write_text('def leaf():\n    return 1\n')
    if change == 'property':
        Path('a.py').write_text('from b import Service\ndef f(s: Service):\n    return s.value\n')
        Path('b.py').write_text('class Service:\n    @property\n    def value(self):\n        return 1\n')
    if change == 'cycle':
        Path('b.py').write_text('from a import f\ndef helper():\n    return f()\n')
    f_id = fid("a.py", "f", "Service") if change == 'property' else fid("a.py", "f")
    run(project, 'scan', '.')
    save_verified(project, f_id)
    before = run(project, 'contract', 'get', f_id)
    run(project, 'scan', '.')
    unchanged = run(project, 'contract', 'get', f_id)
    assert unchanged['freshness'] == before['freshness']
    assert unchanged['verification']['current']
    reason = 'DEPENDENCY_CHANGED'
    if change == 'import':
        Path('a.py').write_text('from c import helper\ndef f():\n    return helper()\n')
        reason = 'IMPORTS_CHANGED'
        scan_path = 'a.py'
    elif change == 'transitive':
        Path('d.py').write_text('def leaf():\n    return -1\n')
        scan_path = 'd.py'
    elif change == 'property':
        Path('b.py').write_text('class Service:\n    @property\n    def value(self):\n        return -1\n')
        scan_path = 'b.py'
    else:
        Path('b.py').write_text('def helper():\n    return -1\n')
        scan_path = 'b.py'
    scan = run(project, 'scan', scan_path)
    assert scan['contracts_invalidated']['examples'][f_id] == reason
    after = assert_stale(project, f_id, reason)
    assert after['freshness']['current_source_hash'] == before['freshness']['current_source_hash']
    assert after['freshness']['current_context_hash'] != before['freshness']['current_context_hash']
    assert run(project, 'status')['verification_evidence']['stale'] == 1
    result = project.invoke(app, ["--json", 'verify', 'run', f_id], input='{"input":{},"output":1}')
    assert result.exit_code == 1
    assert not json.loads(result.output)['data']['current']
    save_verified(project, f_id)
    assert run(project, 'contract', 'get', f_id)['freshness']['current']


@pytest.mark.parametrize('stage', ['update', 'metadata', 'deletion', 'graph', 'embeddings', 'after_sync'])
def test_failed_scan_never_publishes_partial_writes(project, stage):
    import drspec.core.graph_sync as graph
    source = 'def helper():\n    return 1\ndef f():\n    return helper()\n'
    Path('a.py').write_text(source)
    run(project, 'scan', '.')
    save_verified(project, fid("a.py", "f"))
    before = snapshot()
    if stage == 'metadata':
        Path('a.py').write_text('\n\n' + source)
    elif stage == 'deletion':
        Path('a.py').write_text('def f():\n    return 2\n')
    elif stage != 'embeddings':
        Path('a.py').write_text(source.replace('return 1', 'return 2'))
    options = ['scan', '.'] + (['--full', '--embed'] if stage == 'embeddings' else [])
    if stage in ('update', 'metadata', 'deletion', 'graph'):
        name = {'update': '_sync_nodes', 'metadata': '_sync_nodes', 'deletion': '_sync_removed_nodes', 'graph': '_sync_edges'}[stage]
        operation = getattr(graph, name)
        def fail_after(*args, **kwargs):
            operation(*args, **kwargs)
            raise RuntimeError('injected failure after committed helper writes')
        failure = patch('drspec.core.graph_sync.' + name, side_effect=fail_after)
    elif stage == 'embeddings':
        failure = patch('drspec.core.graph_sync.embed_text', side_effect=[[0.1] * 384, RuntimeError('embedding failure')])
    elif stage == 'after_sync':
        import drspec.staleness_worker as scan_module
        real_sync = scan_module.sync_scan_results

        def fail_after_sync(*args, **kwargs):
            real_sync(*args, **kwargs)
            raise RuntimeError('failure after the whole sync wrote')
        failure = patch('drspec.staleness_worker.sync_scan_results', side_effect=fail_after_sync)
    with failure:
        result = project.invoke(app, options)
    assert result.exit_code == 1, result.output
    assert snapshot() == before
    with patch('drspec.core.graph_sync.embed_text', return_value=[0.1] * 384):
        run(project, *options)
    assert snapshot() != before
    if stage not in ('metadata', 'embeddings'):
        assert run(project, 'contract', 'get', fid("a.py", "f"))['status'] == 'STALE'


@pytest.mark.parametrize('extension,source,expected', [
    ('py', 'class Service:\n    def helper(self):\n        return 1\n    def execute(self):\n        return self.helper()\n    @property\n    def value(self):\n        return 1\n    def read(self):\n        return self.value\n', 'helper'),
    ('js', 'class Service { helper() { return 1; } execute() { return this.helper(); } }', 'helper'),
    ('cpp', 'class Service { public: int helper() { return 1; } int execute() { return helper(); } };', 'helper'),
])
def test_same_named_classes_are_scoped_to_file(project, extension, source, expected):
    for file in ('a', 'b'):
        Path(file + '.' + extension).write_text(source)
    run(project, 'scan', '.')
    conn = get_connection()
    for table in ('dependencies', 'code_references'):
        rows = conn.execute(f'SELECT caller_id, callee_id, confidence FROM {table}').fetchall()
        if table == 'dependencies':
            assert len(rows) == 2
        for caller, callee, confidence in rows:
            assert caller.split('::')[0] == callee.split('::')[0]
            assert confidence == 'EXTRACTED'
    conn.close()


@pytest.mark.parametrize('binding,annotation,callee_prefix', [
    ('from a import Service as Remote', 'Remote', 'a.py'),
    ('import b as remote', 'remote.Service', 'b.py'),
    ('from a import Service\nfrom b import Service', 'Service', None),
    ('', 'Service', None),
])
def test_typed_receivers_follow_import_binding(project, binding, annotation, callee_prefix):
    source = 'class Service:\n    def helper(self):\n        return 1\n    @property\n    def value(self):\n        return 1\n'
    Path('a.py').write_text(source)
    Path('b.py').write_text(source)
    Path('caller.py').write_text(binding + '\ndef f(s: ' + annotation + '):\n    return s.helper() + s.value\n')
    run(project, 'scan', '.')
    conn = get_connection()
    for table, name in [('dependencies', 'helper'), ('code_references', 'value')]:
        rows = conn.execute(f"SELECT callee_id FROM {table} WHERE caller_id=?", [fid("caller.py", "f", annotation)]).fetchall()
        expected = [(fid(callee_prefix, 'Service.' + name),)] if callee_prefix else []
        assert rows == expected
    conn.close()


@pytest.mark.parametrize('before,after', [
    ('', '@wrap'), ('@wrap', ''), ('@first\n@second', '@second\n@first'),
    ('@wrap(1)', '@wrap(2)'), ('@staticmethod', '@classmethod'), ('@property', ''),
])
def test_decorators_participate_in_source_and_review(project, before, after):
    def source(decorators):
        prefix = ''.join('    ' + line + '\n' for line in decorators.splitlines())
        return 'class Service:\n' + prefix + '    def f(self):\n        return 1\n'
    Path('a.py').write_text(source(before))
    run(project, 'scan', '.')
    save_verified(project, fid("a.py", "Service.f"))
    old = run(project, 'contract', 'get', fid("a.py", "Service.f"))
    Path('a.py').write_text(source(after))
    data = run(project, 'scan', '.')
    assert data['functions_changed'] == 1
    new = assert_stale(project, fid("a.py", "Service.f"), 'HASH_MISMATCH')
    assert old['freshness']['current_source_hash'] != new['freshness']['current_source_hash']
    assert run(project, 'scan', '.')['functions_changed'] == 0
    conn = get_connection()
    body, code_hash, start_line, signature = conn.execute("SELECT body, code_hash, start_line, signature FROM artifacts WHERE function_id=?", [fid("a.py", "Service.f")]).fetchone()
    conn.close()
    assert code_hash == compute_hash(body, 'python')
    assert body.startswith(after.splitlines()[0] if after else 'def f')
    assert start_line == 2
    assert signature == 'def f(self):'


def test_rescanning_an_unchanged_stale_context_keeps_the_stale_reason(project):
    Path('a.py').write_text('from b import helper\ndef f():\n    return helper()\n')
    Path('b.py').write_text('def helper():\n    return 1\n')
    run(project, 'scan', '.')
    save_verified(project, fid("a.py", "f"))
    Path('b.py').write_text('def helper():\n    return 2\n')
    run(project, 'scan', 'b.py')
    assert_stale(project, fid("a.py", "f"), 'DEPENDENCY_CHANGED')
    run(project, 'scan', '.')
    assert_stale(project, fid("a.py", "f"), 'DEPENDENCY_CHANGED')


def test_nested_classes_and_imported_nested_type_keep_qualified_identity(project):
    Path('a.py').write_text('class Outer:\n    class Service:\n        def helper(self):\n            return 1\n        def execute(self):\n            return self.helper()\nclass Other:\n    class Service:\n        def helper(self):\n            return 2\n        def execute(self):\n            return self.helper()\n')
    Path('b.py').write_text('from a import Outer\ndef f(s: Outer.Service):\n    return s.helper()\n')
    run(project, 'scan', '.')
    conn = get_connection()
    edges = conn.execute('SELECT caller_id, callee_id FROM dependencies ORDER BY 1').fetchall()
    conn.close()
    assert edges == [(fid("a.py", "Other.Service.execute"), fid("a.py", "Other.Service.helper")),
                     (fid("a.py", "Outer.Service.execute"), fid("a.py", "Outer.Service.helper")),
                     (fid("b.py", "f", "Outer.Service"), fid("a.py", "Outer.Service.helper"))]


def test_removing_last_property_read_removes_reference_context(project):
    Path('a.py').write_text('class Service:\n    @property\n    def value(self):\n        return 1\n    def f(self):\n        return self.value\n')
    run(project, 'scan', '.')
    Path('a.py').write_text('class Service:\n    @property\n    def value(self):\n        return 1\n    def f(self):\n        return 1\n')
    run(project, 'scan', '.')
    conn = get_connection()
    assert conn.execute('SELECT count(*) FROM code_references').fetchone() == (0,)
    conn.close()


def test_unknown_annotated_receiver_cannot_fall_back_to_shadowed_module(project):
    Path('a.py').write_text('def helper():\n    return 1\n')
    Path('b.py').write_text('import a as s\ndef f(s: Unknown):\n    return s.helper()\n')
    run(project, 'scan', '.')
    conn = get_connection()
    assert conn.execute('SELECT count(*) FROM dependencies').fetchone() == (0,)
    conn.close()


def test_python_verification_apis_reject_stale_context(project):
    from drspec.api import configure, run_verification
    from drspec.debugging.runtime import verify_contract_at_runtime

    Path('a.py').write_text('from b import helper\ndef f():\n    return helper()\n')
    Path('b.py').write_text('def helper():\n    return 1\n')
    run(project, 'scan', '.')
    save_verified(project, fid("a.py", "f"))
    Path('b.py').write_text('def helper():\n    return -1\n')
    run(project, 'scan', 'b.py')
    configure(str(Path('_drspec/contracts.db').resolve()))
    try:
        result = run_verification(fid("a.py", "f"), {}, 1)
        assert not result.passed
        assert 'Stale contract' in result.error
    finally:
        configure(None)
    conn = get_connection()
    try:
        result = verify_contract_at_runtime(conn, fid("a.py", "f"), {}, 1)
        assert not result.passed
        assert 'Stale contract' in result.error
    finally:
        conn.close()
