import pytest

from ai_context_memory.chat import ChatSession


class FakeLLM:
    """呼び出し時点の履歴を記録し、用意された応答を順に返す。"""

    def __init__(self, replies):
        self.replies = list(replies)
        self.calls = []

    def complete(self, messages, memories=(), working_memory=None, history_events=()):
        self.calls.append([dict(m) for m in messages])
        reply = self.replies.pop(0)
        if isinstance(reply, Exception):
            raise reply
        return reply


def test_send_returns_reply_and_records_both_turns():
    session = ChatSession(FakeLLM(["こんにちは"]))

    assert session.send("やあ") == "こんにちは"
    assert session.messages == [
        {"role": "user", "content": "やあ"},
        {"role": "assistant", "content": "こんにちは"},
    ]


def test_previous_turns_are_passed_to_llm():
    llm = FakeLLM(["了解", "有本さんです"])
    session = ChatSession(llm)

    session.send("私の名前は有本です")
    session.send("私の名前は？")

    assert llm.calls[1] == [
        {"role": "user", "content": "私の名前は有本です"},
        {"role": "assistant", "content": "了解"},
        {"role": "user", "content": "私の名前は？"},
    ]


def test_failed_send_does_not_leave_user_message_in_history():
    llm = FakeLLM(["一回目", RuntimeError("boom"), "三回目"])
    session = ChatSession(llm)
    session.send("1")

    with pytest.raises(RuntimeError):
        session.send("2")

    assert len(session.messages) == 2
    session.send("3")
    assert [m["role"] for m in session.messages] == ["user", "assistant"] * 2
