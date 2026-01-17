"""Tests for CLI command structure and subcommands."""

import json

from typer.testing import CliRunner

from drspec.cli.app import app


runner = CliRunner()


class TestGlobalFlags:
    """Tests for global CLI flags."""

    def test_json_flag_default_enabled(self):
        """Test that --json is enabled by default."""
        result = runner.invoke(app, ["init"])
        assert result.exit_code == 0
        # Should output JSON by default
        assert "{" in result.stdout

    def test_pretty_flag(self):
        """Test --pretty flag is accepted."""
        result = runner.invoke(app, ["--pretty", "init"])
        assert result.exit_code == 0

    def test_no_json_flag(self):
        """Test --no-json flag is accepted."""
        result = runner.invoke(app, ["--no-json", "init"])
        assert result.exit_code == 0

    def test_db_path_flag(self):
        """Test --db flag is accepted."""
        result = runner.invoke(app, ["--db", "/tmp/test.db", "init"])
        assert result.exit_code == 0


class TestCommandGroups:
    """Tests for command group registration."""

    def test_init_command_exists(self):
        """Test init command is registered."""
        result = runner.invoke(app, ["init", "--help"])
        assert result.exit_code == 0
        assert "Initialize" in result.stdout

    def test_scan_command_exists(self):
        """Test scan command is registered."""
        result = runner.invoke(app, ["scan", "--help"])
        assert result.exit_code == 0
        assert "scan" in result.stdout.lower()

    def test_status_command_exists(self):
        """Test status command is registered."""
        result = runner.invoke(app, ["status", "--help"])
        assert result.exit_code == 0
        assert "status" in result.stdout.lower()

    def test_queue_command_exists(self):
        """Test queue command is registered."""
        result = runner.invoke(app, ["queue", "--help"])
        assert result.exit_code == 0
        assert "queue" in result.stdout.lower()

    def test_contract_command_exists(self):
        """Test contract command is registered."""
        result = runner.invoke(app, ["contract", "--help"])
        assert result.exit_code == 0
        assert "contract" in result.stdout.lower()

    def test_source_command_exists(self):
        """Test source command is registered."""
        result = runner.invoke(app, ["source", "--help"])
        assert result.exit_code == 0
        assert "source" in result.stdout.lower()

    def test_verify_command_exists(self):
        """Test verify command is registered."""
        result = runner.invoke(app, ["verify", "--help"])
        assert result.exit_code == 0
        assert "verify" in result.stdout.lower()

    def test_deps_command_exists(self):
        """Test deps command is registered."""
        result = runner.invoke(app, ["deps", "--help"])
        assert result.exit_code == 0
        assert "deps" in result.stdout.lower()


class TestQueueSubcommands:
    """Tests for queue subcommands."""

    def test_queue_next(self):
        """Test queue next subcommand (requires init)."""
        import tempfile
        with tempfile.TemporaryDirectory() as tmpdir:
            with runner.isolated_filesystem(temp_dir=tmpdir):
                result = runner.invoke(app, ["queue", "next"])
                assert result.exit_code == 1
                response = json.loads(result.output)
                assert response["error"]["code"] == "DB_NOT_INITIALIZED"

    def test_queue_peek(self):
        """Test queue peek subcommand (requires init)."""
        import tempfile
        with tempfile.TemporaryDirectory() as tmpdir:
            with runner.isolated_filesystem(temp_dir=tmpdir):
                result = runner.invoke(app, ["queue", "peek"])
                assert result.exit_code == 1
                response = json.loads(result.output)
                assert response["error"]["code"] == "DB_NOT_INITIALIZED"

    def test_queue_get(self):
        """Test queue get subcommand (requires init)."""
        import tempfile
        with tempfile.TemporaryDirectory() as tmpdir:
            with runner.isolated_filesystem(temp_dir=tmpdir):
                result = runner.invoke(app, ["queue", "get", "test.py::foo"])
                assert result.exit_code == 1
                response = json.loads(result.output)
                assert response["error"]["code"] == "DB_NOT_INITIALIZED"

    def test_queue_prioritize(self):
        """Test queue prioritize subcommand (requires init)."""
        import tempfile
        with tempfile.TemporaryDirectory() as tmpdir:
            with runner.isolated_filesystem(temp_dir=tmpdir):
                result = runner.invoke(app, ["queue", "prioritize", "test.py::foo", "1"])
                assert result.exit_code == 1
                response = json.loads(result.output)
                assert response["error"]["code"] == "DB_NOT_INITIALIZED"


