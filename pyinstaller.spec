# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller spec file for DrSpec.

Builds a single-file executable that bundles Python runtime and all dependencies.

Usage:
    pyinstaller pyinstaller.spec

Output:
    dist/drspec (Linux/macOS) or dist/drspec.exe (Windows)
"""

import sys
from pathlib import Path

# Get the project root
project_root = Path(SPECPATH)

a = Analysis(
    [str(project_root / 'src' / 'drspec' / '__main__.py')],
    pathex=[str(project_root / 'src')],
    binaries=[],
    datas=[
        # Bundle agent templates (from src/drspec/agents/)
        (str(project_root / 'src' / 'drspec' / 'agents'), 'agents'),
        # Bundle database schema
        (str(project_root / 'src' / 'drspec' / 'db' / 'schema.sql'), 'drspec/db'),
    ],
    hiddenimports=[
        # Typer and Click dependencies
        'typer',
        'typer.core',
        'typer.main',
        'click',
        'click.core',
        'click.decorators',
        'rich',
        'rich.console',
        'rich.markup',
        'rich.text',
        'shellingham',
        # Pydantic dependencies
        'pydantic',
        'pydantic.fields',
        'pydantic_core',
        'annotated_types',
        'tree_sitter_python',
        'tree_sitter_javascript',
        'tree_sitter_typescript',
        'tree_sitter_cpp',
        'tree_sitter_rust',
        # DuckDB
        'duckdb',
        # Standard library modules that may be missed
        'typing_extensions',
    ],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=['tkinter', 'pandas'],
    noarchive=False,
)

pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name='drspec',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=True,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)
