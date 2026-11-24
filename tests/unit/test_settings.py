"""Settings stored in the `config` table, and the `config` command / MCP tools."""

from __future__ import annotations

import json

import duckdb
import pytest
from typer.testing import CliRunner

from drspec.cli.app import app
from drspec.core.settings import (
    SETTINGS,
    SettingError,
    get_setting,
    list_settings,
    parse_value,
    read_all_settings,
    reset_setting,
    set_setting,
)
from drspec.db import init_schema
from drspec.db.queries import set_config

runner = CliRunner()

POLL_BLOCK = "  Key: worker.poll_interval\n    Value: 60.0\n    Default: 60.0\n    Is Default: Yes\n    Description: Seconds between background passes.\n"


@pytest.fixture
def conn():
    connection = duckdb.connect(":memory:")  # only the `config` table is read and written: no file
    init_schema(connection)
    yield connection
    connection.close()


@pytest.mark.unit
class TestParsing:
    @pytest.mark.parametrize("raw,expected", [("true", True), ("ON", True), ("1", True), ("false", False), ("no", False), (True, True)])
    def test_booleans(self, raw, expected):
        assert parse_value(SETTINGS["embedding.enabled"], raw) is expected

    def test_an_unparseable_boolean_is_rejected(self):
        with pytest.raises(SettingError) as raised:
            parse_value(SETTINGS["embedding.enabled"], "maybe")
        assert str(raised.value) == "embedding.enabled expects bool, got 'maybe'."

    def test_integers_reject_fractions_and_bools(self):
        setting = SETTINGS["worker.rescan_budget"]
        assert parse_value(setting, "12") == 12
        assert parse_value(setting, 12.0) == 12
        for bad in ("1.5", 1.5, True, "abc"):
            with pytest.raises(SettingError) as raised:
                parse_value(setting, bad)
            assert str(raised.value) == f"worker.rescan_budget expects int, got {bad!r}."

    def test_bounds_are_enforced(self):
        with pytest.raises(SettingError) as low:
            parse_value(SETTINGS["worker.poll_interval"], "0")
        with pytest.raises(SettingError) as high:
            parse_value(SETTINGS["embedding.cpu_percent"], "101")
        assert str(low.value) == "worker.poll_interval must be at least 1, got 0."
        assert str(high.value) == "embedding.cpu_percent must be at most 100, got 101."
        assert parse_value(SETTINGS["worker.poll_interval"], "1") == 1.0 and parse_value(SETTINGS["embedding.cpu_percent"], "100") == 100  # the bounds themselves are allowed
        with pytest.raises(SettingError) as bad_choice:  # a setting with a list of choices takes only those
            parse_value(SETTINGS["trace.level"], "bogus")
        assert str(bad_choice.value) == "trace.level must be one of off, slow, all, got 'bogus'."


@pytest.mark.unit
class TestStorage:
    def test_defaults_until_set(self, conn):
        assert get_setting(conn, "worker.poll_interval") == 60.0
        assert get_setting(conn, "worker.rescan_budget") == 200
        assert get_setting(conn, "embedding.enabled") is True

    def test_set_get_reset_round_trip(self, conn):
        assert set_setting(conn, "worker.rescan_budget", "25") == 25
        assert get_setting(conn, "worker.rescan_budget") == 25
        assert conn.execute("SELECT key, value FROM config").fetchall() == [("setting:worker.rescan_budget", "25")]  # stored under the setting prefix
        assert reset_setting(conn, "worker.rescan_budget") == 200
        assert get_setting(conn, "worker.rescan_budget") == 200

    def test_an_unknown_key_is_an_error_naming_the_known_ones(self, conn):
        with pytest.raises(SettingError) as read:
            get_setting(conn, "nope")
        with pytest.raises(SettingError) as write:
            set_setting(conn, "nope", 1)
        assert str(read.value) == f"Unknown setting: 'nope'. Known: {', '.join(sorted(SETTINGS))}."  # every known key, sorted
        assert str(write.value) == str(read.value)

    def test_a_stored_value_that_became_invalid_falls_back_to_the_default(self, conn):
        set_config(conn, "setting:worker.rescan_budget", json.dumps(-5))
        assert get_setting(conn, "worker.rescan_budget") == 200
        set_config(conn, "setting:worker.rescan_budget", "not json")
        assert get_setting(conn, "worker.rescan_budget") == 200

    def test_read_all_matches_the_individual_reads(self, conn):
        set_setting(conn, "embedding.top_n", 500)
        assert read_all_settings(conn)["embedding.top_n"] == 500
        assert read_all_settings(conn) == {key: get_setting(conn, key) for key in SETTINGS}

    def test_settings_do_not_collide_with_internal_config_keys(self, conn):
        set_config(conn, "embedding:[\"artifacts\", \"a.py::f\"]", "internal")
        set_setting(conn, "embedding.enabled", False)
        assert get_setting(conn, "embedding.enabled") is False
        assert conn.execute("SELECT value FROM config WHERE key LIKE 'embedding:%'").fetchone()[0] == "internal"

    def test_list_marks_non_defaults(self, conn):
        set_setting(conn, "worker.poll_interval", 30)
        rows = {r["key"]: r for r in list_settings(conn)}
        assert rows["worker.poll_interval"] == {
            "key": "worker.poll_interval", "value": 30.0, "default": 60.0, "is_default": False, "description": "Seconds between background passes.",
        }
        assert rows["worker.rescan_budget"]["is_default"] is True
        assert set(rows) == set(SETTINGS)
        assert [key for key, row in rows.items() if not row["is_default"]] == ["worker.poll_interval"]  # only the one that was set


