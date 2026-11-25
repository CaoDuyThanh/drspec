"""The YAML side and the literal side of `strings get`, as pure functions: the keys of a YAML text (flattened with `/`, lists kept as one
value, merge keys left out, a file that does not parse is a problem, a file over the size limit is not read), the normalized form of a key,
and the string literals of a code text (comments left out, `#` a comment only in Python, a run-time built key not a literal).
In memory: no file is scanned, no process runs; the one file test writes a tmp file."""

from __future__ import annotations

import sys

import pytest

from drspec.core import config_keys as ck

pytestmark = pytest.mark.unit

REPRO = "avoidance:\n  timeout: 5.0\n  retry:\n    count: 3\n"


def _keys(text: str) -> dict:
    return {key.key: (key.line, key.value) for key in ck.parse_config(text).keys}


class TestTheKeysOfAYamlText:
    def test_nested_mappings_are_flattened_with_slashes_and_their_lines(self):
        assert _keys(REPRO) == {"avoidance/timeout": (2, "5.0"), "avoidance/retry/count": (4, "3")}

    def test_a_list_keeps_its_key_and_its_scalars_as_one_value(self):
        assert _keys("gains:\n  - 1.0\n  - [3, 4]\n") == {"gains": (1, "[1.0, 3, 4]")}

    def test_an_empty_mapping_is_a_key_with_the_value_braces(self):
        assert _keys("box: {}\n") == {"box": (1, "{}")}

    def test_a_long_value_is_cut_to_eighty_characters(self):
        (key,) = ck.parse_config("name: " + "x" * 200 + "\n").keys
        assert len(key.value) == ck.VALUE_CHARACTERS and key.value.endswith("...")

    def test_a_dotted_key_is_stored_in_the_slash_form(self):
        assert _keys("avoidance.timeout: 5\n") == {"avoidance/timeout": (1, "5")}

    def test_a_merge_key_is_left_out_and_the_anchored_mapping_is_read_where_written(self):
        text = "base: &b\n  speed: 1\nderived:\n  <<: *b\n  dist: 2\n"
        assert set(_keys(text)) == {"base/speed", "derived/dist"}

    def test_several_documents_in_one_file_are_all_read(self):
        assert set(_keys("a: 1\n---\nb: 2\n")) == {"a", "b"}

    def test_a_file_that_does_not_parse_has_no_keys_and_a_problem_with_its_line(self):
        parsed = ck.parse_config("avoidance:\n  timeout: [5.0\n")
        assert parsed.keys == [] and parsed.skipped is None
        assert parsed.problem["line"] >= 2 and parsed.problem["message"]

    def test_a_file_over_the_size_limit_is_not_read_and_says_so(self):
        parsed = ck.parse_config(REPRO, size_kb=2048, max_kb=1024)
        assert parsed.keys == [] and parsed.skipped == "over 1024 KB (2048 KB): not read"

    def test_a_file_at_the_limit_is_read(self):
        assert ck.parse_config(REPRO, size_kb=1024, max_kb=1024).keys

    def test_a_tag_is_read_as_text_and_nothing_is_built_from_it(self):
        parsed = ck.parse_config("boom: !!python/object/apply:os.system ['echo x']\n")
        assert parsed.problem is None and [(key.key, key.value) for key in parsed.keys] == [("boom", "[echo x]")]

    def test_a_non_utf8_file_is_a_problem_not_a_failure(self, tmp_path):
        path = tmp_path / "bad.yaml"
        path.write_bytes(b"name: \xff\xfe\n")
        parsed = ck.read_config_file(path)
        assert parsed.problem is not None and "UTF-8" in parsed.problem["message"]

    def test_is_config_path_is_yaml_and_yml_only(self):
        assert ck.is_config_path("config/params.yaml") and ck.is_config_path("a/B.YML")
        assert not ck.is_config_path("a.yaml.bak") and not ck.is_config_path("a.json")


class TestTheNormalForm:
    @pytest.mark.parametrize(
        "written, normal",
        [
            ("avoidance/timeout", "avoidance/timeout"),
            ("avoidance.timeout", "avoidance/timeout"),
            ("/avoidance/timeout", "avoidance/timeout"),
            ("~/avoidance/timeout", "avoidance/timeout"),  # pattern-ok: outside-path the `~/` spelling of a key name (a key, not a filesystem path)
            ("  avoidance/timeout  ", "avoidance/timeout"),
            ("avoidance/", "avoidance/"),
        ],
    )
    def test_each_written_form_is_the_same_key(self, written, normal):
        assert ck.normalize_key(written) == normal


