"""MVP7: 作業記憶の変更履歴を、AIが必要と判断したときだけ検索して回答用のClaudeへ渡す。"""

import json

import pytest

from ai_context_memory import chat as chat_module
from ai_context_memory import llm as llm_module
from ai_context_memory.chat import ChatSession
from ai_context_memory.cli import run
from ai_context_memory.history import load_history
from ai_context_memory.llm import (
    LLMError,
    format_prompt,
    format_search_request,
    format_working_memory_history,
    format_working_memory_request,
)
from ai_context_memory.memory import append_memories
from ai_context_memory.search import (
    MAX_HISTORY_RESULTS,
    MAX_QUERIES,
    SearchPlan,
    SearchPlanError,
    parse_plan,
    parse_search_plan,
    search_working_memory_history,
)
from ai_context_memory.working_memory import (
    load_working_memory,
    load_working_memory_history,
    save_working_memory,
)

FIRST_TEXT = "今回の作業では商用環境、検証環境、開発環境を対象にします。対象環境すべてに対応できれば完了です。"
SWAP_TEXT = "検証環境の代わりにステージング環境を対象にします。"
DROP_TEXT = "ステージング環境はやっぱり対象外にします。"
BACK_TEXT = "やはりステージング環境も対象に戻します。"
FIRST_EVIDENCE = "今回の作業では商用環境、検証環境、開発環境を対象にします。"
DONE = "対象環境すべてに対応できれば完了"


def event(timestamp, operation, field, text, evidence, target=None):
    e = {"timestamp": timestamp, "operation": operation, "field": field}
    if target:
        e["target"] = target
    return {**e, "text": text, "evidence": evidence}


# MVP6 の実Claudeテストで残った履歴と同じ流れ
ADD_PROD = event("2026-10-07T18:47:57+09:00", "add", "scope", "商用環境", FIRST_EVIDENCE)
ADD_VERIFY = event("2026-10-07T18:47:57+09:00", "add", "scope", "検証環境", FIRST_EVIDENCE)
ADD_DEV = event("2026-10-07T18:47:57+09:00", "add", "scope", "開発環境", FIRST_EVIDENCE)
ADD_DONE = event(
    "2026-10-07T18:47:57+09:00", "add", "acceptance_criteria", DONE, "対象環境すべてに対応できれば完了です。"
)
REPLACE = event(
    "2026-10-07T18:48:19+09:00", "replace", "scope", "ステージング環境", SWAP_TEXT, target="検証環境"
)
REMOVE = event("2026-10-07T18:48:45+09:00", "remove", "scope", "ステージング環境", DROP_TEXT)
ADD_BACK = event("2026-10-07T18:49:06+09:00", "add", "scope", "ステージング環境", BACK_TEXT)
EVENTS = [ADD_PROD, ADD_VERIFY, ADD_DEV, ADD_DONE, REPLACE, REMOVE, ADD_BACK]

CURRENT = {
    "mission": [],
    "scope": [
        {"text": "商用環境", "evidence": FIRST_EVIDENCE},
        {"text": "開発環境", "evidence": FIRST_EVIDENCE},
        {"text": "ステージング環境", "evidence": BACK_TEXT},
    ],
    "acceptance_criteria": [{"text": DONE, "evidence": "対象環境すべてに対応できれば完了です。"}],
}
CURRENT_BLOCK = (
    "[working memory]\n"
    "Scope:\n- 商用環境\n- 開発環境\n- ステージング環境\n\n"
    f"Acceptance Criteria:\n- {DONE}\n"
    "[/working memory]"
)

FRUIT = {"text": "ユーザーの好きな果物は梨", "origin": "user", "evidence": "好きな果物は梨です"}
SCOPE_MEMORY = {
    "text": "今回の作業の対象は商用環境、検証環境、開発環境",
    "origin": "user",
    "evidence": FIRST_EVIDENCE,
}

