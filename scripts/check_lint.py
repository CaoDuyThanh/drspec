"""Enforce no new Ruff findings while retaining the explicit legacy baseline.

Run with the pinned Ruff in CI: python scripts/check_lint.py.
The baseline is reviewed data; this script never expands it automatically.
"""

from __future__ import annotations

import json
import subprocess
import sys
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
BASELINE = ROOT / 'scripts' / 'lint-baseline.json'


def findings():
    result = subprocess.run(
        [sys.executable, '-m', 'ruff', 'check', 'src/', 'tests/', '--isolated',
         '--target-version', 'py38', '--output-format', 'json'],
        cwd=ROOT, capture_output=True, text=True, check=False,
    )
    if result.returncode not in (0, 1):
        raise RuntimeError(result.stderr or result.stdout)
    return json.loads(result.stdout)


def signature(issue):
    path = Path(issue['filename'])
    line = path.read_text(encoding='utf-8').splitlines()[issue['location']['row'] - 1].strip()
    return json.dumps([str(path.relative_to(ROOT)), issue['code'], issue['message'], line])


def main():
    baseline = Counter(json.loads(BASELINE.read_text(encoding='utf-8')))
    issues = findings()
    seen = Counter()
    new = []
    for issue in issues:
        key = signature(issue)
        seen[key] += 1
        if seen[key] > baseline[key]:
            new.append(issue)
    for issue in new:
        print(f"{Path(issue['filename']).relative_to(ROOT)}:{issue['location']['row']}: {issue['code']} {issue['message']}")
    if new:
        print(f'{len(new)} new Ruff finding(s); legacy findings must not be used to hide new issues.')
        return 1
    print(f'Ruff baseline check passed: {len(issues)} documented legacy findings, no new findings.')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