class TestTheStringLiteralsOfCode:
    def test_a_literal_equal_to_the_key_is_found_with_its_offset(self):
        text = 'void Node::init() { nh.getParam("avoidance/timeout", timeout_); }\n'
        ((key, offset),) = ck.literal_matches(text, "cpp", ["avoidance/timeout"])
        assert key == "avoidance/timeout" and text[offset] == '"'

    def test_a_comment_is_not_a_literal_in_c_and_in_python(self):
        assert ck.literal_matches('int x; // "avoidance/timeout"\n', "cpp", ["avoidance/timeout"]) == []
        assert ck.literal_matches('/* "avoidance/timeout" */ int x;\n', "cpp", ["avoidance/timeout"]) == []
        assert ck.literal_matches('x = 1  # "avoidance/timeout"\n', "python", ["avoidance/timeout"]) == []

    def test_a_hash_is_not_a_comment_in_c_so_a_define_is_read(self):
        assert ck.literal_matches('#define KEY "avoidance/timeout"\n', "cpp", ["avoidance/timeout"])

    def test_a_substring_or_a_longer_literal_is_not_the_key(self):
        assert ck.literal_matches('get("avoidance/timeout_s")', "cpp", ["avoidance/timeout"]) == []
        assert ck.literal_matches('get("timeout")', "cpp", ["avoidance/timeout"]) == []

    def test_a_key_built_at_run_time_is_not_a_literal(self):
        text = 'nh.getParam("avoidance/" + name, value);\n'
        assert ck.literal_matches(text, "cpp", ["avoidance/name"]) == []  # (the literal `avoidance/` is a prefix, which `strings get` never asks code for)

    def test_the_dotted_and_the_leading_slash_literal_are_the_same_key(self):
        text = 'a("avoidance.timeout"); b("/avoidance/timeout"); c("~/avoidance/timeout");\n'  # pattern-ok: outside-path the `~/` spelling of a key name (a key, not a filesystem path)
        assert [key for key, _offset in ck.literal_matches(text, "python", ["avoidance/timeout"])] == ["avoidance/timeout"] * 3

    def test_an_escaped_quote_is_undone_and_an_escaped_n_is_kept(self):
        values = [value for value, _offset in ck.literals_in('f("a\\"b"); g("x\\ny");', "cpp")]
        assert values == ['a"b', "x\\ny"]

    def test_no_keys_means_no_matches(self):
        assert ck.literal_matches('f("avoidance/timeout")', "cpp", []) == []


class TestTheFileIsCheckedBeforeItIsRead:
    def test_a_file_over_the_limit_is_skipped_with_its_size_and_is_not_read(self, tmp_path):
        path = tmp_path / "big.yaml"
        path.write_text("key: 1\n" * 400)  # about 3 KB
        parsed = ck.read_config_file(path, max_kb=1)
        assert parsed.keys == [] and parsed.problem is None and parsed.skipped.startswith("over 1 KB (") and parsed.skipped.endswith("KB): not read")

    def test_a_file_within_the_limit_is_read_whole(self, tmp_path):
        path = tmp_path / "small.yaml"
        path.write_text("key: 1\n")
        assert [key.key for key in ck.read_config_file(path, max_kb=1).keys] == ["key"]


class TestWithoutPyYAML:
    """PyYAML not importable (an install without it): nothing else of drspec breaks; the yaml files are skipped, and it says so."""

    @pytest.fixture
    def blocked(self, monkeypatch):
        monkeypatch.setitem(sys.modules, "yaml", None)  # `import yaml` now raises ImportError, as on an install without it

    def test_it_is_seen_as_missing_and_a_file_is_skipped_with_that_reason(self, blocked):
        assert ck.yaml_available() is False
        parsed = ck.parse_config(REPRO)
        assert parsed.keys == [] and parsed.problem is None and parsed.skipped == ck.YAML_MISSING

    def test_a_file_is_read_as_skipped_without_its_text_being_parsed(self, blocked, tmp_path):
        path = tmp_path / "params.yaml"
        path.write_text(REPRO)
        assert ck.read_config_file(path).skipped == ck.YAML_MISSING

    def test_the_rest_of_the_module_still_imports_and_the_code_side_does_not_need_it(self, blocked):
        import importlib

        module = importlib.reload(ck)  # (a fresh import of the module, with yaml blocked)
        assert module.literal_matches('nh.getParam("avoidance/timeout")', "cpp", ["avoidance/timeout"])
        assert module.is_config_path("a/b.yaml")

    def test_when_it_is_back_the_file_is_read_again(self, blocked, monkeypatch):
        monkeypatch.delitem(sys.modules, "yaml")  # (undone by monkeypatch at the end; the real module is imported again here)
        assert ck.yaml_available() is True and [key.key for key in ck.parse_config(REPRO).keys] == ["avoidance/timeout", "avoidance/retry/count"]