REPLACE_ENTRY = (
    "- 2026-10-07T18:48:19+09:00\n"
    "  operation: replace\n"
    "  field: scope\n"
    "  target: 検証環境\n"
    "  text: ステージング環境\n"
    f"  evidence: {SWAP_TEXT}"
)
REMOVE_ENTRY = (
    "- 2026-10-07T18:48:45+09:00\n"
    "  operation: remove\n"
    "  field: scope\n"
    "  text: ステージング環境\n"
    f"  evidence: {DROP_TEXT}"
)
ADD_BACK_ENTRY = (
    "- 2026-10-07T18:49:06+09:00\n"
    "  operation: add\n"
    "  field: scope\n"
    "  text: ステージング環境\n"
    f"  evidence: {BACK_TEXT}"
)
STAGING_BLOCK = (
    "[working memory history]\n"
    f"{REPLACE_ENTRY}\n\n{REMOVE_ENTRY}\n\n{ADD_BACK_ENTRY}\n"
    "[/working memory history]"
)


def plan(queries=(), history_queries=()):
    """検索プランナーの出力（JSONオブジェクトの文字列）を作る。"""
    return json.dumps(
        {
            "needs_memory": bool(queries),
            "queries": list(queries),
            "needs_working_memory_history": bool(history_queries),
            "history_queries": list(history_queries),
        },
        ensure_ascii=False,
    )


NOTHING_NEEDED = plan()


class FakeLLM:
    """通常回答・検索プラン作成・作業記憶の抽出を別々に記録し、用意された応答を順に返す。"""

    def __init__(self, replies, plans=(), working=()):
        self.replies = list(replies)
        self.plans = list(plans)
        self.working = list(working)  # 尽きたら「候補なし」を返す
        self.prompts = []  # 通常回答でclaudeへ渡されるプロンプト
        self.plan_inputs = []  # 検索プラン作成でclaudeへ渡される入力

    def complete(self, messages, memories=(), working_memory=None, history_events=(), *context):
        self.prompts.append(format_prompt(messages, memories, working_memory, history_events))
        return self.replies.pop(0)

    def plan_search(self, user_text, previous_queries=(), working_memory=None):
        self.plan_inputs.append(format_search_request(user_text, previous_queries, working_memory))
        result = self.plans.pop(0)
        if isinstance(result, Exception):
            raise result
        return result

    def extract_memories(self, user_text):
        return "[]"

    def extract_working_memory(self, user_text, working_memory):
        format_working_memory_request(user_text, working_memory)
        return self.working.pop(0) if self.working else "[]"


def write_history(path, events):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "".join(json.dumps(e, ensure_ascii=False) + "\n" for e in events), encoding="utf-8"
    )


@pytest.fixture
def paths(tmp_path):
    """再起動後の状態: 現在の作業記憶、その変更履歴、長期記憶が保存済み。"""
    data = tmp_path / "data"
    paths = (
        data / "conversation.jsonl",
        data / "memories.jsonl",
        data / "working_memory.json",
        data / "working_memory_history.jsonl",
    )
    append_memories(paths[1], [FRUIT, SCOPE_MEMORY])
    save_working_memory(paths[2], CURRENT)
    write_history(paths[3], EVENTS)
    return paths


@pytest.fixture
def history_searches(monkeypatch):
    """ChatSession からの履歴検索の呼び出しを記録する（検索自体は本物を実行する）。"""
    calls = []

    def spy(events, queries, *args, **kwargs):
        calls.append(list(queries))
        return search_working_memory_history(events, queries, *args, **kwargs)

    monkeypatch.setattr(chat_module, "search_working_memory_history", spy)
    return calls


def run_cli(session, inputs):
    it = iter(inputs)
    outputs = []
    run(session, input_fn=lambda prompt: next(it), output_fn=outputs.append)
    return outputs


def history_block(prompt):
    assert "[working memory history]" in prompt
    return prompt.split("[working memory history]\n")[1].split("\n[/working memory history]")[0]


# --- 検索プランの解析 ---