class TestContractSubcommands:
    """Tests for contract subcommands."""

    def test_contract_get_help(self):
        """Test contract get subcommand exists and shows help."""
        result = runner.invoke(app, ["contract", "get", "--help"])
        assert result.exit_code == 0
        assert "function_id" in result.stdout.lower()

    def test_contract_get_requires_init(self):
        """Test contract get fails without initialization."""
        import tempfile
        with tempfile.TemporaryDirectory() as tmpdir:
            with runner.isolated_filesystem(temp_dir=tmpdir):
                result = runner.invoke(app, ["contract", "get", "test.py::foo"])
                # Should fail because DrSpec is not initialized
                assert result.exit_code == 1
                assert "DB_NOT_INITIALIZED" in result.stdout

    def test_contract_save_help(self):
        """Test contract save subcommand exists and shows help."""
        result = runner.invoke(app, ["contract", "save", "--help"])
        assert result.exit_code == 0
        assert "confidence" in result.stdout.lower()

    def test_contract_save_requires_confidence(self):
        """Test contract save requires --confidence option."""
        result = runner.invoke(app, ["contract", "save", "test.py::foo"])
        # Exit code 2 indicates missing required option
        assert result.exit_code == 2
        # Error message may be in stdout or output
        assert "confidence" in result.output.lower()

    def test_contract_list(self):
        """Test contract list subcommand."""
        result = runner.invoke(app, ["contract", "list"])
        assert result.exit_code == 0

    def test_contract_list_with_status_filter(self):
        """Test contract list with --status filter."""
        result = runner.invoke(app, ["contract", "list", "--status", "VERIFIED"])
        assert result.exit_code == 0


class TestSourceSubcommands:
    """Tests for source subcommands."""

    def test_source_get(self):
        """Test source get subcommand (requires init)."""
        import tempfile
        # Without init, should fail with DB_NOT_INITIALIZED
        with tempfile.TemporaryDirectory() as tmpdir:
            with runner.isolated_filesystem(temp_dir=tmpdir):
                result = runner.invoke(app, ["source", "get", "test.py::foo"])
                assert result.exit_code == 1
                response = json.loads(result.output)
                assert response["error"]["code"] == "DB_NOT_INITIALIZED"


class TestVerifySubcommands:
    """Tests for verify subcommands."""

    def test_verify_run_requires_contract(self):
        """Test verify run returns error without database."""
        result = runner.invoke(app, ["verify", "run", "test.py::foo"], input="{}")
        # Should fail because no database is initialized
        assert result.exit_code == 1
        response = json.loads(result.output)
        # Either DB_NOT_INITIALIZED or CONTRACT_NOT_FOUND
        assert response["success"] is False

    def test_verify_script_requires_contract(self):
        """Test verify script returns error without database."""
        result = runner.invoke(app, ["verify", "script", "test.py::foo"])
        # Should fail because no database is initialized
        assert result.exit_code == 1
        response = json.loads(result.output)
        assert response["success"] is False


class TestDepsSubcommands:
    """Tests for deps subcommands."""

    def test_deps_get(self):
        """Test deps get subcommand - expects failure without DB."""
        result = runner.invoke(app, ["deps", "get", "test.py::foo"])
        # Should fail because no database is initialized
        assert result.exit_code == 1

    def test_deps_get_with_depth(self):
        """Test deps get with --depth option - expects failure without DB."""
        result = runner.invoke(app, ["deps", "get", "--depth", "3", "test.py::foo"])
        # Should fail because no database is initialized
        assert result.exit_code == 1


class TestHelpText:
    """Tests for help text and documentation."""

    def test_main_help_shows_all_commands(self):
        """Test that main help shows all command groups."""
        result = runner.invoke(app, ["--help"])
        assert result.exit_code == 0

        # Check all command groups are listed
        expected_commands = ["init", "scan", "status", "queue", "contract", "source", "verify", "deps"]
        for cmd in expected_commands:
            assert cmd in result.stdout, f"Command '{cmd}' not found in help"

    def test_command_descriptions_present(self):
        """Test that command descriptions are shown."""
        result = runner.invoke(app, ["--help"])
        assert result.exit_code == 0

        # Should have descriptive text
        assert "AI-powered" in result.stdout or "contract" in result.stdout.lower()


class TestExitCodes:
    """Tests for proper exit codes."""

    def test_success_exit_code(self):
        """Test successful command returns exit code 0."""
        result = runner.invoke(app, ["init"])
        assert result.exit_code == 0

    def test_version_exit_code(self):
        """Test --version returns exit code 0."""
        result = runner.invoke(app, ["--version"])
        assert result.exit_code == 0

    def test_invalid_command_exit_code(self):
        """Test invalid command returns non-zero exit code."""
        result = runner.invoke(app, ["nonexistent-command"])
        assert result.exit_code != 0
