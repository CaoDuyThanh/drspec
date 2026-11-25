"""Regression/acceptance tests, run offline with explicit project roots."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
from unittest.mock import patch

import pytest
from typer.testing import CliRunner

from drspec.cli.app import app
from drspec.contracts import (
    Contract,
    execute_verification,
    generate_verification_script,
)
from drspec.core.hasher import compute_hash
from drspec.core.scanner import Scanner
from drspec.db import get_connection
from tests.helpers import fid

pytestmark = pytest.mark.usefixtures("scripts_in_process_unless_integration")  # no test here starts a Python process, unless it is marked integration


@pytest.fixture
def project(kept_project):
    """A CLI runner in a project folder with an empty in-memory project database (`kept_project`): these tests are about scan, contracts and
    verification evidence, not about `init`."""
    return CliRunner()


def invoke(runner, *args, **kwargs):
    result = runner.invoke(app, ['--json', *args], **kwargs)
    assert result.exit_code == 0, result.output
    return json.loads(result.output)['data']


def contract(*rules):
    return Contract.from_dict(dict(function_signature='def f(x):', intent_summary='Return a computed result',
        invariants=[dict(name=f'rule{i}', logic=rule, criticality='HIGH', on_fail='error') for i, rule in enumerate(rules)]))


@pytest.mark.parametrize('rules,output,status,counts', [
    (['Match invoices to approved purchase orders'], 3, 'unverified', (0, 0, 1, 0)),
    (['Output is positive', 'Match invoices to approved purchase orders'], 3, 'partial', (1, 0, 1, 0)),
    (['Output is positive', 'Match invoices to approved purchase orders'], -1, 'failed', (0, 1, 1, 0)),
    (['Output is positive', 'Output is a string'], 'abc', 'error', (1, 0, 0, 1)),
    (['Output is positive', 'Output is an integer'], 3, 'passed', (2, 0, 0, 0)),
    (['Output is positive and matches the approved invoice'], 3, 'unverified', (0, 0, 1, 0)),
])
def test_rule_evidence(rules, output, status, counts):
    result = execute_verification(generate_verification_script(contract(*rules), fid("f.py", "f")), {}, output)
    assert result.status == status
    assert result.passed == (status == 'passed')
    assert (result.invariants_passed, result.invariants_failed, result.invariants_skipped, result.invariants_error) == counts
    assert len(result.results) == len(rules)


@pytest.mark.parametrize('before,after', [
    ('def f(x):\n    if x:\n        x += 1\n    return x', 'def f(x):\n    if x:\n        x += 1\n        return x'),
    ('def f():\n    return "a  b"', 'def f():\n    return "a b"'),
    ('def f():\n    return """first"""', 'def f():\n    return """second"""'),
])
def test_exact_hash_marks_the_contract_stale(project, before, after):
    path = Path('f.py')
    path.write_text(before)
    invoke(project, 'scan', 'f.py')
    invoke(project, 'contract', 'save', 'f.py::f', '--confidence', '99', input=contract('Output is not None').to_json())
    path.write_text(after)
    data = invoke(project, 'scan', 'f.py')
    assert data['functions_changed'] == 1
    data = invoke(project, 'contract', 'get', 'f.py::f')
    assert not data['freshness']['current']
    assert data['status'] == 'STALE'
    assert invoke(project, 'scan', '--full', 'f.py')['functions_unchanged'] == 1


@pytest.mark.parametrize('extension,source,name,signature,body,call', [
    ('py', '# tiếng Việt\ndef chào():\n    return helper("đúng")\n', 'chào', 'def chào():', 'def chào():\n    return helper("đúng")', 'helper("đúng")'),
    ('js', '// tiếng Việt\nfunction chào() { return helper("đúng"); }\n', 'chào', 'function chào() { return helper("đúng"); }', 'function chào() { return helper("đúng"); }', 'helper("đúng")'),
    ('cpp', '// tiếng Việt\nint chào() { return helper("đúng"); }\n', 'chào', 'int chào() {', 'int chào() { return helper("đúng"); }', 'helper("đúng")'),
])
def test_unicode_extraction(tmp_path, extension, source, name, signature, body, call):
    path = tmp_path / ('f.' + extension)
    path.write_text(source, encoding='utf8')
    result = Scanner().scan_file(path, relative_to=tmp_path)
    assert not result.errors
    function = result.functions[0]
    assert function.name == name
    assert function.signature.startswith(signature)
    assert function.body == body
    assert function.calls[0].text == call
    assert function.start_line == 2


def test_malformed_scan_preserves_records_and_recovers(project):
    Path('f.py').write_text('def helper():\n    return 1\ndef f():\n    return helper()\n')
    invoke(project, 'scan', '.')
    invoke(project, 'contract', 'save', fid("f.py", "f"), '--confidence', '99', input=contract('Output is positive').to_json())
    conn = get_connection()
    before = [conn.execute(f'SELECT * FROM {table} ORDER BY 1').fetchall() for table in ('artifacts', 'contracts', 'dependencies')]
    conn.close()
    Path('f.py').write_text('def helper(:\n    return 2\n')
    for scope in ('f.py', '.'):
        data = invoke(project, 'scan', '--full', scope)  # --full: the file is read again although nothing changed since the last look
        assert data['error_count'] > 0
        assert data['errors'][0]['line'] is not None
        conn = get_connection()
        after = [conn.execute(f'SELECT * FROM {table} ORDER BY 1').fetchall() for table in ('artifacts', 'contracts', 'dependencies')]
        conn.close()
        assert after == before
    Path('f.py').write_text('def f():\n    return 2\n')
    assert invoke(project, 'scan', '.')['functions_removed'] == 1


def test_deleted_file_scope_exclusions_and_failed_enumeration(project):
    for folder in ('src', 'other', 'src/nested'):
        Path(folder).mkdir(exist_ok=True)
        Path(folder, 'f.py').write_text('def f():\n    return 1\n')
    invoke(project, 'scan', '.')
    Path('src/f.py').unlink()
    Path('other/f.py').unlink()
    Path('src/nested/f.py').unlink()
    with patch('drspec.core.project_files.os.walk', side_effect=PermissionError('cannot enumerate')):
        result = project.invoke(app, ['scan', 'src'])
        assert result.exit_code == 1
    assert invoke(project, 'scan', 'src', '--no-recursive')['functions_removed'] == 1
    conn = get_connection()
    assert conn.execute('SELECT file_path FROM artifacts ORDER BY 1').fetchall() == [('other/f.py',), ('src/nested/f.py',)]
    conn.close()
    assert invoke(project, 'scan', 'src')['functions_removed'] == 1


def test_metadata_move_keeps_contract_and_hash(project):
    source = 'def f():\n    return 1\n'
    Path('f.py').write_text(source)
    invoke(project, 'scan', 'f.py')
    invoke(project, 'contract', 'save', fid("f.py", "f"), '--confidence', '99', input=contract('Output is positive').to_json())
    before = invoke(project, 'contract', 'get', fid("f.py", "f"))
    Path('f.py').write_text('\n\n' + source)
    assert invoke(project, 'scan', 'f.py')['functions_unchanged'] == 1
    conn = get_connection()
    assert conn.execute("SELECT start_line FROM artifacts WHERE function_id=?", [fid("f.py", "f")]).fetchone() == (3,)
    conn.close()
    after = invoke(project, 'contract', 'get', fid("f.py", "f"))
    assert before['contract'] == after['contract']
    assert after['freshness']['current']


def test_import_and_new_dependency_trigger_resolution(project):
    Path('a.py').write_text('from b import helper\ndef f():\n    return helper()\n')
    invoke(project, 'scan', 'a.py')
    Path('b.py').write_text('def helper():\n    return 1\n')
    data = invoke(project, 'scan', 'b.py')
    assert data['resolution_reasons']['examples'][fid("a.py", "f")] == 'symbols_changed'
    conn = get_connection()
    assert conn.execute('SELECT caller_id, callee_id FROM dependencies').fetchall() == [(fid("a.py", "f"), fid("b.py", "helper"))]
    conn.close()
    Path('c.py').write_text('def helper():\n    return 2\n')
    invoke(project, 'scan', 'c.py')
    Path('a.py').write_text('from c import helper\ndef f():\n    return helper()\n')
    data = invoke(project, 'scan', 'a.py')
    assert data['functions_unchanged'] == 1
    assert data['resolution_reasons']['examples'][fid("a.py", "f")] == 'imports_changed'
    conn = get_connection()
    assert conn.execute('SELECT caller_id, callee_id FROM dependencies').fetchall() == [(fid("a.py", "f"), fid("c.py", "helper"))]
    conn.close()


def test_embedding_backfill_reuse_and_model_upgrade(project):
    Path('f.py').write_text('class C:\n    @property\n    def value(self):\n        return 1\n    def f(self):\n        return self.value\ndef g():\n    return h()\ndef h():\n    return 1\n')
    invoke(project, 'scan', '.')
    with patch('drspec.core.graph_sync.embed_text', return_value=[0.1] * 384) as embed:
        data = invoke(project, 'scan', '--full', '--embed', '.')
        assert embed.call_count == 6  # four functions, one call, one property read
        assert data['embeddings_updated'] == 6
        embed.reset_mock()
        invoke(project, 'scan', '--full', '--embed', '.')
        embed.assert_not_called()
        with patch('drspec.core.graph_sync.MODEL_VERSION', 'new-model-version'):
            invoke(project, 'scan', '--full', '--embed', '.')
            assert embed.call_count == 6
    conn = get_connection()
    assert conn.execute('SELECT count(*) FROM artifacts a WHERE NOT EXISTS (SELECT 1 FROM artifact_embeddings e WHERE e.function_id = a.function_id)').fetchone() == (0,)
    for table in ('dependencies', 'code_references'):
        assert conn.execute(f'SELECT count(*) FROM {table} WHERE embedding IS NULL').fetchone() == (0,)
    conn.close()


@pytest.mark.parametrize('extension,source', [
    ('ts', 'import { helper } from "./b";\ninterface Item { value: number; }\nexport function f<T extends Item>(x: T): number { return helper(x.value); }'),
    ('tsx', 'import { helper } from "./b";\ninterface Props { title: string; }\nexport const View = ({title}: Props) => <div>{helper(title)}</div>;'),
])
def test_typescript_grammars(tmp_path, extension, source):
    path = tmp_path / ('f.' + extension)
    path.write_text(source)
    result = Scanner().scan_file(path, relative_to=tmp_path)
    assert result.parse_status == 'successful', result.errors
    assert result.functions
    assert result.functions[0].calls[0].name == 'helper'
    assert result.file_imports[path.name].name_imports[0].name == 'helper'


def test_confidence_freshness_and_verification_are_separate(project):
    Path('f.py').write_text('def f(x):\n    return x\n')
    invoke(project, 'scan', 'f.py')
    invoke(project, 'contract', 'save', fid("f.py", "f", "x"), '--confidence', '100', input=contract('Output is positive').to_json())
    data = invoke(project, 'contract', 'get', fid("f.py", "f", "x"))
    assert data['status'] == 'NEEDS_REVIEW'
    assert data['verification'] is None
    assert data['check_coverage'] == {'supported': 1, 'total': 1, 'unsupported': 0}
    invoke(project, 'verify', 'run', fid("f.py", "f", "x"), input='{"input":{"x":1},"output":1}')
    assert invoke(project, 'contract', 'get', fid("f.py", "f", "x"))['verification']['current']
    Path('f.py').write_text('def f(x):\n    return -x\n')
    invoke(project, 'scan', 'f.py')
    data = invoke(project, 'contract', 'get', fid("f.py", "f", "x"))
    assert not data['freshness']['current']
    assert not data['verification']['current']
    assert invoke(project, 'status')['verification_evidence']['stale'] == 1


def test_legacy_hash_is_invalidated_once(project):
    from drspec.core.hasher import normalize_code
    from drspec.db.queries import insert_artifact, insert_contract
    source = 'def f():\n    return 1'
    Path('f.py').write_text(source)
    old_hash = hashlib.sha256(normalize_code(source, 'python').encode()).hexdigest()
    assert old_hash != compute_hash(source, 'python')
    conn = get_connection()
    insert_artifact(conn, fid("f.py", "f"), 'f.py', 'f', 'def f():', source, old_hash, 'python', 1, 2, status='VERIFIED')
    insert_contract(conn, fid("f.py", "f"), contract('Output is positive').to_json(), 0.99)
    conn.close()
    assert invoke(project, 'scan', 'f.py')['functions_changed'] == 1
    assert invoke(project, 'contract', 'get', fid("f.py", "f"))['status'] == 'STALE'
    assert invoke(project, 'scan', 'f.py')['functions_changed'] == 0


def test_cli_partial_coverage_cannot_verify_high_confidence_contract(project):
    Path('f.py').write_text('def f():\n    return 1\n')
    invoke(project, 'scan', 'f.py')
    invoke(project, 'contract', 'save', fid("f.py", "f"), '--confidence', '100',
           input=contract('Output is positive', 'Match invoices to approved purchase orders').to_json())
    result = project.invoke(app, ["--json", 'verify', 'run', fid("f.py", "f")], input='{"input":{},"output":1}')
    assert result.exit_code == 1
    data = json.loads(result.output)['data']
    assert data['status'] == 'partial'
    assert not data['passed']
    assert data['invariants'] == {'checked': 1, 'passed': 1, 'failed': 0, 'skipped': 1, 'error': 0}
    assert invoke(project, 'contract', 'get', fid("f.py", "f"))['status'] == 'NEEDS_REVIEW'


def test_failed_caller_reconciles_on_later_success(project):
    Path('a.py').write_text('from b import helper\ndef f():\n    return helper()\n')
    invoke(project, 'scan', 'a.py')
    Path('a.py').write_text('def f(:\n')
    Path('b.py').write_text('def helper():\n    return 1\n')
    invoke(project, 'scan', '.')
    conn = get_connection()
    assert conn.execute('SELECT count(*) FROM dependencies').fetchone() == (0,)
    conn.close()
    Path('a.py').write_text('from b import helper\ndef f():\n    return helper()\n')
    data = invoke(project, 'scan', 'a.py')
    assert data['functions_unchanged'] == 1
    conn = get_connection()
    assert conn.execute('SELECT caller_id, callee_id FROM dependencies').fetchall() == [(fid("a.py", "f"), fid("b.py", "helper"))]
    conn.close()