CAM_HEADER = "%YAML:1.0\n---\nwinSizeMax: 23\nwinSizeStep: 10\n"
CAM_MATRIX = (
    "%YAML:1.0\n---\nimage_width: 640\ncamera_matrix: !!opencv-matrix\n   rows: 3\n   cols: 3\n   dt: d\n"
    "   data: [ 500., 0., 320., 0., 500., 240., 0., 0., 1. ]\n"
)


class TestOpenCVStyleFiles:
    """OpenCV's FileStorage writes a `%YAML:1.0` first line and `!!opencv-matrix` tags: both are read, and every key keeps its file line."""

    def test_the_opencv_directive_line_is_blanked_and_the_keys_keep_their_lines(self):
        parsed = ck.parse_config(CAM_HEADER)
        assert parsed.problem is None and [(key.key, key.line, key.value) for key in parsed.keys] == [("winSizeMax", 3, "23"), ("winSizeStep", 4, "10")]

    def test_an_opencv_matrix_tag_is_read_as_its_mapping(self):
        parsed = ck.parse_config(CAM_MATRIX)
        assert parsed.problem is None
        assert [(key.key, key.line) for key in parsed.keys] == [
            ("image_width", 3), ("camera_matrix/rows", 5), ("camera_matrix/cols", 6), ("camera_matrix/dt", 7), ("camera_matrix/data", 8),
        ]

    def test_an_unknown_tag_on_a_scalar_and_on_a_sequence_is_read_as_the_node(self):
        parsed = ck.parse_config("a: !foo bar\nb: !foo [1, 2]\n")
        assert parsed.problem is None and [(key.key, key.value) for key in parsed.keys] == [("a", "bar"), ("b", "[1, 2]")]

    def test_the_standard_yaml_directive_is_unchanged(self):
        parsed = ck.parse_config("%YAML 1.1\n---\na: 1\n")
        assert parsed.problem is None and [(key.key, key.line) for key in parsed.keys] == [("a", 3)]

    def test_a_directive_that_is_not_the_first_line_is_not_blanked(self):
        assert ck.parse_config("a: 1\n%YAML:1.0\n").problem is not None

    def test_a_really_invalid_file_is_still_a_problem_with_its_line(self):
        parsed = ck.parse_config("- name: a\n  port: -\n")
        assert parsed.keys == [] and parsed.problem["line"] == 2 and parsed.problem["message"]

    def test_a_python_object_tag_still_builds_nothing(self):
        parsed = ck.parse_config("boom: !!python/object/apply:os.system ['echo x']\n")
        assert parsed.problem is None and [(key.key, key.value) for key in parsed.keys] == [("boom", "[echo x]")]


class TestTheRequeueOfYamlFiles:
    """A yaml file read by an older parser is read again once, at the next worker start (`facts_version`), then not again."""

    def test_the_yaml_files_are_queued_once_for_the_yaml_version(self):
        import duckdb

        from drspec.core import facts_version
        from drspec.db import init_schema
        from drspec.db.queries import list_staleness_queue

        conn = duckdb.connect(":memory:")
        try:
            init_schema(conn)
            conn.execute("INSERT INTO scanned_files (file_path, mtime, size) VALUES ('cam.yaml', 1.0, 10), ('b.yml', 1.0, 10), ('c.py', 1.0, 10)")
            facts_version.requeue_for_new_facts(conn)  # (the code families first: they are queued too)
            conn.execute("DELETE FROM staleness_queue")
            conn.execute("DELETE FROM config WHERE key = 'scan:facts:yaml'")  # what an index of the older parser has
            assert facts_version.requeue_for_new_facts(conn) >= 2
            queued = set(list_staleness_queue(conn))
            assert {"cam.yaml", "b.yml"} <= queued and "c.py" not in queued
            conn.execute("DELETE FROM staleness_queue")
            assert facts_version.requeue_for_new_facts(conn) == 0 and list_staleness_queue(conn) == []  # not again
        finally:
            conn.close()
