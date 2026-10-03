import pytest

from ai_context_memory.chat import ChatSession
from ai_context_memory.cli import run
from ai_context_memory.llm import LLMError


class EchoLLM:
    def complete(self, messages):
        return f"echo: {messages[-1]['content']}"


class FailingLLM:
    def complete(self, messages):
        raise LLMError("接続できません")


def run_cli(llm, inputs):
    """inputsを順に入力として与え、(出力行のリスト, セッション) を返す。"""
    it = iter(inputs)

    def input_fn(prompt):
        value = next(it)
        if isinstance(value, BaseException):
            raise value
        return value

    outputs = []
    session = ChatSession(llm)
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