def test_plan_with_history_keys_is_parsed():
    """1. needs_working_memory_history / history_queries を解析できる。"""
    raw = (
        '{"needs_memory": false, "queries": [], "needs_working_memory_history": true, '
        '"history_queries": ["ステージング環境", "検証環境 ステージング環境"]}'
    )

    assert parse_plan(raw) == SearchPlan([], ["ステージング環境", "検証環境 ステージング環境"])
    # 長期記憶だけを見る既存の関数は、そのまま使える
    assert parse_search_plan(raw) == []


def test_plan_can_ask_for_both_sources():
    assert parse_plan(plan(["果物"], ["scope"])) == SearchPlan(["果物"], ["scope"])


def test_plan_without_history_keys_means_history_is_not_needed():
    # MVP4 形式の出力
    assert parse_plan('{"needs_memory": true, "queries": ["名前"]}') == SearchPlan(["名前"], [])


def test_history_queries_are_ignored_unless_history_is_explicitly_needed():
    assert parse_plan(
        '{"needs_working_memory_history": false, "history_queries": ["scope"]}'
    ).history_queries == []
    assert parse_plan('{"history_queries": ["scope"]}').history_queries == []


def test_history_queries_are_cleaned_deduplicated_and_capped():
    queries = parse_plan(
        plan(history_queries=["scope", "  SCOPE ", "", "検証環境　 ステージング環境", "remove", "add"])
    ).history_queries

    assert queries == ["scope", "検証環境 ステージング環境", "remove"]
    assert len(queries) == MAX_QUERIES


@pytest.mark.parametrize(
    "raw",
    [
        '{"needs_working_memory_history": true, "history_queries": "scope"}',
        '{"needs_working_memory_history": true, "history_queries": ["scope", 1]}',
    ],
)
def test_invalid_history_queries_are_rejected(raw):
    with pytest.raises(SearchPlanError):
        parse_plan(raw)


# --- 履歴の検索関数 ---


def test_history_search_matches_operation():
    """3."""
    assert search_working_memory_history(EVENTS, ["replace"]) == [REPLACE]
    assert search_working_memory_history(EVENTS, ["remove"]) == [REMOVE]


def test_history_search_matches_field():
    """4."""
    assert search_working_memory_history(EVENTS, ["acceptance_criteria"]) == [ADD_DONE]
    assert search_working_memory_history(EVENTS, ["scope"]) == [
        ADD_PROD,
        ADD_VERIFY,
        ADD_DEV,
        REPLACE,
        REMOVE,
        ADD_BACK,
    ]


def test_history_search_matches_text():
    """5. 「本番DR環境」は text にだけ現れる。"""
    events = [event("2026-10-07T10:00:00+09:00", "add", "scope", "本番DR環境", "DRも対象です")]

    assert search_working_memory_history(events, ["本番DR環境"]) == events


def test_history_search_matches_target():
    """6. 「検証環境」は target にだけ現れる。"""
    events = [
        event("2026-10-07T10:00:00+09:00", "replace", "scope", "ステージング環境", "入れ替えます", target="検証環境")
    ]

    assert search_working_memory_history(events, ["検証環境"]) == events


def test_history_search_matches_evidence():
    """7. 「やっぱり」は evidence にだけ現れる。"""
    assert search_working_memory_history(EVENTS, ["やっぱり"]) == [REMOVE]


def test_history_search_results_keep_timestamp_and_all_fields():
    """8."""
    [found] = search_working_memory_history(EVENTS, ["replace"])

    assert found["timestamp"] == "2026-10-07T18:48:19+09:00"
    assert found == REPLACE


def test_history_search_does_not_exceed_limit_and_prefers_newer_events():
    """9. 上限を超えない。同点なら新しいイベントを残し、古い順に並べて返す。"""
    events = [
        event(f"2026-10-07T10:{i:02d}:00+09:00", "add", "scope", f"環境{i}", f"環境{i}を追加")
        for i in range(MAX_HISTORY_RESULTS + 5)
    ]

    found = search_working_memory_history(events, ["scope"])

    assert len(found) == MAX_HISTORY_RESULTS
    assert found == events[5:]
    assert search_working_memory_history(events, ["scope"], limit=2) == events[-2:]


