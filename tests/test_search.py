import json

import pytest

from ai_context_memory.chat import ChatSession
from ai_context_memory.cli import run
from ai_context_memory.history import append_history, load_history
from ai_context_memory.llm import LLMError, format_prompt
from ai_context_memory.memory import append_memories, load_memories
from ai_context_memory.search import (
    MAX_QUERIES,
    MAX_RESULTS,
    SearchPlanError,
    parse_search_plan,
    search_memories,
)


def memory(text, evidence):
    return {"text": text, "origin": "user", "evidence": evidence}


NAME = memory("ユーザーの名前はテスト太郎", "私の名前はテスト太郎です")
CAR = memory("ユーザーはヴェゼルに乗る予定である", "ヴェゼルに乗る予定です")
FRUIT = memory("ユーザーの好きな果物は梨", "好きな果物は梨です")
TIRE = memory("ユーザーは冬タイヤを16インチで検討している", "冬タイヤは16インチで考えています")
MEMORIES = [NAME, CAR, FRUIT, TIRE]

NO_MEMORY_PLAN = '{"needs_memory": false, "queries": []}'


def plan(*queries):
    """検索プランナーの出力（JSONオブジェクトの文字列）を作る。"""
    return json.dumps({"needs_memory": True, "queries": list(queries)}, ensure_ascii=False)


class FakeLLM:
    """通常回答・検索プラン作成・記憶抽出の呼び出しを別々に記録し、用意された応答を順に返す。"""

    def __init__(self, replies, plans=(), extractions=()):
        self.replies = list(replies)
        self.plans = list(plans)
        self.extractions = list(extractions)  # 尽きたら「記憶なし」を返す
        self.prompts = []  # 通常回答でclaudeへ渡されるプロンプト
        self.plan_calls = []  # 検索プラン作成の入力 (ユーザー発言, 0件だった検索語)
        self.extract_calls = []

    def complete(self, messages, memories=(), working_memory=None, history_events=()):
        self.prompts.append(format_prompt(messages, memories, working_memory))
        return self.replies.pop(0)

    def plan_search(self, user_text, previous_queries=(), working_memory=None):
        self.plan_calls.append((user_text, list(previous_queries)))
        result = self.plans.pop(0)
        if isinstance(result, Exception):
            raise result
        return result

    def extract_memories(self, user_text):
        self.extract_calls.append(user_text)
        return self.extractions.pop(0) if self.extractions else "[]"


@pytest.fixture
def paths(tmp_path):
    """4件の記憶が保存済みの状態。"""
    history_path = tmp_path / "data" / "conversation.jsonl"
    memory_path = tmp_path / "data" / "memories.jsonl"
    append_memories(memory_path, MEMORIES)
    return history_path, memory_path


def run_cli(session, inputs):
    it = iter(inputs)
    outputs = []
    run(session, input_fn=lambda prompt: next(it), output_fn=outputs.append)
    return outputs


# --- 検索関数 ---


def test_search_matches_text():
    assert search_memories(MEMORIES, ["名前"]) == [NAME]


def test_search_matches_evidence():
    # 「考えています」は evidence にだけ現れる
    assert search_memories(MEMORIES, ["考えています"]) == [TIRE]


def test_search_returns_only_related_memories():
    assert search_memories(MEMORIES, ["ヴェゼル"]) == [CAR]
    assert search_memories(MEMORIES, ["スタッドレス"]) == []


def test_all_keywords_in_a_query_must_match():
    # 「ユーザー」は全記憶に含まれるが、もう一方のキーワードで絞られる
    assert search_memories(MEMORIES, ["ユーザー 名前"]) == [NAME]
    assert search_memories(MEMORIES, ["冬タイヤ 16インチ"]) == [TIRE]
    assert search_memories(MEMORIES, ["冬タイヤ 17インチ"]) == []


def test_any_query_may_match_and_higher_score_comes_first():
    # 梨の記憶は2つの検索語にヒットするので、保存順が後でも先に来る
    assert search_memories(MEMORIES, ["名前", "果物", "梨"]) == [FRUIT, NAME]


