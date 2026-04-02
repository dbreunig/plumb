"""Tests for ClaudeCodeLM — DSPy BaseLM subclass that routes through claude CLI."""

import subprocess
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from plumb.programs.claude_code_lm import (
    ClaudeCodeLM,
    _call_claude,
    _make_response,
    _serialize_messages,
    find_claude_cli,
)


class TestFindClaudeCli:
    def test_returns_path_when_found(self):
        with patch("shutil.which", return_value="/usr/local/bin/claude"):
            assert find_claude_cli() == "/usr/local/bin/claude"

    def test_returns_none_when_missing(self):
        with patch("shutil.which", return_value=None):
            assert find_claude_cli() is None


class TestSerializeMessages:
    def test_prompt_only(self):
        result = _serialize_messages(prompt="hello", messages=None)
        assert result == "hello"

    def test_single_user_message(self):
        msgs = [{"role": "user", "content": "hello"}]
        result = _serialize_messages(prompt=None, messages=msgs)
        assert result == "hello"

    def test_system_and_user_messages(self):
        msgs = [
            {"role": "system", "content": "You are helpful."},
            {"role": "user", "content": "hello"},
        ]
        result = _serialize_messages(prompt=None, messages=msgs)
        assert "<system>\nYou are helpful.\n</system>" in result
        assert "hello" in result

    def test_multi_turn_with_assistant(self):
        msgs = [
            {"role": "user", "content": "hi"},
            {"role": "assistant", "content": "hey"},
            {"role": "user", "content": "bye"},
        ]
        result = _serialize_messages(prompt=None, messages=msgs)
        assert "[user]\nhi" in result
        assert "[assistant]\nhey" in result
        assert "[user]\nbye" in result

    def test_empty_messages_falls_back_to_prompt(self):
        result = _serialize_messages(prompt="fallback", messages=[])
        assert result == "fallback"


class TestMakeResponse:
    def test_has_correct_structure(self):
        resp = _make_response("hello world", "claude-sonnet")
        assert resp.choices[0].message.content == "hello world"
        assert resp.choices[0].message.role == "assistant"
        assert resp.choices[0].finish_reason == "stop"
        assert resp.model == "claude-sonnet"

    def test_usage_is_dictable(self):
        resp = _make_response("text", "model")
        usage = dict(resp.usage)
        assert "prompt_tokens" in usage
        assert "completion_tokens" in usage
        assert "total_tokens" in usage


