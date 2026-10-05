import json

import pytest

from ai_context_memory.chat import ChatSession
from ai_context_memory.cli import run
from ai_context_memory.history import load_history
from ai_context_memory.llm import LLMError, format_prompt
from ai_context_memory.memory import MemoryStoreError, append_memories, load_memories

NAME_TEXT = "私の名前はテスト太郎です"
NAME_MEMORY = {"text": "ユーザーの名前はテスト太郎", "origin": "user", "evidence": NAME_TEXT}


def extraction(*candidates):
    """抽出AIの出力（JSON配列の文字列）を作る。"""
    return json.dumps(list(candidates), ensure_ascii=False)


NAME_EXTRACTION = extraction({"text": "ユーザーの名前はテスト太郎", "evidence": NAME_TEXT})


class FakeLLM:
    """通常回答・記憶抽出・検索プラン作成の呼び出しを別々に記録し、用意された応答を順に返す。"""

    def __init__(self, replies, extractions=(), plans=()):
        self.replies = list(replies)
        self.extractions = list(extractions)
        self.plans = list(plans)  # 尽きたら「記憶は不要」を返す
        self.prompts = []  # 通常回答でclaudeへ渡されるプロンプト
        self.extract_calls = []  # 記憶抽出の入力

    def complete(self, messages, memories=()):
        self.prompts.append(format_prompt(messages, memories))
        return self.replies.pop(0)

    def plan_search(self, user_text, previous_queries=()):
        return self.plans.pop(0) if self.plans else '{"needs_memory": false, "queries": []}'

    def extract_memories(self, user_text):
        self.extract_calls.append(user_text)
        result = self.extractions.pop(0)
        if isinstance(result, Exception):
            raise result
        return result


@pytest.fixture
def paths(tmp_path):
    return tmp_path / "data" / "conversation.jsonl", tmp_path / "data" / "memories.jsonl"


def turn(session, user_text):
    """CLIの1ターンと同じ順序: 記憶の検索、通常回答、記憶抽出。"""
    found, _, _ = session.recall(user_text)
    reply = session.send(user_text, found)
    saved, warnings = session.remember(user_text)
    return reply, saved, warnings


def run_cli(session, inputs):
    it = iter(inputs)
    outputs = []
    run(session, input_fn=lambda prompt: next(it), output_fn=outputs.append)
    return outputs


# --- 記憶ファイル ---


def test_missing_or_empty_file_loads_as_no_memories(paths):
    _, memory_path = paths
    assert load_memories(memory_path) == []

    memory_path.parent.mkdir()
    memory_path.write_text("", encoding="utf-8")
    assert load_memories(memory_path) == []


def test_file_is_jsonl_with_text_origin_and_evidence(paths):
    _, memory_path = paths

    append_memories(memory_path, [NAME_MEMORY])

    assert memory_path.read_text(encoding="utf-8").splitlines() == [
        '{"text": "ユーザーの名前はテスト太郎", "origin": "user", '
        '"evidence": "私の名前はテスト太郎です"}'
    ]
    assert load_memories(memory_path) == [NAME_MEMORY]


@pytest.mark.parametrize(
    "line",
    [
        "壊れた行",
        '"ただの文字列"',
        '{"text": "x", "origin": "user"}',
        '{"text": "x", "origin": "assistant", "evidence": "x"}',
        '{"text": 1, "origin": "user", "evidence": "x"}',
    ],
)
def test_broken_memory_line_is_reported_with_line_number(paths, line):
    _, memory_path = paths
    memory_path.parent.mkdir()
    memory_path.write_text(line + "\n", encoding="utf-8")

    with pytest.raises(MemoryStoreError, match="1 行目"):
        load_memories(memory_path)


# --- 抽出と保存 ---


def test_memory_is_saved_from_user_utterance(paths):
    history_path, memory_path = paths
    llm = FakeLLM(["テスト太郎さん、はじめまして。"], [NAME_EXTRACTION])
    session = ChatSession(llm, history_path, memory_path)

    reply, saved, warnings = turn(session, NAME_TEXT)

    assert reply == "テスト太郎さん、はじめまして。"
    assert saved == [NAME_MEMORY]
    assert warnings == []
    assert load_memories(memory_path) == [NAME_MEMORY]
    # 原文は別ファイルにそのまま残る
    assert [m["content"] for m in load_history(history_path)] == [
        NAME_TEXT,
        "テスト太郎さん、はじめまして。",
    ]


