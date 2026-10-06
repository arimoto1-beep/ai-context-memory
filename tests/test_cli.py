import io

import pytest

from ai_context_memory.chat import ChatSession
from ai_context_memory import cli
from ai_context_memory.cli import run
from ai_context_memory.history import load_history
from ai_context_memory.llm import LLMError


class EchoLLM:
    def complete(self, messages, memories=(), working_memory=None):
        return f"echo: {messages[-1]['content']}"


class FailingLLM:
    def complete(self, messages, memories=(), working_memory=None):
        raise LLMError("接続できません")


def run_cli(llm, inputs, history_path=None):
    """inputsを順に入力として与え、(出力行のリスト, セッション) を返す。"""
    it = iter(inputs)

    def input_fn(prompt):
        value = next(it)
        if isinstance(value, BaseException):
            raise value
        return value

    outputs = []
    session = ChatSession(llm, history_path)
    run(session, input_fn=input_fn, output_fn=outputs.append)
    return outputs, session


@pytest.mark.parametrize("command", ["exit", "quit", "EXIT", "  quit  "])
def test_exit_commands_end_the_loop_without_calling_llm(command):
    outputs, session = run_cli(EchoLLM(), [command])

    assert session.messages == []
    assert outputs[-1] == "終了します。"


def test_reply_is_printed():
    outputs, _ = run_cli(EchoLLM(), ["こんにちは", "exit"])

    assert "\nAI> echo: こんにちは" in outputs


def test_empty_input_is_skipped():
    _, session = run_cli(EchoLLM(), ["", "   ", "exit"])

    assert session.messages == []


@pytest.mark.parametrize("interrupt", [EOFError(), KeyboardInterrupt()])
def test_eof_and_ctrl_c_at_prompt_end_the_loop(interrupt):
    outputs, _ = run_cli(EchoLLM(), [interrupt])

    assert outputs[-1] == "終了します。"


def test_llm_error_is_shown_and_loop_continues():
    outputs, session = run_cli(FailingLLM(), ["こんにちは", "もう一度", "exit"])

    assert outputs.count("[エラー] 接続できません") == 2
    assert session.messages == []


def test_exit_command_is_not_saved_to_history(tmp_path):
    path = tmp_path / "conversation.jsonl"

    run_cli(EchoLLM(), ["こんにちは", "exit"], path)

    assert load_history(path) == [
        {"role": "user", "content": "こんにちは"},
        {"role": "assistant", "content": "echo: こんにちは"},
    ]


def test_restart_starts_a_new_conversation_and_keeps_saved_history(tmp_path):
    path = tmp_path / "conversation.jsonl"
    run_cli(EchoLLM(), ["私の名前はテスト太郎です", "exit"], path)

    _, session = run_cli(EchoLLM(), ["前回私が名乗った名前は？", "exit"], path)

    # 再起動前の会話は現在の会話履歴へ戻さない（MVP3）
    assert [m["content"] for m in session.messages] == [
        "前回私が名乗った名前は？",
        "echo: 前回私が名乗った名前は？",
    ]
    # 原文は証拠としてすべて残る
    assert [m["content"] for m in load_history(path)] == [
        "私の名前はテスト太郎です",
        "echo: 私の名前はテスト太郎です",
        "前回私が名乗った名前は？",
        "echo: 前回私が名乗った名前は？",
    ]


def test_save_failure_shows_reply_and_error_and_loop_continues(tmp_path):
    # 親が通常ファイルなのでディレクトリを作れず、保存に失敗する
    blocker = tmp_path / "data"
    blocker.write_text("", encoding="utf-8")

    outputs, session = run_cli(
        EchoLLM(), ["こんにちは", "exit"], blocker / "conversation.jsonl"
    )

    assert "\nAI> echo: こんにちは" in outputs
    assert any(o.startswith("[エラー] 会話履歴を保存できませんでした") for o in outputs)
    assert outputs[-1] == "終了します。"
    assert len(session.messages) == 2


class FakeStdin(io.StringIO):
    def reconfigure(self, **kwargs):
        pass


@pytest.fixture
def main_env(tmp_path, monkeypatch):
    """main() を実データ領域にも実際の claude にも触れさせずに動かす。"""
    path = tmp_path / "memories.jsonl"
    monkeypatch.setenv("ACM_HISTORY_FILE", str(tmp_path / "conversation.jsonl"))
    monkeypatch.setenv("ACM_MEMORY_FILE", str(path))
    monkeypatch.setenv("ACM_WORKING_MEMORY_FILE", str(tmp_path / "working_memory.json"))
    monkeypatch.setattr(cli, "ClaudeCLI", EchoLLM)
    monkeypatch.setattr(cli.sys, "stdin", FakeStdin("exit\n"))
    monkeypatch.setattr(cli.sys.stdout, "reconfigure", lambda **kwargs: None, raising=False)
    return path


def test_main_reports_loaded_memories(main_env, capsys):
    main_env.write_text(
        '{"text": "ユーザーの名前はテスト太郎", "origin": "user", '
        '"evidence": "私の名前はテスト太郎です"}\n',
        encoding="utf-8",
    )

    assert cli.main() == 0
    assert "長期記憶を読み込みました（1 件）" in capsys.readouterr().out


def test_main_does_not_read_back_saved_conversation(main_env, capsys):
    # 原文ファイルは読み戻さないので、壊れていても起動できる
    (main_env.parent / "conversation.jsonl").write_text("壊れた行\n", encoding="utf-8")

    assert cli.main() == 0
    assert "チャットを開始します" in capsys.readouterr().out


def test_main_exits_with_error_on_broken_memory_file(main_env, capsys):
    main_env.write_text("壊れた行\n", encoding="utf-8")

    assert cli.main() == 1
    out = capsys.readouterr().out
    assert "[エラー]" in out
    assert "1 行目" in out
    assert "チャットを開始します" not in out


def test_main_reports_loaded_working_memory(main_env, capsys):
    (main_env.parent / "working_memory.json").write_text(
        '{"scope": [{"text": "商用環境", "evidence": "商用環境"}, '
        '{"text": "検証環境", "evidence": "検証環境"}], '
        '"acceptance_criteria": [{"text": "全環境で動けば完了", "evidence": "全環境で動けば完了"}]}',
        encoding="utf-8",
    )

    assert cli.main() == 0
    assert "作業記憶を読み込みました（3項目）" in capsys.readouterr().out


def test_main_exits_with_error_on_broken_working_memory_file(main_env, capsys):
    (main_env.parent / "working_memory.json").write_text("壊れた内容", encoding="utf-8")

    assert cli.main() == 1
    out = capsys.readouterr().out
    assert "[エラー] 作業記憶ファイル" in out
    assert "チャットを開始します" not in out
