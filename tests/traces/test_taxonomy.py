import pytest
from plumb.traces.taxonomy import (
    categorize,
    file_path_from_input,
    file_paths_from_input,
    input_summary,
)


@pytest.mark.parametrize("name,expected", [
    ("Read", "Read"), ("Edit", "Edit"), ("Write", "Write"), ("NotebookEdit", "Write"),
    ("Bash", "Bash"), ("Grep", "Grep"), ("Glob", "Glob"), ("Task", "Task"), ("Agent", "Task"),
    ("Skill", "Tool"),
    ("shell_command", "Bash"), ("exec_command", "Bash"), ("shell", "Bash"), ("exec", "Bash"),
    ("apply_patch", "Edit"), ("list_files", "Read"), ("spawn_agent", "Task"),
    ("read_file", "Read"), ("read", "Read"), ("find", "Read"), ("view", "Read"),
    ("str_replace", "Edit"), ("edit", "Edit"), ("edit_file", "Edit"),
    ("create_file", "Write"), ("write", "Write"), ("write_file", "Write"),
    ("run_command", "Bash"), ("bash", "Bash"),
    ("grep", "Grep"), ("glob", "Glob"), ("report_intent", "Tool"),
    ("mcp__pencil__batch_get", "Tool"),
    ("something_new", "Other"),
])
def test_categorize(name, expected):
    assert categorize(name) == expected


def test_file_path_from_common_keys():
    assert file_path_from_input("Edit", {"file_path": "a.py"}) == "a.py"
    assert file_path_from_input("read", {"path": "b.py"}) == "b.py"
    assert file_path_from_input("Bash", {"command": "ls"}) is None


def test_file_path_from_apply_patch():
    patch = "*** Begin Patch\n*** Update File: src/x.py\n@@\n-a\n+b\n*** End Patch"
    assert file_path_from_input("apply_patch", {"input": patch}) == "src/x.py"
    assert file_path_from_input("apply_patch", patch) == "src/x.py"


def test_input_summary_prefers_command_then_path():
    assert input_summary("Bash", {"command": "pytest -x"}) == "pytest -x"
    assert input_summary("Edit", {"file_path": "a.py", "old_string": "x"}) == "a.py"
    assert len(input_summary("Bash", {"command": "x" * 500})) == 120
    assert input_summary("weird", "raw string arg") == "raw string arg"


def test_input_summary_apply_patch_uses_file():
    patch = "*** Begin Patch\n*** Update File: src/x.py\n@@\n+1\n*** End Patch"
    assert input_summary("apply_patch", {"input": patch}) == "src/x.py"
    assert input_summary("apply_patch", patch) == "src/x.py"


def test_input_summary_url_and_skill():
    assert input_summary("WebFetch", {"url": "https://x.io"}) == "https://x.io"
    assert input_summary("Skill", {"skill": "plumb"}) == "plumb"


def test_file_paths_from_multi_file_patch():
    patch = ("*** Begin Patch\n*** Update File: a.py\n@@\n+1\n"
             "*** Add File: b.py\n+x\n*** Delete File: c.py\n*** End Patch")
    assert file_paths_from_input("apply_patch", patch) == ["a.py", "b.py", "c.py"]
    assert file_path_from_input("apply_patch", patch) == "a.py"
    assert file_paths_from_input("Edit", {"file_path": "z.py"}) == ["z.py"]
    assert file_paths_from_input("Bash", {"command": "ls"}) == []
