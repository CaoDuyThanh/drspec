"""The pattern register (tests/README.md, `tests/tools/pattern_audit.py`): a cleaned pattern must not come back, and the detector must find
what it says it finds."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from tests.tools import pattern_audit

README = Path(__file__).resolve().parents[1] / "README.md"

SAMPLES = {
    "tempfile": "import tempfile\nx = tempfile.mkdtemp()\n",
    "init-for-setup": 'runner.invoke(app, ["init", "--no-ide"])\n',
    "outside-path": 'p = "/nonexistent/x.db"\n',
    "skip-on-machine": "@pytest.mark.skipif(os.geteuid() == 0, reason='root')\n",
    "os-chdir": "os.chdir(folder)\n",
    "real-sleep": "time.sleep(1)\n",
    "real-subprocess": "subprocess.run(['x'])\n",
    "slow-socket-model-marker": "@pytest.mark.slow\n",
    "environ-assigned": "os.environ['A'] = '1'\n",
    "duckdb-file": "conn = duckdb.connect(str(path))\n",
    "git": "subprocess_args = ['git', 'status']\n",
    "test-module-import-in-test": "def test_x():\n    from tests.unit.test_other import helper\n",
    "real-time-assert": "assert time.monotonic() - began < 2\n",
    "no-assert": "def test_x():\n    x = 1\n",
    "local-copy-of-product-code": "def get_dependency_info(conn, function_id, depth=1):\n    a = 1\n    b = 2\n    c = 3\n    d = 4\n    e = 5\n    f = 6\n    g = 7\n    h = 8\n    return a\n",
}


@pytest.mark.parametrize("pattern", sorted(SAMPLES))
def test_the_detector_finds_each_pattern_and_a_justification_silences_it(tmp_path, pattern):
    path = tmp_path / "test_sample.py"
    path.write_text(SAMPLES[pattern])
    assert pattern in pattern_audit.detect(path)
    if pattern != "no-assert":
        silenced = tmp_path / "test_silenced.py"
        silenced.write_text(SAMPLES[pattern].rstrip("\n") + f"  # pattern-ok: {pattern} on purpose\n")
        assert pattern not in pattern_audit.detect(silenced)


def test_every_pattern_has_a_sample_and_a_row_in_the_readme():
    readme = README.read_text()
    assert set(pattern_audit.PATTERNS) == set(SAMPLES)
    for pattern in pattern_audit.PATTERNS:
        assert f"| {pattern} |" in readme, pattern


def test_no_file_has_more_hits_of_a_pattern_than_the_baseline_allows():
    baseline = json.loads(pattern_audit.BASELINE.read_text())
    grown = []
    for file, patterns in pattern_audit.counts(pattern_audit.audit()).items():
        for pattern, count in patterns.items():
            allowed = baseline.get(file, {}).get(pattern, 0)
            if count > allowed:
                grown.append(f"{file}: {pattern} {count} (allowed {allowed})")
    assert not grown, (
        "a pattern of the register came back (tests/README.md, 'Pattern register'): fix it, or justify the line with "
        "`# pattern-ok: <pattern> <why>`:\n" + "\n".join(grown)
    )