def test_history_search_prefers_higher_score_when_over_limit_and_returns_time_order():
    old_but_relevant = event("2026-10-07T09:00:00+09:00", "remove", "scope", "検証環境", "検証環境を外す")
    newer = [
        event(f"2026-10-07T10:0{i}:00+09:00", "add", "scope", f"環境{i}", f"環境{i}を追加")
        for i in range(3)
    ]

    found = search_working_memory_history([old_but_relevant] + newer, ["scope", "remove"], limit=2)

    # 2つの検索語にヒットした古いイベントが残り、結果は時系列に戻る
    assert found == [old_but_relevant, newer[-1]]


def test_history_search_is_and_within_a_query_and_or_across_queries():
    assert search_working_memory_history(EVENTS, ["検証環境 ステージング環境"]) == [REPLACE]
    assert search_working_memory_history(EVENTS, ["検証環境 remove"]) == []
    assert search_working_memory_history(EVENTS, ["replace", "remove"]) == [REPLACE, REMOVE]


def test_history_search_results_are_in_time_order():
    assert search_working_memory_history(EVENTS, ["ステージング環境"]) == [REPLACE, REMOVE, ADD_BACK]


def test_history_search_ignores_case_and_character_width():
    events = [event("2026-10-07T10:00:00+09:00", "add", "scope", "ＡＷＳ環境", "AWSも対象")]

    assert search_working_memory_history(events, ["aws環境"]) == events
    assert search_working_memory_history(EVENTS, ["REPLACE"]) == [REPLACE]


def test_history_search_does_not_search_timestamp():
    assert search_working_memory_history(EVENTS, ["2026-10-07"]) == []


def test_history_search_with_no_events_or_no_queries_returns_nothing():
    assert search_working_memory_history([], ["scope"]) == []
    assert search_working_memory_history(EVENTS, []) == []
    assert search_working_memory_history(EVENTS, ["", "  "]) == []


def test_history_search_tolerates_events_with_missing_keys():
    events = [{"timestamp": "2026-10-07T10:00:00+09:00", "operation": "add"}]

    assert search_working_memory_history(events, ["add"]) == events
    assert search_working_memory_history(events, ["scope"]) == []


# --- 履歴ブロックの形式 ---


def test_history_block_format():
    assert format_working_memory_history([REPLACE, REMOVE, ADD_BACK]) == STAGING_BLOCK
    assert format_working_memory_history([]) == ""


def test_prompt_order_is_working_memory_history_memories_conversation():
    messages = [
        {"role": "user", "content": "こんにちは"},
        {"role": "assistant", "content": "こんにちは。"},
        {"role": "user", "content": "対象環境はどう変わってきた？"},
    ]

    prompt = format_prompt(messages, [FRUIT], CURRENT, [REPLACE, REMOVE, ADD_BACK])

    assert prompt == (
        f"{CURRENT_BLOCK}\n\n"
        f"{STAGING_BLOCK}\n\n"
        "[retrieved memories]\n"
        "- ユーザーの好きな果物は梨 (origin: user, evidence: 好きな果物は梨です)\n"
        "[/retrieved memories]\n\n"
        "[user]\nこんにちは\n\n"
        "[assistant]\nこんにちは。\n\n"
        "[user]\n対象環境はどう変わってきた？"
    )


# --- 検索プランナーへ渡す情報 ---


def test_planner_receives_current_working_memory_but_not_history(paths):
    """2. 検索プランナーへ現在の作業記憶を渡す。履歴そのものは渡さない。"""
    llm = FakeLLM(["商用環境、開発環境、ステージング環境です。"], [NOTHING_NEEDED])
    session = ChatSession(llm, *paths)

    run_cli(session, ["この作業の対象環境は？", "exit"])

    assert llm.plan_inputs == [f"{CURRENT_BLOCK}\n\n[question]\nこの作業の対象環境は？"]
    for word in ("検証環境", "replace", "remove", "2026-10-07", "梨"):
        assert word not in llm.plan_inputs[0]