def test_search_ignores_case_and_character_width():
    memories = [memory("ユーザーはPythonを使っている", "Ｐｙｔｈｏｎを使っています")]

    assert search_memories(memories, ["python"]) == memories
    assert search_memories(MEMORIES, ["１６インチ"]) == [TIRE]


def test_search_does_not_exceed_limit():
    memories = [memory(f"ユーザーの猫{i}号の名前はタマ{i}", f"猫{i}号はタマ{i}です") for i in range(8)]

    assert search_memories(memories, ["猫"]) == memories[:MAX_RESULTS]
    assert search_memories(memories, ["猫"], limit=2) == memories[:2]


def test_search_with_no_or_blank_queries_returns_nothing():
    assert search_memories(MEMORIES, []) == []
    assert search_memories(MEMORIES, ["", "   "]) == []


# --- 検索プランの解析 ---


def test_search_plan_json_is_parsed():
    assert parse_search_plan('{"needs_memory": true, "queries": ["名前", "ユーザー 名前"]}') == [
        "名前",
        "ユーザー 名前",
    ]


def test_search_plan_wrapped_in_code_fence_is_accepted():
    assert parse_search_plan(f"```json\n{plan('名前')}\n```") == ["名前"]


def test_search_plan_without_need_for_memory_has_no_queries():
    assert parse_search_plan(NO_MEMORY_PLAN) == []
    # needs_memory が false なら、検索語が付いていても使わない
    assert parse_search_plan('{"needs_memory": false, "queries": ["名前"]}') == []


def test_search_plan_queries_are_cleaned_deduplicated_and_capped():
    queries = parse_search_plan(plan("名前", "  名前 ", "", "ユーザー　 名前", "氏名", "呼び名"))

    assert queries == ["名前", "ユーザー 名前", "氏名"]
    assert len(queries) == MAX_QUERIES


@pytest.mark.parametrize(
    "raw",
    [
        "名前で検索します。",
        '{"needs_memory": true, "queries": ["名前"',
        '["名前"]',
        '{"needs_memory": true, "queries": "名前"}',
        '{"needs_memory": true, "queries": ["名前", 1]}',
    ],
)
def test_invalid_search_plan_is_rejected(raw):
    with pytest.raises(SearchPlanError):
        parse_search_plan(raw)


# --- 検索してから回答する ---


def test_only_hit_memories_are_sent_to_the_answering_llm(paths):
    history_path, memory_path = paths
    llm = FakeLLM(["テスト太郎さんです。"], [plan("名前", "氏名")])
    session = ChatSession(llm, history_path, memory_path)

    outputs = run_cli(session, ["私の名前は？", "exit"])

    # 検索語は現在の質問だけからLLMに考えさせる
    assert llm.plan_calls == [("私の名前は？", [])]
    # ヒットした記憶だけが渡り、無関係な記憶（全件）は渡らない
    assert llm.prompts == [
        "[retrieved memories]\n"
        "- ユーザーの名前はテスト太郎 (origin: user, evidence: 私の名前はテスト太郎です)\n"
        "[/retrieved memories]\n\n"
        "[user]\n私の名前は？"
    ]
    for unrelated in ("ヴェゼル", "梨", "冬タイヤ"):
        assert unrelated not in llm.prompts[0]
    assert "[検索] 名前 / 氏名" in outputs
    assert "[検索結果] 1件" in outputs
    assert "\nAI> テスト太郎さんです。" in outputs


def test_no_search_when_planner_says_memory_is_not_needed(paths):
    history_path, memory_path = paths
    llm = FakeLLM(["2です。"], [NO_MEMORY_PLAN])
    session = ChatSession(llm, history_path, memory_path)

    outputs = run_cli(session, ["1+1は？", "exit"])

    assert len(llm.plan_calls) == 1
    assert llm.prompts == ["[user]\n1+1は？"]
    assert not any("検索" in o for o in outputs)
    assert "\nAI> 2です。" in outputs