def test_origin_is_decided_by_python_not_by_the_extractor(paths):
    history_path, memory_path = paths
    llm = FakeLLM(
        ["了解"],
        [extraction({"text": "ユーザーの名前はテスト太郎", "evidence": NAME_TEXT, "origin": "assistant"})],
    )
    session = ChatSession(llm, history_path, memory_path)

    turn(session, NAME_TEXT)

    assert load_memories(memory_path) == [NAME_MEMORY]


def test_assistant_utterance_is_not_turned_into_memory(paths):
    history_path, memory_path = paths
    reply_text = "テスト太郎さんですね。好きな色は青にしておきます。"
    llm = FakeLLM(
        [reply_text],
        # assistantの発言を根拠にした候補を返してきても、ユーザー発言に無いので保存しない
        [extraction({"text": "ユーザーの好きな色は青", "evidence": "好きな色は青にしておきます"})],
    )
    session = ChatSession(llm, history_path, memory_path)

    _, saved, warnings = turn(session, NAME_TEXT)

    # 抽出の入力はユーザー発言だけで、assistantの発言は渡らない
    assert llm.extract_calls == [NAME_TEXT]
    assert saved == []
    assert len(warnings) == 1
    assert load_memories(memory_path) == []


def test_candidate_whose_evidence_is_not_in_user_text_is_not_saved(paths):
    history_path, memory_path = paths
    llm = FakeLLM(
        ["了解"],
        [
            extraction(
                {"text": "ユーザーの名前はテスト太郎", "evidence": NAME_TEXT},
                {"text": "ユーザーは東京在住", "evidence": "東京に住んでいます"},
            )
        ],
    )
    session = ChatSession(llm, history_path, memory_path)

    _, saved, warnings = turn(session, NAME_TEXT)

    # 検証を通った候補だけが保存される
    assert saved == [NAME_MEMORY]
    assert load_memories(memory_path) == [NAME_MEMORY]
    assert len(warnings) == 1
    assert "evidence" in warnings[0] and "東京に住んでいます" in warnings[0]


@pytest.mark.parametrize(
    "candidate",
    [
        "ただの文字列",
        {"text": "ユーザーの名前はテスト太郎"},
        {"evidence": NAME_TEXT},
        {"text": "", "evidence": NAME_TEXT},
        {"text": "ユーザーの名前はテスト太郎", "evidence": ""},
        {"text": "ユーザーの名前はテスト太郎", "evidence": 1},
    ],
)
def test_malformed_candidate_is_not_saved(paths, candidate):
    history_path, memory_path = paths
    session = ChatSession(FakeLLM(["了解"], [extraction(candidate)]), history_path, memory_path)

    _, saved, warnings = turn(session, NAME_TEXT)

    assert saved == []
    assert len(warnings) == 1
    assert not memory_path.exists()


def test_empty_extraction_saves_nothing(paths):
    history_path, memory_path = paths
    session = ChatSession(FakeLLM(["こんにちは"], ["[]"]), history_path, memory_path)

    _, saved, warnings = turn(session, "こんにちは")

    assert saved == []
    assert warnings == []
    assert not memory_path.exists()


def test_extraction_wrapped_in_code_fence_is_accepted(paths):
    history_path, memory_path = paths
    llm = FakeLLM(["了解"], [f"```json\n{NAME_EXTRACTION}\n```"])
    session = ChatSession(llm, history_path, memory_path)

    turn(session, NAME_TEXT)

    assert load_memories(memory_path) == [NAME_MEMORY]


def test_memory_with_same_text_is_not_saved_twice(paths):
    history_path, memory_path = paths
    llm = FakeLLM(["了解", "了解"], [NAME_EXTRACTION, NAME_EXTRACTION])
    session = ChatSession(llm, history_path, memory_path)

    turn(session, NAME_TEXT)
    _, saved, warnings = turn(session, NAME_TEXT)

    assert saved == []
    assert warnings == []
    assert load_memories(memory_path) == [NAME_MEMORY]


# --- 抽出の失敗は会話を止めない ---


@pytest.mark.parametrize(
    "bad_extraction",
    [
        "名前を記憶しました。",
        '[{"text": "ユーザーの名前は',
        '{"text": "ユーザーの名前はテスト太郎", "evidence": "私の名前はテスト太郎です"}',
        LLMError("接続できません"),
    ],
)
def test_failed_extraction_does_not_fail_the_conversation(paths, bad_extraction):
    history_path, memory_path = paths
    llm = FakeLLM(["はじめまして", "二回目の応答"], [bad_extraction, "[]"])
    session = ChatSession(llm, history_path, memory_path)

    outputs = run_cli(session, [NAME_TEXT, "続けます", "exit"])

    assert "\nAI> はじめまして" in outputs
    assert any(o.startswith("[警告] 記憶を抽出できませんでした") for o in outputs)
    # 会話は続行でき、原文も保存されている
    assert "\nAI> 二回目の応答" in outputs
    assert outputs[-1] == "終了します。"
    assert len(load_history(history_path)) == 4
    assert not memory_path.exists()