def test_planner_prompt_explains_both_sources():
    prompt = llm_module.SEARCH_PLAN_SYSTEM_PROMPT

    for word in (
        "needs_memory",
        "queries",
        "needs_working_memory_history",
        "history_queries",
        "[working memory]",
        "operation",
        "evidence",
    ):
        assert word in prompt
    assert "[working memory] だけで答えられるので、どの検索も不要です" in prompt


# --- 質問ごとの使い分け ---


def test_current_state_question_does_not_search_history(paths, history_searches):
    """12. 成功条件1: 現在状態の質問では履歴を検索しない。"""
    llm = FakeLLM(["商用環境、開発環境、ステージング環境です。"], [NOTHING_NEEDED])
    session = ChatSession(llm, *paths)

    outputs = run_cli(session, ["この作業の対象環境は？", "exit"])

    assert history_searches == []
    assert llm.prompts == [f"{CURRENT_BLOCK}\n\n[user]\nこの作業の対象環境は？"]
    assert not any("検索" in o for o in outputs)
    assert "\nAI> 商用環境、開発環境、ステージング環境です。" in outputs


def test_past_state_question_searches_history_and_sends_only_hits(paths, history_searches):
    """10・11・13. 成功条件2: 過去状態の質問では履歴を検索し、ヒットしたイベントだけを渡す。"""
    queries = ["ステージング環境", "検証環境 ステージング環境"]
    llm = FakeLLM(["商用環境と開発環境でした。"], [plan(history_queries=queries)])
    session = ChatSession(llm, *paths)

    outputs = run_cli(session, ["ステージング環境を戻す直前の対象環境は何でしたか？", "exit"])

    assert history_searches == [queries]
    assert llm.prompts == [
        f"{CURRENT_BLOCK}\n\n{STAGING_BLOCK}\n\n"
        "[user]\nステージング環境を戻す直前の対象環境は何でしたか？"
    ]
    # ヒットしなかったイベント（履歴の全文）は渡らない
    block = history_block(llm.prompts[0])
    assert block.count("operation:") == 3
    assert FIRST_EVIDENCE not in llm.prompts[0]
    assert "acceptance_criteria" not in llm.prompts[0]
    assert outputs[1:3] == [
        "[履歴検索] ステージング環境 / 検証環境 ステージング環境",
        "[履歴検索結果] 3件",
    ]
    # イベントの中身はCLIへ表示しない
    assert not any("operation" in o or DROP_TEXT in o for o in outputs)
    # 検索で取得した履歴は会話履歴には入らない
    assert [m["content"] for m in session.messages] == [
        "ステージング環境を戻す直前の対象環境は何でしたか？",
        "商用環境と開発環境でした。",
    ]
    assert len(load_history(paths[0])) == 2


def test_when_question_passes_original_timestamp_to_answering_llm(paths, history_searches):
    """14. 成功条件3: 変更時刻の質問で、保存されている timestamp をそのまま渡す。"""
    llm = FakeLLM(["10月7日の18時48分です。"], [plan(history_queries=["replace"])])
    session = ChatSession(llm, *paths)

    outputs = run_cli(session, ["検証環境をステージング環境に変えたのはいつ？", "exit"])

    assert history_block(llm.prompts[0]) == REPLACE_ENTRY
    assert "2026-10-07T18:48:19+09:00" in llm.prompts[0]
    assert outputs[1:3] == ["[履歴検索] replace", "[履歴検索結果] 1件"]


def test_flow_question_gets_events_in_time_order(paths):
    """成功条件4: 変更の流れの質問では、該当するイベントを古い順に渡す。"""
    llm = FakeLLM(["検証→ステージング→対象外→再追加です。"], [plan(history_queries=["scope"])])
    session = ChatSession(llm, *paths)

    run_cli(session, ["対象環境はどう変わってきた？", "exit"])

    block = history_block(llm.prompts[0])
    assert [line.strip() for line in block.split("\n") if "operation:" in line] == [
        "operation: add",
        "operation: add",
        "operation: add",
        "operation: replace",
        "operation: remove",
        "operation: add",
    ]
    timestamps = [line[2:] for line in block.split("\n") if line.startswith("- ")]
    assert timestamps == sorted(timestamps)
    assert DONE not in block