def test_planner_is_not_called_when_there_are_no_memories(tmp_path):
    llm = FakeLLM(["はじめまして"])
    session = ChatSession(llm, tmp_path / "conversation.jsonl", tmp_path / "memories.jsonl")

    run_cli(session, ["私の名前は？", "exit"])

    assert llm.plan_calls == []
    assert llm.prompts == ["[user]\n私の名前は？"]


def test_search_is_retried_once_when_first_round_has_no_hits(paths):
    history_path, memory_path = paths
    llm = FakeLLM(["16インチです。"], [plan("冬用タイヤ", "スタッドレス"), plan("冬タイヤ")])
    session = ChatSession(llm, history_path, memory_path)

    outputs = run_cli(session, ["冬用のタイヤ、何インチで考えてたっけ？", "exit"])

    # 2回目のプランナーには、元の質問と0件だった検索語だけを渡す
    assert llm.plan_calls == [
        ("冬用のタイヤ、何インチで考えてたっけ？", []),
        ("冬用のタイヤ、何インチで考えてたっけ？", ["冬用タイヤ", "スタッドレス"]),
    ]
    assert "ユーザーは冬タイヤを16インチで検討している" in llm.prompts[0]
    assert "テスト太郎" not in llm.prompts[0]
    assert outputs[1:5] == [
        "[検索] 冬用タイヤ / スタッドレス",
        "[検索結果] 0件",
        "[再検索] 冬タイヤ",
        "[検索結果] 1件",
    ]


def test_search_stops_after_second_round_without_hits(paths):
    history_path, memory_path = paths
    # 3つ目のプランは使われないはず
    llm = FakeLLM(["覚えていません。"], [plan("血液型"), plan("血液"), plan("名前")])
    session = ChatSession(llm, history_path, memory_path)

    outputs = run_cli(session, ["私の血液型は？", "exit"])

    assert len(llm.plan_calls) == 2
    assert llm.prompts == ["[user]\n私の血液型は？"]
    assert outputs.count("[検索結果] 0件") == 2
    assert "\nAI> 覚えていません。" in outputs


def test_no_retry_when_first_round_has_hits(paths):
    history_path, memory_path = paths
    llm = FakeLLM(["梨です。"], [plan("果物"), plan("梨")])
    session = ChatSession(llm, history_path, memory_path)

    run_cli(session, ["好きな果物は？", "exit"])

    assert len(llm.plan_calls) == 1


def test_already_used_queries_are_not_searched_again(paths):
    history_path, memory_path = paths
    llm = FakeLLM(["覚えていません。"], [plan("血液型"), plan("血液型", " 血液型 ")])
    session = ChatSession(llm, history_path, memory_path)

    found, log, warnings = session.recall("私の血液型は？")

    # 2回目のプランが使用済みの検索語だけなら、検索は実行しない
    assert log == [(["血液型"], 0)]
    assert (found, warnings) == ([], [])


def test_only_new_queries_are_searched_in_second_round(paths):
    history_path, memory_path = paths
    llm = FakeLLM([], [plan("冬用タイヤ"), plan("冬用タイヤ", "冬タイヤ")])
    session = ChatSession(llm, history_path, memory_path)

    found, log, _ = session.recall("冬用のタイヤ、何インチ？")

    assert log == [(["冬用タイヤ"], 0), (["冬タイヤ"], 1)]
    assert found == [TIRE]


@pytest.mark.parametrize(
    "bad_plan",
    ["名前で検索します。", '["名前"]', LLMError("接続できません")],
)
def test_broken_search_plan_does_not_break_the_conversation(paths, bad_plan):
    history_path, memory_path = paths
    llm = FakeLLM(["すみません、分かりません。", "2です。"], [bad_plan, NO_MEMORY_PLAN])
    session = ChatSession(llm, history_path, memory_path)

    outputs = run_cli(session, ["私の名前は？", "1+1は？", "exit"])

    assert any(o.startswith("[警告] 記憶を検索できませんでした") for o in outputs)
    # 記憶なしで回答へ進み、会話は続行できる
    assert llm.prompts[0] == "[user]\n私の名前は？"
    assert "\nAI> すみません、分かりません。" in outputs
    assert "\nAI> 2です。" in outputs
    assert outputs[-1] == "終了します。"
    assert len(load_history(history_path)) == 4


