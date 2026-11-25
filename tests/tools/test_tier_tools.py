"""The test-tier recorder (`tier_report.py`): its patches are put in once and taken out again."""

from __future__ import annotations

import subprocess

import pytest

from tests.tools import tier_report

pytestmark = pytest.mark.component


def test_installing_twice_patches_once_and_removing_the_patches_restores_the_originals():
    if tier_report._originals:
        pytest.skip("the recorder is already loaded in this session; its patches are not ours to remove")
    original = subprocess.Popen
    tier_report._install()
    tier_report._install()
    assert subprocess.Popen is not original
    tier_report._unpatch_all()
    assert subprocess.Popen is original
    assert not tier_report._originals