def test_long_term_only_question_does_not_search_history(paths, history_searches):
    """15. 成功条件5: 長期記憶だけ必要な質問では履歴を検索しない。"""
    llm = FakeLLM(["梨です。"], [plan(["果物"])])
    session = ChatSession(llm, *paths)

    outputs = run_cli(session, ["私の好きな果物は？", "exit"])

    assert history_searches == []
    assert "[working memory history]" not in llm.prompts[0]
    assert "- ユーザーの好きな果物は梨 (origin: user" in llm.prompts[0]
    assert outputs[1:3] == ["[検索] 果物", "[検索結果] 1件"]
    assert not any("履歴検索" in o for o in outputs)


def test_general_question_searches_neither(paths, history_searches):
    """16. 成功条件6: 一般質問では長期記憶も履歴も検索しない。"""
    llm = FakeLLM(["2です。"], [NOTHING_NEEDED])
    session = ChatSession(llm, *paths)

    outputs = run_cli(session, ["1+1は？", "exit"])

    assert history_searches == []
    assert len(llm.plan_inputs) == 1
    assert llm.prompts == [f"{CURRENT_BLOCK}\n\n[user]\n1+1は？"]
    assert not any("検索" in o for o in outputs)


def test_both_sources_can_be_searched_with_one_plan(paths, history_searches):
    llm = FakeLLM(["最初は検証環境でした。"], [plan(["検証環境"], ["検証環境"])])
    session = ChatSession(llm, *paths)

    outputs = run_cli(session, ["検証環境は最初から対象でしたか？", "exit"])

    # プランナーの呼び出しは1回だけ。最初の add 3件は evidence（同じ発言）に「検証環境」を含むのでヒットする
    assert len(llm.plan_inputs) == 1
    prompt = llm.prompts[0]
    assert prompt.index("[/working memory]") < prompt.index("[working memory history]")
    assert prompt.index("[/working memory history]") < prompt.index("[retrieved memories]")
    assert prompt.index("[/retrieved memories]") < prompt.index("[user]")
    assert outputs[1:5] == ["[履歴検索] 検証環境", "[履歴検索結果] 4件", "[検索] 検証環境", "[検索結果] 1件"]


# --- 履歴が無い・見つからない・読めない ---


def test_conversation_continues_without_history_file(paths, history_searches):
    """17. 履歴ファイルが無くても会話を続ける（MVP6 導入前のデータ）。"""
    paths[3].unlink()
    llm = FakeLLM(["履歴からは確認できません。"], [plan(history_queries=["ステージング環境"])])
    session = ChatSession(llm, *paths)

    outputs = run_cli(session, ["ステージング環境を戻す直前の対象環境は何でしたか？", "exit"])

    assert outputs[1:3] == ["[履歴検索] ステージング環境", "[履歴検索結果] 0件"]
    assert "[working memory history]" not in llm.prompts[0]
    assert llm.prompts[0].startswith(CURRENT_BLOCK)
    assert "\nAI> 履歴からは確認できません。" in outputs
    assert outputs[-1] == "終了します。"
    assert not paths[3].exists()  # 検索のためにファイルを作らない


def test_conversation_continues_when_history_search_finds_nothing(paths):
    """18. 履歴検索が0件でも会話を続ける。再検索はしない。"""
    llm = FakeLLM(["履歴からは確認できません。", "2です。"], [plan(history_queries=["本番DR環境"]), NOTHING_NEEDED])
    session = ChatSession(llm, *paths)

    outputs = run_cli(session, ["本番DR環境はいつ外しましたか？", "1+1は？", "exit"])

    assert outputs[1:3] == ["[履歴検索] 本番DR環境", "[履歴検索結果] 0件"]
    assert "[working memory history]" not in llm.prompts[0]
    assert len(llm.plan_inputs) == 2  # 1ターンに1回ずつ
    assert "\nAI> 2です。" in outputs


