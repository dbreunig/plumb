import ast

from plumb.testgen import (
    clean_generated_tests,
    prune_failing_tests,
    sanitize_generated_tests,
    split_top_level_blocks,
)


GOOD = '''
def test_req_aaaaaaaa_ok(tmp_path):
    # plumb:req-aaaaaaaa
    from plumb.config import PlumbConfig
    assert PlumbConfig(spec_paths=["s.md"]).spec_paths == ["s.md"]
'''

BAD_SYNTAX = '''
def test_req_bbbbbbbb_broken(tmp_path):
    # plumb:req-bbbbbbbb
    x.y / "z" = 1
'''

BAD_IMPORT_IN_FUNC = '''
def test_req_cccccccc_missing(tmp_path):
    # plumb:req-cccccccc
    from plumb.conversation import read_conversation_log
    assert callable(read_conversation_log)
'''

FAILS_AT_RUNTIME = '''
def test_req_dddddddd_wrong_assertion():
    # plumb:req-dddddddd
    assert 1 == 2
'''

TRIPLE_QUOTED = '''
def test_req_eeeeeeee_docstring_with_column0_lines():
    """Spec says:
def not_a_real_def():
    pass
    """
    text = """
from nowhere import nothing
"""
    assert "nowhere" in text
'''

BAD_MODULE_IMPORT = "from plumb.cli import main\n"
GOOD_MODULE_IMPORT = "from plumb.config import PlumbConfig\n"
BAD_MODULE = "from plumb.does_not_exist import thing\n"


def test_split_keeps_decorators_and_markers_with_their_def():
    src = "import os\n\n# plumb:req-x\n@pytest.mark.slow\ndef test_a():\n    pass\n\ndef test_b():\n    pass\n"
    blocks, dropped = split_top_level_blocks(src)
    assert dropped == []
    assert blocks[0].strip() == "import os"
    assert blocks[1].startswith("# plumb:req-x\n@pytest.mark.slow\ndef test_a")
    assert blocks[2].startswith("def test_b")


def test_split_survives_column0_lines_inside_strings():
    blocks, dropped = split_top_level_blocks(TRIPLE_QUOTED + "\n" + GOOD)
    assert dropped == []
    assert len(blocks) == 2
    assert 'from nowhere import nothing' in blocks[0]


def test_split_excises_only_the_unparseable_block():
    blocks, dropped = split_top_level_blocks(GOOD + "\n" + BAD_SYNTAX + "\n" + TRIPLE_QUOTED)
    assert len(dropped) == 1 and "syntax error" in dropped[0] and "bbbbbbbb" in dropped[0]
    assert len(blocks) == 2
    assert "aaaaaaaa" in blocks[0] and "eeeeeeee" in blocks[1]


def test_keeps_good_code_verbatim():
    r = sanitize_generated_tests(GOOD)
    assert r.code.strip() == GOOD.strip()
    assert r.dropped == []
    assert r.kept_tests == 1
    ast.parse(r.code)


def test_drops_function_whose_plumb_import_does_not_resolve():
    r = sanitize_generated_tests(GOOD + "\n" + BAD_IMPORT_IN_FUNC)
    assert r.kept_tests == 1
    assert "read_conversation_log" not in r.code
    assert any("read_conversation_log" in d for d in r.dropped)


def test_drops_only_the_bad_module_level_import():
    r = sanitize_generated_tests(BAD_MODULE_IMPORT + GOOD_MODULE_IMPORT + BAD_MODULE + GOOD)
    assert "from plumb.cli import main" not in r.code
    assert "from plumb.does_not_exist" not in r.code
    assert "from plumb.config import PlumbConfig" in r.code
    assert r.kept_tests == 1
    assert len(r.dropped) == 2


def test_submodule_import_resolves():
    r = sanitize_generated_tests("from plumb.traces import claude\n" + GOOD)
    assert "from plumb.traces import claude" in r.code and r.dropped == []


def test_non_plumb_imports_are_not_checked():
    r = sanitize_generated_tests("from some_missing_third_party import x\n" + GOOD)
    assert "some_missing_third_party" in r.code and r.dropped == []


def test_empty_and_all_bad_yield_empty_code():
    assert sanitize_generated_tests("").code == ""
    r = sanitize_generated_tests(BAD_SYNTAX)
    assert r.code == "" and r.kept_tests == 0 and len(r.dropped) == 1


def test_prune_failing_tests_drops_runtime_failures(tmp_repo):
    tests_dir = tmp_repo / "tests"
    tests_dir.mkdir()
    r = prune_failing_tests(GOOD + "\n" + FAILS_AT_RUNTIME, tmp_repo, tests_dir)
    assert r.kept_tests == 1
    assert "dddddddd" not in r.code
    assert r.dropped == ["fails at runtime: test_req_dddddddd_wrong_assertion"]
    assert not list(tests_dir.glob("_plumb_gencheck_*"))  # temp file cleaned up


def test_clean_generated_tests_end_to_end(tmp_repo):
    tests_dir = tmp_repo / "tests"
    tests_dir.mkdir()
    r = clean_generated_tests(GOOD + BAD_SYNTAX + BAD_IMPORT_IN_FUNC + FAILS_AT_RUNTIME, tmp_repo, tests_dir)
    assert r.kept_tests == 1 and "aaaaaaaa" in r.code
    assert len(r.dropped) == 3


PARTIAL_IMPORT = "from plumb.conversation import (\n    chunk_conversation,\n    read_conversation_log,\n)\n"
CLASS_WITH_FAILING_METHOD = '''
class TestThings:
    def test_ok(self):
        assert True

    def test_bad(self):
        assert 1 == 2
'''
CLASS_ALL_FAILING = '''
class TestGone:
    def test_bad(self):
        assert 1 == 2
'''


def test_partial_import_keeps_resolvable_names():
    r = sanitize_generated_tests(PARTIAL_IMPORT + GOOD)
    assert "from plumb.conversation import chunk_conversation" in r.code
    assert "read_conversation_log" not in r.code
    assert len(r.dropped) == 1 and "read_conversation_log" in r.dropped[0]
    ast.parse(r.code)


def test_prune_removes_failing_methods_inside_classes(tmp_repo):
    tests_dir = tmp_repo / "tests"
    tests_dir.mkdir()
    r = prune_failing_tests(CLASS_WITH_FAILING_METHOD + CLASS_ALL_FAILING + GOOD, tmp_repo, tests_dir)
    assert "def test_ok" in r.code and "def test_bad" not in r.code
    assert "class TestGone" not in r.code
    assert "TestThings::test_bad" in " ".join(r.dropped) and "TestGone::test_bad" in " ".join(r.dropped)
    ast.parse(r.code)