def test_memory_save_failure_is_a_warning_and_conversation_continues(tmp_path):
    history_path = tmp_path / "conversation.jsonl"
    # 親が通常ファイルなのでディレクトリを作れず、保存に失敗する
    blocker = tmp_path / "blocked"
    blocker.write_text("", encoding="utf-8")
    llm = FakeLLM(["はじめまして", "二回目の応答"], [NAME_EXTRACTION, "[]"])
    session = ChatSession(llm, history_path, blocker / "memories.jsonl")

    outputs = run_cli(session, [NAME_TEXT, "続けます", "exit"])

    assert any(o.startswith("[警告] 記憶を保存できませんでした") for o in outputs)
    assert "\nAI> 二回目の応答" in outputs
    # 保存できなかった記憶は、メモリ上にも持たない
    assert session.memories == []


def test_saved_memory_is_shown_in_cli(paths):
    history_path, memory_path = paths
    session = ChatSession(FakeLLM(["はじめまして"], [NAME_EXTRACTION]), history_path, memory_path)

    outputs = run_cli(session, [NAME_TEXT, "exit"])

    assert "[記憶] ユーザーの名前はテスト太郎" in outputs


def test_no_extraction_when_reply_failed_or_on_exit(paths):
    history_path, memory_path = paths

    class FailingLLM(FakeLLM):
        def complete(self, messages, memories=()):
            raise LLMError("接続できません")

    llm = FailingLLM([])
    session = ChatSession(llm, history_path, memory_path)

    outputs = run_cli(session, [NAME_TEXT, "quit"])

    assert "[エラー] 接続できません" in outputs
    assert llm.extract_calls == []
    assert outputs[-1] == "終了します。"


# --- 再起動後にClaudeへ渡すもの ---


def test_after_restart_memories_are_sent_but_old_conversation_is_not(paths):
    """再起動相当: 新しいChatSessionを同じファイルで生成する。"""
    history_path, memory_path = paths
    first_reply = "テスト太郎さん、はじめまして。よろしくお願いします。"
    first = ChatSession(FakeLLM([first_reply], [NAME_EXTRACTION]), history_path, memory_path)
    turn(first, NAME_TEXT)
    del first

    llm = FakeLLM(["テスト太郎さんです。"], ["[]"], ['{"needs_memory": true, "queries": ["名前"]}'])
    second = ChatSession(llm, history_path, memory_path)

    assert second.messages == []
    assert second.memories == [NAME_MEMORY]

    turn(second, "私の名前は？")

    prompt = llm.prompts[0]
    # 古い会話（ユーザー発言・assistant応答）は会話履歴として通常回答用プロンプトに含まれない
    assert f"[user]\n{NAME_TEXT}" not in prompt
    assert first_reply not in prompt
    assert "[assistant]" not in prompt
    # 検索でヒットした記憶は別枠で含まれる（過去の発言は根拠として付くだけ）
    assert prompt == (
        "[retrieved memories]\n"
        "- ユーザーの名前はテスト太郎 (origin: user, evidence: 私の名前はテスト太郎です)\n"
        "[/retrieved memories]\n\n"
        "[user]\n私の名前は？"
    )
    # 原文は証拠として残り、再起動後のターンも追記される
    assert [m["content"] for m in load_history(history_path)] == [
        NAME_TEXT,
        first_reply,
        "私の名前は？",
        "テスト太郎さんです。",
    ]


def test_conversation_in_current_process_is_still_kept(paths):
    history_path, memory_path = paths
    memory_path.parent.mkdir()
    append_memories(memory_path, [NAME_MEMORY])
    llm = FakeLLM(["了解しました", "AとBです"], ["[]", "[]"])
    session = ChatSession(llm, history_path, memory_path)

    turn(session, "今回の試験対象はAとBです")
    turn(session, "今回の試験対象は？")

    # 検索にヒットしていない保存済みの記憶は渡らず、現在の会話はすべて渡る
    assert llm.prompts[1] == (
        "[user]\n今回の試験対象はAとBです\n\n"
        "[assistant]\n了解しました\n\n"
        "[user]\n今回の試験対象は？"
    )


def test_session_without_memory_path_does_not_extract():
    llm = FakeLLM(["こんにちは"])
    session = ChatSession(llm)

    _, saved, warnings = turn(session, "やあ")

    assert (saved, warnings) == ([], [])
    assert llm.extract_calls == []