def test_broken_history_file_is_a_warning_and_conversation_continues(paths):
    paths[3].write_text("壊れた行\n", encoding="utf-8")
    llm = FakeLLM(["履歴からは確認できません。"], [plan(history_queries=["scope"])])
    session = ChatSession(llm, *paths)

    outputs = run_cli(session, ["対象環境はどう変わってきた？", "exit"])

    assert any(o.startswith("[警告] 作業記憶の変更履歴がJSONとして読めません") for o in outputs)
    assert "[working memory history]" not in llm.prompts[0]
    assert "\nAI> 履歴からは確認できません。" in outputs


@pytest.mark.parametrize("bad_plan", ["履歴を検索します。", LLMError("接続できません")])
def test_broken_plan_searches_nothing_and_conversation_continues(paths, history_searches, bad_plan):
    llm = FakeLLM(["分かりません。"], [bad_plan])
    session = ChatSession(llm, *paths)

    outputs = run_cli(session, ["対象環境はどう変わってきた？", "exit"])

    assert history_searches == []
    assert any(o.startswith("[警告] 記憶を検索できませんでした") for o in outputs)
    assert llm.prompts == [f"{CURRENT_BLOCK}\n\n[user]\n対象環境はどう変わってきた？"]


def test_planner_is_not_called_when_there_is_nothing_to_search(tmp_path):
    llm = FakeLLM(["はじめまして"])
    session = ChatSession(llm, *(tmp_path / name for name in ("c.jsonl", "m.jsonl", "w.json", "h.jsonl")))

    run_cli(session, ["対象環境はどう変わってきた？", "exit"])

    assert llm.plan_inputs == []


def test_history_is_searchable_without_any_long_term_memory(paths):
    paths[1].unlink()
    # 長期記憶が無ければ、長期記憶の検索語が出ても検索も再検索もしない
    llm = FakeLLM(["18時48分です。"], [plan(["検証環境"], ["replace"])])
    session = ChatSession(llm, *paths)

    outputs = run_cli(session, ["検証環境をステージング環境に変えたのはいつ？", "exit"])

    assert len(llm.plan_inputs) == 1
    assert outputs[1:4] == ["[履歴検索] replace", "[履歴検索結果] 1件", "\nAI> 18時48分です。"]
    assert history_block(llm.prompts[0]) == REPLACE_ENTRY


# --- 回答用Claudeへの指示 ---


def test_system_prompt_says_working_memory_is_the_current_state():
    """19. 作業記憶が現在状態として最優先である指示が入る。"""
    system = llm_module.SYSTEM_PROMPT

    assert "これは現在有効な作業状態で" in system
    assert "現在の状態については、常に [working memory] を優先してください。" in system
    assert "[working memory] を現在の状態として優先してください。" in system  # 長期記憶との矛盾（MVP6）


def test_system_prompt_says_history_is_past_events_not_current_values():
    """20. 履歴は過去イベントであり現在値ではない指示が入る。"""
    system = llm_module.SYSTEM_PROMPT

    assert "[working memory history]" in system
    assert "これらは過去に起きた状態変更の記録であって、現在の状態ではありません。" in system
    assert "イベントにある値を、現在の対象や条件として扱わないでください。" in system
    assert "このブロックのイベントを根拠にしてください。" in system
    assert "推定としてではなく、履歴を根拠として答えてください。" in system
    assert "ブロックに無い出来事を補って述べないでください。" in system
    assert "履歴からは確認できないと伝えてください。" in system


# --- MVP6・MVP4 の動作を保っている ---


