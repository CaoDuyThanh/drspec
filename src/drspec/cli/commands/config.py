"""Config command - view and change drspec settings."""

from __future__ import annotations

from pathlib import Path
from typing import Optional

import typer

from drspec.cli.output import ErrorCode, error_response, output, success_response
from drspec.core.settings import (
    SETTINGS,
    SettingError,
    get_setting,
    list_settings,
    reset_setting,
    set_setting,
)
from drspec.worker_ipc import WorkerIPCError, get_worker_connection

app = typer.Typer(
    name="config",
    help="View and change drspec settings (background worker and embedding behavior)",
    no_args_is_help=True,
)


def setting_record(key: str, value: object) -> dict:
    setting = SETTINGS[key]
    return {
        "key": key,
        "value": value,
        "default": setting.default,
        "is_default": value == setting.default,
        "description": setting.description,
    }


def _open(ctx: typer.Context):
    """The worker connection, or `(None, exit_code_response)` on failure."""
    cli_ctx = ctx.obj or {}
    json_output = cli_ctx.get("json_output", False)
    db_path = cli_ctx.get("db_path") or (Path.cwd() / "_drspec" / "contracts.db")

    if not db_path.exists():
        output(
            error_response(ErrorCode.DB_NOT_INITIALIZED, "DrSpec not initialized. Run 'drspec init' first.", {"db_path": str(db_path)}),
            json_output=json_output,
        )
        raise typer.Exit(1)
    try:
        return get_worker_connection(db_path), json_output
    except WorkerIPCError as e:
        output(
            error_response(ErrorCode.DATABASE_BUSY, f"Could not reach the drspec worker: {e}", {"db_path": str(db_path)}),
            json_output=json_output,
        )
        raise typer.Exit(1)


def _fail_setting(error: SettingError, json_output: bool) -> None:
    output(error_response(ErrorCode.INVALID_INPUT, str(error)), json_output=json_output)
    raise typer.Exit(1)


@app.command("get")
def get_command(
    ctx: typer.Context,
    key: Optional[str] = typer.Argument(None, help="Setting key. Omit to list every setting."),
) -> None:
    """Show one setting, or all of them.

    Examples:
        drspec config get
        drspec config get worker.poll_interval
    """
    conn, json_output = _open(ctx)
    try:
        if key is None:
            output(success_response({"settings": list_settings(conn)}), json_output=json_output)
            return
        try:
            value = get_setting(conn, key)
        except SettingError as e:
            _fail_setting(e, json_output)
        output(success_response(setting_record(key, value)), json_output=json_output)
    finally:
        conn.close()


@app.command("set")
def set_command(
    ctx: typer.Context,
    key: str = typer.Argument(..., help="Setting key."),
    value: str = typer.Argument(..., help="New value."),
) -> None:
    """Change a setting. The background worker picks it up on its next pass.

    Examples:
        drspec config set worker.poll_interval 30
        drspec config set embedding.enabled false
    """
    conn, json_output = _open(ctx)
    try:
        try:
            stored = set_setting(conn, key, value)
        except SettingError as e:
            _fail_setting(e, json_output)
        output(success_response(setting_record(key, stored)), json_output=json_output)
    finally:
        conn.close()


@app.command("reset")
def reset_command(
    ctx: typer.Context,
    key: str = typer.Argument(..., help="Setting key."),
) -> None:
    """Restore a setting's default.

    Examples:
        drspec config reset worker.poll_interval
    """
    conn, json_output = _open(ctx)
    try:
        try:
            default = reset_setting(conn, key)
        except SettingError as e:
            _fail_setting(e, json_output)
        output(success_response(setting_record(key, default)), json_output=json_output)
    finally:
        conn.close()