@pytest.mark.component  # the command runner; the project database is in memory (`kept_project`), no `init`
class TestConfigCommand:
    def test_get_set_reset(self, kept_project):
        before = json.loads(runner.invoke(app, ["--json", "config", "get", "worker.poll_interval"]).output)["data"]
        after = json.loads(runner.invoke(app, ["--json", "config", "set", "worker.poll_interval", "15"]).output)["data"]
        reset = json.loads(runner.invoke(app, ["--json", "config", "reset", "worker.poll_interval"]).output)["data"]

        assert (before["value"], before["is_default"]) == (60.0, True)
        assert (after["value"], after["is_default"]) == (15.0, False)
        assert (reset["value"], reset["is_default"]) == (60.0, True)
        runner.invoke(app, ["--json", "config", "set", "worker.rescan_budget", "25"])  # and a value that was set is there for the next command
        stays = json.loads(runner.invoke(app, ["--json", "config", "get", "worker.rescan_budget"]).output)["data"]
        assert (stays["value"], stays["default"], stays["is_default"]) == (25, 200, False)

    def test_listing_everything(self, kept_project):
        data = json.loads(runner.invoke(app, ["--json", "config", "get"]).output)["data"]

        assert [s["key"] for s in data["settings"]] == list(SETTINGS)  # all of them, in the order they are declared
        assert {"key": "worker.poll_interval", "value": 60.0, "default": 60.0, "is_default": True, "description": "Seconds between background passes."} in data["settings"]

    def test_text_output_leads_each_setting_with_its_key(self, kept_project):
        result = runner.invoke(app, ["config", "get"])

        assert result.output.startswith("Settings:\n")
        assert POLL_BLOCK in result.output
        assert result.output.count("  Key: ") == len(SETTINGS)  # one block per setting, each led by its key

    def test_bad_input_is_a_clean_error(self, kept_project):
        for args, message in (
            (["set", "worker.poll_interval", "0"], "worker.poll_interval must be at least 1, got 0."),
            (["set", "nope", "1"], f"Unknown setting: 'nope'. Known: {', '.join(sorted(SETTINGS))}."),
            (["get", "nope"], f"Unknown setting: 'nope'. Known: {', '.join(sorted(SETTINGS))}."),
            (["reset", "nope"], f"Unknown setting: 'nope'. Known: {', '.join(sorted(SETTINGS))}."),
        ):
            result = runner.invoke(app, ["--json", "config", *args])
            assert result.exit_code == 1, args
            error = json.loads(result.output)["error"]
            assert (error["code"], error["message"]) == ("INVALID_INPUT", message), args
        data = json.loads(runner.invoke(app, ["--json", "config", "get", "worker.poll_interval"]).output)["data"]
        assert (data["value"], data["is_default"]) == (60.0, True)  # the bad value was not stored

    def test_requires_init(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)  # a folder with no `_drspec`

        result = runner.invoke(app, ["--json", "config", "get"])

        assert result.exit_code == 1
        error = json.loads(result.output)["error"]
        assert (error["code"], error["message"]) == ("DB_NOT_INITIALIZED", "DrSpec not initialized. Run 'drspec init' first.")