def test_mvp6_operations_still_work_and_new_events_are_searchable(tmp_path):
    """21. add / replace / remove が引き続き動き、今回の起動中に残した履歴も検索できる。"""
    data = tmp_path / "data"
    paths = (
        data / "conversation.jsonl",
        data / "memories.jsonl",
        data / "working_memory.json",
        data / "working_memory_history.jsonl",
    )

    def candidates(*items):
        return json.dumps(list(items), ensure_ascii=False)

    def add(text, evidence):
        return {"operation": "add", "field": "scope", "text": text, "evidence": evidence}

    llm = FakeLLM(
        ["承知しました。"] * 4 + ["商用環境と開発環境でした。"],
        # 1ターン目は検索できるものが無いので、プランナーは呼ばれない
        [NOTHING_NEEDED, NOTHING_NEEDED, NOTHING_NEEDED, plan(history_queries=["ステージング環境"])],
        [
            candidates(
                add("商用環境", FIRST_EVIDENCE), add("検証環境", FIRST_EVIDENCE), add("開発環境", FIRST_EVIDENCE)
            ),
            candidates(
                {
                    "operation": "replace",
                    "field": "scope",
                    "target": "検証環境",
                    "text": "ステージング環境",
                    "evidence": SWAP_TEXT,
                }
            ),
            candidates(
                {"operation": "remove", "field": "scope", "target": "ステージング環境", "evidence": DROP_TEXT}
            ),
            candidates(add("ステージング環境", BACK_TEXT)),
        ],
    )
    session = ChatSession(llm, *paths)

    outputs = run_cli(
        session,
        [FIRST_TEXT, SWAP_TEXT, DROP_TEXT, BACK_TEXT, "ステージング環境を戻す直前の対象環境は何でしたか？", "exit"],
    )

    assert [o for o in outputs if o.startswith("[作業記憶]")] == [
        "[作業記憶] scope: 商用環境 / 検証環境 / 開発環境",
        "[作業記憶] scope: 検証環境 → ステージング環境",
        "[作業記憶] scope: ステージング環境 → 削除",
        "[作業記憶] scope: ステージング環境",
    ]
    assert [i["text"] for i in load_working_memory(paths[2])["scope"]] == [
        "商用環境",
        "開発環境",
        "ステージング環境",
    ]
    history = load_working_memory_history(paths[3])
    assert [e["operation"] for e in history] == ["add", "add", "add", "replace", "remove", "add"]
    # 履歴を検索しなかったターンには、履歴ブロックは無い
    assert all("[working memory history]" not in p for p in llm.prompts[:4])
    assert "[履歴検索結果] 3件" in outputs
    assert [line.strip() for line in history_block(llm.prompts[4]).split("\n") if "operation:" in line] == [
        "operation: replace",
        "operation: remove",
        "operation: add",
    ]
    # 検索は履歴ファイルを書き換えない
    assert load_working_memory_history(paths[3]) == history


def test_mvp4_retry_still_works_and_history_is_searched_only_once(paths, history_searches):
    """22. 長期記憶の0件時の再検索は維持する。履歴の検索は1回目のプランの分だけ。"""
    llm = FakeLLM(
        ["梨です。"],
        [plan(["フルーツ"], ["replace"]), plan(["果物"], ["remove"])],
    )
    session = ChatSession(llm, *paths)

    outputs = run_cli(session, ["好きなフルーツと、環境を入れ替えた時刻は？", "exit"])

    assert history_searches == [["replace"]]
    assert outputs[1:7] == [
        "[履歴検索] replace",
        "[履歴検索結果] 1件",
        "[検索] フルーツ",
        "[検索結果] 0件",
        "[再検索] 果物",
        "[検索結果] 1件",
    ]
    # 2回目のプランナーにも現在の作業記憶と、0件だった長期記憶の検索語を渡す
    assert llm.plan_inputs[1] == (
        f"{CURRENT_BLOCK}\n\n[question]\n好きなフルーツと、環境を入れ替えた時刻は？\n\n"
        "[previous queries: 0 hits]\n- フルーツ"
    )
    assert history_block(llm.prompts[0]) == REPLACE_ENTRY
    assert "ユーザーの好きな果物は梨" in llm.prompts[0]


def test_recall_still_returns_long_term_results_only(paths):
    llm = FakeLLM([], [plan(["果物"], ["replace"])])
    session = ChatSession(llm, *paths)

    assert session.recall("私の好きな果物は？") == ([FRUIT], [(["果物"], 1)], [])