def test_broken_retry_plan_is_a_warning_too(paths):
    history_path, memory_path = paths
    llm = FakeLLM(["覚えていません。"], [plan("血液型"), "壊れた出力"])
    session = ChatSession(llm, history_path, memory_path)

    outputs = run_cli(session, ["私の血液型は？", "exit"])

    assert "[検索結果] 0件" in outputs
    assert any(o.startswith("[警告] 記憶を検索できませんでした") for o in outputs)
    assert "\nAI> 覚えていません。" in outputs


# --- MVP3までの動作を保っている ---


def test_memory_extraction_still_works_and_new_memory_is_searchable(paths):
    history_path, memory_path = paths
    new_memory = memory("ユーザーの血液型はO型", "私の血液型はO型です")
    llm = FakeLLM(
        ["O型なんですね。", "O型です。"],
        [NO_MEMORY_PLAN, plan("血液型")],
        [json.dumps([{"text": new_memory["text"], "evidence": new_memory["evidence"]}], ensure_ascii=False)],
    )
    session = ChatSession(llm, history_path, memory_path)

    outputs = run_cli(session, ["私の血液型はO型です", "私の血液型は？", "exit"])

    # 抽出の入力はユーザー発言だけ（検索結果やassistantの発言は混ざらない）
    assert llm.extract_calls == ["私の血液型はO型です", "私の血液型は？"]
    assert "[記憶] ユーザーの血液型はO型" in outputs
    assert load_memories(memory_path) == MEMORIES + [new_memory]
    # 今回の起動中に保存した記憶も検索対象になる
    assert "- ユーザーの血液型はO型 (origin: user" in llm.prompts[1]


def test_conversation_in_current_process_is_kept_alongside_retrieved_memories(paths):
    history_path, memory_path = paths
    llm = FakeLLM(["了解しました", "テスト太郎さん、対象はAとBです"], [NO_MEMORY_PLAN, plan("名前")])
    session = ChatSession(llm, history_path, memory_path)

    run_cli(session, ["今回の試験対象はAとBです", "私の名前と試験対象は？", "exit"])

    assert llm.prompts[1] == (
        "[retrieved memories]\n"
        "- ユーザーの名前はテスト太郎 (origin: user, evidence: 私の名前はテスト太郎です)\n"
        "[/retrieved memories]\n\n"
        "[user]\n今回の試験対象はAとBです\n\n"
        "[assistant]\n了解しました\n\n"
        "[user]\n私の名前と試験対象は？"
    )
    # 検索で取得した記憶は会話履歴には入らない
    assert [m["content"] for m in session.messages] == [
        "今回の試験対象はAとBです",
        "了解しました",
        "私の名前と試験対象は？",
        "テスト太郎さん、対象はAとBです",
    ]


def test_saved_conversation_is_not_sent_to_the_answering_llm(paths):
    history_path, memory_path = paths
    old_turn = [
        {"role": "user", "content": "ヴェゼルに乗る予定です。色は白にしようと思います"},
        {"role": "assistant", "content": "白のヴェゼル、いいですね。"},
    ]
    append_history(history_path, old_turn)
    llm = FakeLLM(["テスト太郎さんです。"], [plan("名前")])
    session = ChatSession(llm, history_path, memory_path)

    run_cli(session, ["私の名前は？", "exit"])

    prompt = llm.prompts[0]
    assert old_turn[0]["content"] not in prompt
    assert old_turn[1]["content"] not in prompt
    assert "[assistant]" not in prompt
    # 原文は証拠として残り、今回のターンが追記される
    assert load_history(history_path)[:2] == old_turn
    assert len(load_history(history_path)) == 4