class TestCallClaude:
    def test_success(self):
        mock_result = subprocess.CompletedProcess(
            args=["claude"], returncode=0, stdout="hello\n", stderr=""
        )
        with patch("subprocess.run", return_value=mock_result) as mock_run:
            result = _call_claude("say hello")
            assert result == "hello\n"
            args = mock_run.call_args
            assert args[0][0][:2] == ["claude", "-p"]
            assert "--output-format" in args[0][0]
            assert "text" in args[0][0]
            assert args[1]["input"] == "say hello"

    def test_strips_claudecode_env_var(self):
        mock_result = subprocess.CompletedProcess(
            args=["claude"], returncode=0, stdout="ok", stderr=""
        )
        with patch("subprocess.run", return_value=mock_result) as mock_run, \
             patch.dict("os.environ", {"CLAUDECODE": "1", "PATH": "/usr/bin"}):
            _call_claude("test")
            env = mock_run.call_args[1]["env"]
            assert "CLAUDECODE" not in env
            assert "PATH" in env

    def test_passes_model_flag(self):
        mock_result = subprocess.CompletedProcess(
            args=["claude"], returncode=0, stdout="ok", stderr=""
        )
        with patch("subprocess.run", return_value=mock_result) as mock_run:
            _call_claude("test", model="opus")
            cmd = mock_run.call_args[0][0]
            assert "--model" in cmd
            idx = cmd.index("--model")
            assert cmd[idx + 1] == "opus"

    def test_raises_on_nonzero_exit(self):
        mock_result = subprocess.CompletedProcess(
            args=["claude"], returncode=1, stdout="", stderr="auth failed"
        )
        with patch("subprocess.run", return_value=mock_result):
            with pytest.raises(RuntimeError, match="auth failed"):
                _call_claude("test")

    def test_raises_on_timeout(self):
        with patch("subprocess.run", side_effect=subprocess.TimeoutExpired("claude", 300)):
            with pytest.raises(subprocess.TimeoutExpired):
                _call_claude("test")

    def test_strips_repo_local_git_env_vars(self):
        """Repo-local GIT_* vars must be stripped to prevent claude -p
        from corrupting a worktree's git index during pre-commit hooks.
        Safe transport/config vars (GIT_SSH, GIT_CONFIG_*, etc.) are kept."""
        mock_result = subprocess.CompletedProcess(
            args=["claude"], returncode=0, stdout="ok", stderr=""
        )
        with patch("subprocess.run", return_value=mock_result) as mock_run, \
             patch.dict("os.environ", {
                 "GIT_INDEX_FILE": "/tmp/.git/worktrees/wt/index",
                 "GIT_DIR": "/tmp/.git/worktrees/wt",
                 "GIT_WORK_TREE": "/tmp/worktree",
                 "GIT_SSH_COMMAND": "ssh -i ~/.ssh/id_rsa",
                 "GIT_CONFIG_COUNT": "1",
                 "GIT_CONFIG_KEY_0": "user.name",
                 "GIT_CONFIG_VALUE_0": "Test",
                 "PATH": "/usr/bin",
             }):
            _call_claude("test")
            env = mock_run.call_args[1]["env"]
            # Repo-local vars stripped
            assert "GIT_INDEX_FILE" not in env
            assert "GIT_DIR" not in env
            assert "GIT_WORK_TREE" not in env
            # Transport/config vars kept
            assert env["GIT_SSH_COMMAND"] == "ssh -i ~/.ssh/id_rsa"
            assert env["GIT_CONFIG_COUNT"] == "1"
            assert env["GIT_CONFIG_KEY_0"] == "user.name"
            assert env["GIT_CONFIG_VALUE_0"] == "Test"
            # Non-GIT vars kept
            assert "PATH" in env


class TestClaudeCodeLM:
    def test_is_base_lm_subclass(self):
        import dspy
        with patch("plumb.programs.claude_code_lm.find_claude_cli", return_value="/usr/bin/claude"):
            lm = ClaudeCodeLM()
            assert isinstance(lm, dspy.BaseLM)

    def test_forward_calls_claude_cli(self):
        mock_result = subprocess.CompletedProcess(
            args=["claude"], returncode=0, stdout="response text", stderr=""
        )
        with patch("plumb.programs.claude_code_lm.find_claude_cli", return_value="/usr/bin/claude"), \
             patch("subprocess.run", return_value=mock_result):
            lm = ClaudeCodeLM()
            response = lm.forward(prompt="hello")
            assert response.choices[0].message.content == "response text"

    def test_forward_serializes_messages(self):
        mock_result = subprocess.CompletedProcess(
            args=["claude"], returncode=0, stdout="answer", stderr=""
        )
        messages = [
            {"role": "system", "content": "Be concise."},
            {"role": "user", "content": "What is 1+1?"},
        ]
        with patch("plumb.programs.claude_code_lm.find_claude_cli", return_value="/usr/bin/claude"), \
             patch("subprocess.run", return_value=mock_result) as mock_run:
            lm = ClaudeCodeLM()
            lm.forward(messages=messages)
            stdin_input = mock_run.call_args[1]["input"]
            assert "Be concise." in stdin_input
            assert "What is 1+1?" in stdin_input

    def test_forward_raises_on_cli_error(self):
        mock_result = subprocess.CompletedProcess(
            args=["claude"], returncode=1, stdout="", stderr="error"
        )
        with patch("plumb.programs.claude_code_lm.find_claude_cli", return_value="/usr/bin/claude"), \
             patch("subprocess.run", return_value=mock_result):
            lm = ClaudeCodeLM()
            with pytest.raises(RuntimeError, match="error"):
                lm.forward(prompt="hello")

    def test_model_name_stored(self):
        with patch("plumb.programs.claude_code_lm.find_claude_cli", return_value="/usr/bin/claude"):
            lm = ClaudeCodeLM(model="opus")
            assert lm.cli_model == "opus"
            assert "claude-code/" in lm.model
