import pytest

from ai_context_memory.chat import ChatSession
from ai_context_memory.history import HistoryError, append_history, load_history

TURN = [
    {"role": "user", "content": "私の名前はテスト太郎です"},
    {"role": "assistant", "content": "テスト太郎さん、はじめまして。"},
]


class FakeLLM:
    """呼び出し時点の履歴を記録し、用意された応答を順に返す。"""

    def __init__(self, replies):
        self.replies = list(replies)
        self.calls = []

    def complete(self, messages):
        self.calls.append([dict(m) for m in messages])
        reply = self.replies.pop(0)
        if isinstance(reply, Exception):
            raise reply
        return reply


@pytest.fixture
def path(tmp_path):
    return tmp_path / "data" / "conversation.jsonl"


def test_missing_file_loads_as_empty_history(path):
    assert load_history(path) == []
    assert not path.exists()


def test_empty_file_loads_as_empty_history(path):
    path.parent.mkdir()
    path.write_text("", encoding="utf-8")

    assert load_history(path) == []


def test_appended_messages_can_be_loaded_back(path):
    append_history(path, TURN)

    assert load_history(path) == TURN


def test_file_is_jsonl_with_one_readable_message_per_line(path):
    append_history(path, TURN)

    assert path.read_text(encoding="utf-8").splitlines() == [
        '{"role": "user", "content": "私の名前はテスト太郎です"}',
        '{"role": "assistant", "content": "テスト太郎さん、はじめまして。"}',
    ]


def test_multiline_content_survives_round_trip(path):
    messages = [{"role": "user", "content": "1行目\n2行目\n\n4行目"}]

    append_history(path, messages)

    assert load_history(path) == messages
    assert len(path.read_text(encoding="utf-8").splitlines()) == 1


def test_broken_json_line_is_reported_with_line_number(path):
    append_history(path, TURN)
    with open(path, "a", encoding="utf-8") as f:
        f.write('{"role": "user", "content": \n')

    with pytest.raises(HistoryError, match="3 行目"):
        load_history(path)


@pytest.mark.parametrize(
    "line",
    [
        '"ただの文字列"',
        '{"role": "system", "content": "x"}',
        '{"role": "user"}',
        '{"role": "user", "content": 123}',
    ],
)
def test_line_with_unexpected_shape_is_rejected(path, line):
    path.parent.mkdir()
    path.write_text(line + "\n", encoding="utf-8")

    with pytest.raises(HistoryError, match="1 行目"):
        load_history(path)


def test_save_failure_becomes_history_error(tmp_path):
    # 親が通常ファイルなのでディレクトリを作れず、保存に失敗する
    blocker = tmp_path / "data"
    blocker.write_text("", encoding="utf-8")

    with pytest.raises(HistoryError, match="保存できませんでした"):
        append_history(blocker / "conversation.jsonl", TURN)


def test_session_saves_each_turn_after_reply(path):
    session = ChatSession(FakeLLM(["テスト太郎さん、はじめまして。"]), path)

    session.send("私の名前はテスト太郎です")

    assert load_history(path) == TURN


def test_failed_turn_is_not_saved(path):
    session = ChatSession(FakeLLM([RuntimeError("boom")]), path)

    with pytest.raises(RuntimeError):
        session.send("こんにちは")

    assert not path.exists()


def test_history_is_restored_after_restart(path):
    """再起動相当: 保存 → 新しいChatSessionを生成 → 同じファイルから復元。"""
    first = ChatSession(FakeLLM(["テスト太郎さん、はじめまして。"]), path)
    first.send("私の名前はテスト太郎です")
    del first

    llm = FakeLLM(["テスト太郎さんです。"])
    second = ChatSession(llm, path)

    assert second.messages == TURN

    second.send("前回私が名乗った名前は？")

    # 復元した履歴も含めて、全履歴がLLMへ渡される
    assert llm.calls[0] == TURN + [{"role": "user", "content": "前回私が名乗った名前は？"}]
    # 再起動後のターンは既存の履歴の後ろへ追記される
    assert load_history(path) == TURN + [
        {"role": "user", "content": "前回私が名乗った名前は？"},
        {"role": "assistant", "content": "テスト太郎さんです。"},
    ]


def test_session_with_broken_history_file_fails_to_start(path):
    path.parent.mkdir()
    path.write_text("壊れた行\n", encoding="utf-8")

    with pytest.raises(HistoryError):
        ChatSession(FakeLLM([]), path)

    # 壊れたファイルには手を加えない
    assert path.read_text(encoding="utf-8") == "壊れた行\n"
