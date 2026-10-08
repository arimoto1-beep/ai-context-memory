import json
from datetime import datetime, timedelta, timezone

import pytest

from ai_context_memory import llm as llm_module
from ai_context_memory.chat import ChatSession
from ai_context_memory.cli import run
from ai_context_memory.history import append_history, load_history
from ai_context_memory.llm import (
    LLMError,
    format_prompt,
    format_working_memory_request,
)
from ai_context_memory.memory import append_memories, load_memories
from ai_context_memory.working_memory import (
    WorkingMemoryError,
    append_working_memory_history,
    apply_candidates,
    count_items,
    empty_working_memory,
    load_working_memory,
    load_working_memory_history,
    normalize_candidate,
    parse_candidates,
    save_working_memory,
)

TASK_TEXT = (
    "商用環境、検証環境、開発環境の3環境へ展開できる資産を作りたいです。"
    "3環境すべて揃えば完了です。"
)


def item(text, evidence=None):
    return {"text": text, "evidence": evidence or text}


def candidate(field, text, evidence=None, **extra):
    return {"field": field, "text": text, "evidence": evidence or text, **extra}


def extraction(*candidates):
    """抽出AIの出力（JSON配列の文字列）を作る。"""
    return json.dumps(list(candidates), ensure_ascii=False)


TASK_EXTRACTION = extraction(
    candidate("mission", "3環境へ展開できる資産を作る", "3環境へ展開できる資産を作りたいです"),
    candidate("scope", "商用環境"),
    candidate("scope", "検証環境"),
    candidate("scope", "開発環境"),
    candidate("acceptance_criteria", "3環境すべて揃えば完了", "3環境すべて揃えば完了です"),
)
TASK_WORKING_MEMORY = {
    "mission": [item("3環境へ展開できる資産を作る", "3環境へ展開できる資産を作りたいです")],
    "scope": [item("商用環境"), item("検証環境"), item("開発環境")],
    "acceptance_criteria": [item("3環境すべて揃えば完了", "3環境すべて揃えば完了です")],
}
TASK_BLOCK = (
    "[working memory]\n"
    "Mission:\n- 3環境へ展開できる資産を作る\n\n"
    "Scope:\n- 商用環境\n- 検証環境\n- 開発環境\n\n"
    "Acceptance Criteria:\n- 3環境すべて揃えば完了\n"
    "[/working memory]"
)

NAME = {"text": "ユーザーの名前はテスト太郎", "origin": "user", "evidence": "私の名前はテスト太郎です"}
FRUIT = {"text": "ユーザーの好きな果物は梨", "origin": "user", "evidence": "好きな果物は梨です"}
NAME_LINE = "- ユーザーの名前はテスト太郎 (origin: user, evidence: 私の名前はテスト太郎です)"


def plan(*queries):
    return json.dumps({"needs_memory": True, "queries": list(queries)}, ensure_ascii=False)


class FakeLLM:
    """通常回答・検索プラン作成・長期記憶の抽出・作業記憶の抽出を別々に記録し、用意された応答を順に返す。"""

    def __init__(self, replies, working=(), plans=(), extractions=()):
        self.replies = list(replies)
        self.working = list(working)  # 尽きたら「候補なし」を返す
        self.plans = list(plans)  # 尽きたら「記憶は不要」を返す
        self.extractions = list(extractions)  # 尽きたら「記憶なし」を返す
        self.prompts = []  # 通常回答でclaudeへ渡されるプロンプト
        self.working_calls = []  # 作業記憶の抽出の入力
        self.plan_calls = []

    def complete(self, messages, memories=(), working_memory=None, history_events=()):
        self.prompts.append(format_prompt(messages, memories, working_memory))
        return self.replies.pop(0)

    def plan_search(self, user_text, previous_queries=(), working_memory=None):
        self.plan_calls.append((user_text, list(previous_queries)))
        return self.plans.pop(0) if self.plans else '{"needs_memory": false, "queries": []}'

    def extract_memories(self, user_text):
        return self.extractions.pop(0) if self.extractions else "[]"

    def extract_working_memory(self, user_text, working_memory):
        self.working_calls.append(format_working_memory_request(user_text, working_memory))
        result = self.working.pop(0) if self.working else "[]"
        if isinstance(result, Exception):
            raise result
        return result


@pytest.fixture
def paths(tmp_path):
    data = tmp_path / "data"
    return (
        data / "conversation.jsonl",
        data / "memories.jsonl",
        data / "working_memory.json",
        data / "working_memory_history.jsonl",
    )


def run_cli(session, inputs):
    it = iter(inputs)
    outputs = []
    run(session, input_fn=lambda prompt: next(it), output_fn=outputs.append)
    return outputs


# --- 作業記憶ファイル ---


def test_missing_or_empty_file_loads_as_empty_working_memory(paths):
    path = paths[2]
    assert load_working_memory(path) == empty_working_memory()
    assert not path.exists()

    path.parent.mkdir()
    path.write_text("", encoding="utf-8")
    assert load_working_memory(path) == empty_working_memory()


def test_saved_working_memory_can_be_loaded_back(paths):
    path = paths[2]

    save_working_memory(path, TASK_WORKING_MEMORY)

    assert load_working_memory(path) == TASK_WORKING_MEMORY
    assert count_items(load_working_memory(path)) == 5
    # 人が読めるJSONで、3つのフィールドだけを持つ
    saved = json.loads(path.read_text(encoding="utf-8"))
    assert list(saved) == ["mission", "scope", "acceptance_criteria"]
    assert "商用環境" in path.read_text(encoding="utf-8")


def test_file_with_only_some_fields_is_accepted(paths):
    path = paths[2]
    path.parent.mkdir()
    path.write_text('{"scope": [{"text": "商用環境", "evidence": "商用環境"}]}', encoding="utf-8")

    assert load_working_memory(path) == {
        "mission": [],
        "scope": [item("商用環境")],
        "acceptance_criteria": [],
    }


@pytest.mark.parametrize(
    "content",
    [
        "壊れた内容",
        '["商用環境"]',
        '{"progress": []}',
        '{"scope": "商用環境"}',
        '{"scope": ["商用環境"]}',
        '{"scope": [{"text": "商用環境"}]}',
    ],
)
def test_broken_working_memory_file_is_rejected(paths, content):
    path = paths[2]
    path.parent.mkdir()
    path.write_text(content, encoding="utf-8")

    with pytest.raises(WorkingMemoryError, match="作業記憶ファイル"):
        load_working_memory(path)
    # 壊れたファイルには手を加えない
    assert path.read_text(encoding="utf-8") == content


def test_save_failure_becomes_working_memory_error(tmp_path):
    # 親が通常ファイルなのでディレクトリを作れず、保存に失敗する
    blocker = tmp_path / "data"
    blocker.write_text("", encoding="utf-8")

    with pytest.raises(WorkingMemoryError, match="保存できませんでした"):
        save_working_memory(blocker / "working_memory.json", TASK_WORKING_MEMORY)


# --- 候補の検証と反映 ---


@pytest.mark.parametrize("field", ["mission", "scope", "acceptance_criteria"])
def test_candidate_is_saved_to_its_field(paths, field):
    working_path = paths[2]
    text = "検証環境で全試験項目が成功する"
    llm = FakeLLM(["了解しました"], [extraction(candidate(field, text))])
    session = ChatSession(llm, *paths)

    session.send(f"今回は{text}ところまでやります")
    changes, warnings = session.update_working_memory(f"今回は{text}ところまでやります")

    assert changes == [{"operation": "add", "field": field, "text": text, "evidence": text}]
    assert warnings == []
    expected = empty_working_memory()
    expected[field] = [item(text)]
    assert load_working_memory(working_path) == expected


def test_task_statement_fills_all_three_fields(paths):
    llm = FakeLLM(["承知しました"], [TASK_EXTRACTION])
    session = ChatSession(llm, *paths)

    outputs = run_cli(session, [TASK_TEXT, "exit"])

    assert load_working_memory(paths[2]) == TASK_WORKING_MEMORY
    assert outputs[2:5] == [
        "[作業記憶] mission: 3環境へ展開できる資産を作る",
        "[作業記憶] scope: 商用環境 / 検証環境 / 開発環境",
        "[作業記憶] acceptance_criteria: 3環境すべて揃えば完了",
    ]
    # 抽出の入力は現在の作業記憶とユーザー発言だけで、assistantの発言は渡らない
    assert llm.working_calls == [
        f"[working memory]\n(empty)\n[/working memory]\n\n[utterance]\n{TASK_TEXT}"
    ]


def test_out_of_scope_utterance_saves_nothing(paths):
    llm = FakeLLM(["お疲れさまです"], ["[]"])
    session = ChatSession(llm, *paths)

    outputs = run_cli(session, ["今日は眠い", "exit"])

    assert not paths[2].exists()
    assert not any("作業記憶" in o for o in outputs)
    assert session.working_memory == empty_working_memory()


def test_candidate_whose_evidence_is_not_in_user_text_is_rejected(paths):
    llm = FakeLLM(
        ["承知しました"],
        [
            extraction(
                candidate("scope", "商用環境"),
                candidate("scope", "災害対策環境", "災害対策環境も対象です"),
            )
        ],
    )
    session = ChatSession(llm, *paths)

    outputs = run_cli(session, ["商用環境へ展開します", "exit"])

    assert load_working_memory(paths[2])["scope"] == [item("商用環境")]
    warnings = [o for o in outputs if o.startswith("[警告]")]
    assert len(warnings) == 1
    assert "evidence" in warnings[0] and "災害対策環境も対象です" in warnings[0]


@pytest.mark.parametrize(
    "bad",
    [
        "ただの文字列",
        candidate("progress", "商用環境"),
        candidate("next_action", "商用環境"),
        {"text": "商用環境", "evidence": "商用環境"},
        candidate("scope", ""),
        candidate("scope", "   ", "商用環境"),
        {"field": "scope", "text": "商用環境"},
        {"field": "scope", "text": "商用環境", "evidence": ""},
        {"field": "scope", "text": "商用環境", "evidence": 1},
        candidate("scope", "商用環境", replaces="存在しない項目"),
    ],
)
def test_invalid_candidate_is_rejected(bad):
    updated, changes, warnings = apply_candidates(
        empty_working_memory(), [bad], "商用環境へ展開します"
    )

    assert updated == empty_working_memory()
    assert changes == []
    assert len(warnings) == 1


def test_same_item_is_not_saved_twice(paths):
    llm = FakeLLM(
        ["承知しました", "はい"],
        [
            extraction(candidate("scope", "商用環境"), candidate("scope", "商用環境")),
            # 表記の違い（全角・空白）だけなら同じ項目として扱う
            extraction(candidate("scope", " 商用環境 ", "商用環境"), candidate("scope", "検証環境")),
        ],
    )
    session = ChatSession(llm, *paths)

    outputs = run_cli(session, ["商用環境へ展開します", "商用環境と検証環境です", "exit"])

    assert load_working_memory(paths[2])["scope"] == [item("商用環境"), item("検証環境")]
    assert [o for o in outputs if o.startswith("[作業記憶]")] == [
        "[作業記憶] scope: 商用環境",
        "[作業記憶] scope: 検証環境",
    ]
    assert not any(o.startswith("[警告]") for o in outputs)


def test_same_text_in_another_field_is_not_a_duplicate():
    working_memory = empty_working_memory()
    working_memory["scope"] = [item("3環境")]

    updated, changes, _ = apply_candidates(
        working_memory, [candidate("mission", "3環境")], "3環境"
    )

    assert updated["mission"] == [item("3環境")]
    assert len(changes) == 1


def test_existing_item_can_be_replaced(paths):
    save_working_memory(paths[2], TASK_WORKING_MEMORY)
    new_text = "3環境すべてで動作確認が取れれば完了"
    llm = FakeLLM(
        ["承知しました"],
        [extraction(candidate("acceptance_criteria", new_text, replaces="3環境すべて揃えば完了"))],
    )
    session = ChatSession(llm, *paths)

    outputs = run_cli(session, [f"完了条件を変えます。{new_text}とします", "exit"])

    saved = load_working_memory(paths[2])
    assert saved["acceptance_criteria"] == [item(new_text)]
    assert saved["scope"] == TASK_WORKING_MEMORY["scope"]
    assert f"[作業記憶] acceptance_criteria: 3環境すべて揃えば完了 → {new_text}" in outputs
    # 既存の項目を提案の材料として抽出AIへ渡している
    assert TASK_BLOCK in llm.working_calls[0]


def test_apply_candidates_does_not_modify_the_original():
    original = empty_working_memory()

    updated, _, _ = apply_candidates(original, [candidate("scope", "商用環境")], "商用環境")

    assert original == empty_working_memory()
    assert updated["scope"] == [item("商用環境")]


# --- 抽出の失敗は会話を止めない ---


@pytest.mark.parametrize(
    "bad_extraction",
    [
        "scopeに商用環境を追加しました。",
        '[{"field": "scope", "text": "商用',
        '{"field": "scope", "text": "商用環境", "evidence": "商用環境"}',
        LLMError("接続できません"),
    ],
)
def test_failed_extraction_does_not_fail_the_conversation(paths, bad_extraction):
    llm = FakeLLM(["承知しました", "二回目の応答"], [bad_extraction, "[]"])
    session = ChatSession(llm, *paths)

    outputs = run_cli(session, ["商用環境へ展開します", "続けます", "exit"])

    assert "\nAI> 承知しました" in outputs
    assert any(o.startswith("[警告] 作業記憶を抽出できませんでした") for o in outputs)
    assert "\nAI> 二回目の応答" in outputs
    assert outputs[-1] == "終了します。"
    assert len(load_history(paths[0])) == 4
    assert not paths[2].exists()


def test_save_failure_is_a_warning_and_state_is_unchanged(tmp_path):
    # 親が通常ファイルなのでディレクトリを作れず、保存に失敗する
    blocker = tmp_path / "blocked"
    blocker.write_text("", encoding="utf-8")
    llm = FakeLLM(["承知しました", "二回目の応答"], [extraction(candidate("scope", "商用環境"))])
    session = ChatSession(
        llm,
        tmp_path / "conversation.jsonl",
        tmp_path / "memories.jsonl",
        blocker / "working_memory.json",
    )

    outputs = run_cli(session, ["商用環境へ展開します", "続けます", "exit"])

    assert any(o.startswith("[警告] 作業記憶を保存できませんでした") for o in outputs)
    assert "\nAI> 二回目の応答" in outputs
    # 保存できなかった項目は、メモリ上にも持たない
    assert session.working_memory == empty_working_memory()


def test_no_extraction_when_reply_failed(paths):
    class FailingLLM(FakeLLM):
        def complete(self, messages, memories=(), working_memory=None, history_events=()):
            raise LLMError("接続できません")

    llm = FailingLLM([])
    session = ChatSession(llm, *paths)

    outputs = run_cli(session, ["商用環境へ展開します", "quit"])

    assert "[エラー] 接続できません" in outputs
    assert llm.working_calls == []


def test_session_without_working_memory_path_does_not_extract(paths):
    llm = FakeLLM(["こんにちは"])
    session = ChatSession(llm, paths[0], paths[1])

    assert session.update_working_memory("商用環境へ展開します") == ([], [])
    assert llm.working_calls == []


# --- 回答用Claudeへ渡すもの ---


def test_working_memory_is_always_sent_across_turns_and_restart(paths):
    """成功条件1: 作業の前提を伝える → 無関係な会話 → 再起動 → 完成かどうかを聞く。"""
    first = ChatSession(FakeLLM(["承知しました", "眠いですね", "梨はおいしいですね"], [TASK_EXTRACTION]), *paths)
    run_cli(first, [TASK_TEXT, "今日は眠い", "梨が食べたい", "exit"])
    first_prompts = first.llm.prompts
    del first

    llm = FakeLLM(["まだ完成ではありません。", "2です。"])
    second = ChatSession(llm, *paths)
    assert second.working_memory == TASK_WORKING_MEMORY

    run_cli(second, ["ここまでで完成ですか？", "1+1は？", "exit"])

    # 保存されたターン以降、無関係な話題のターンでも毎回先頭に入る
    assert not first_prompts[0].startswith("[working memory]")
    assert all(p.startswith(TASK_BLOCK + "\n\n") for p in first_prompts[1:])
    # 再起動後も、会話履歴なしで作業記憶だけが渡る
    assert llm.prompts[0] == f"{TASK_BLOCK}\n\n[user]\nここまでで完成ですか？"
    assert llm.prompts[1].startswith(TASK_BLOCK + "\n\n[user]\nここまでで完成ですか？")


def test_working_memory_is_sent_when_long_term_search_finds_nothing(paths):
    """成功条件2: 長期記憶の検索が0件でも作業記憶は渡る。"""
    append_memories(paths[1], [NAME, FRUIT])
    save_working_memory(paths[2], TASK_WORKING_MEMORY)
    llm = FakeLLM(["商用・検証・開発の3環境です。"], plans=[plan("環境"), plan("対象")])
    session = ChatSession(llm, *paths)

    outputs = run_cli(session, ["この作業の対象環境は？", "exit"])

    assert outputs.count("[検索結果] 0件") == 2
    assert llm.prompts == [f"{TASK_BLOCK}\n\n[user]\nこの作業の対象環境は？"]


def test_working_memory_is_sent_when_search_fails_or_is_skipped(paths):
    append_memories(paths[1], [NAME])
    save_working_memory(paths[2], TASK_WORKING_MEMORY)
    llm = FakeLLM(["はい", "はい"], plans=["壊れた出力", '{"needs_memory": false, "queries": []}'])
    session = ChatSession(llm, *paths)

    run_cli(session, ["完成ですか？", "本当に？", "exit"])

    assert all(p.startswith(TASK_BLOCK) for p in llm.prompts)


def test_working_memory_and_retrieved_memories_are_both_sent(paths):
    append_memories(paths[1], [NAME, FRUIT])
    save_working_memory(paths[2], TASK_WORKING_MEMORY)
    llm = FakeLLM(["テスト太郎さん、まだ完成ではありません。"], plans=[plan("名前")])
    session = ChatSession(llm, *paths)

    run_cli(session, ["私の名前は？あと、もう完成ですか？", "exit"])

    # 作業記憶、検索でヒットした長期記憶、現在の会話の順
    assert llm.prompts == [
        f"{TASK_BLOCK}\n\n"
        f"[retrieved memories]\n{NAME_LINE}\n[/retrieved memories]\n\n"
        "[user]\n私の名前は？あと、もう完成ですか？"
    ]


def test_saved_conversation_and_all_memories_are_still_not_sent(paths):
    old_turn = [
        {"role": "user", "content": "まず開発環境の定義ファイルから作ってください"},
        {"role": "assistant", "content": "開発環境の定義ファイルを作成しました。"},
    ]
    append_history(paths[0], old_turn)
    append_memories(paths[1], [NAME, FRUIT])
    save_working_memory(paths[2], TASK_WORKING_MEMORY)
    llm = FakeLLM(["テスト太郎さんです。"], plans=[plan("名前")])
    session = ChatSession(llm, *paths)

    run_cli(session, ["私の名前は？", "exit"])

    prompt = llm.prompts[0]
    # 過去の会話全文は入らない
    assert old_turn[0]["content"] not in prompt
    assert old_turn[1]["content"] not in prompt
    assert "[assistant]" not in prompt
    # 長期記憶はヒットしたものだけで、全件は入らない
    assert NAME_LINE in prompt
    assert "梨" not in prompt
    # 原文と長期記憶のファイルは役割を保っている
    assert load_history(paths[0])[:2] == old_turn
    assert load_memories(paths[1]) == [NAME, FRUIT]


def test_long_term_search_and_extraction_still_work_with_working_memory(paths):
    append_memories(paths[1], [NAME, FRUIT])
    blood = {"text": "ユーザーの血液型はO型", "evidence": "私の血液型はO型です"}
    llm = FakeLLM(
        ["16インチですね。", "O型なんですね。"],
        working=[TASK_EXTRACTION],
        plans=[plan("冬用タイヤ"), plan("梨")],
        extractions=["[]", json.dumps([blood], ensure_ascii=False)],
    )
    session = ChatSession(llm, *paths)

    outputs = run_cli(session, [TASK_TEXT, "私の血液型はO型です", "exit"])

    # MVP4: 0件なら1回だけ検索語を変えて再検索する
    assert llm.plan_calls[:2] == [(TASK_TEXT, []), (TASK_TEXT, ["冬用タイヤ"])]
    assert outputs[1:5] == ["[検索] 冬用タイヤ", "[検索結果] 0件", "[再検索] 梨", "[検索結果] 1件"]
    # MVP3: 長期記憶の抽出と保存
    assert "[記憶] ユーザーの血液型はO型" in outputs
    assert load_memories(paths[1])[-1] == {**blood, "origin": "user"}
    # 作業記憶は長期記憶のファイルへは入らない
    assert "商用環境" not in paths[1].read_text(encoding="utf-8")


def test_empty_working_memory_adds_no_block(paths):
    llm = FakeLLM(["こんにちは"])
    session = ChatSession(llm, *paths)

    run_cli(session, ["やあ", "exit"])

    assert llm.prompts == ["[user]\nやあ"]


def test_system_prompt_explains_working_memory():
    assert "[working memory]" in llm_module.SYSTEM_PROMPT
    assert "継続的に守る必要がある条件" in llm_module.SYSTEM_PROMPT


# --- MVP6: remove と変更履歴 ---

THREE_ENVS_TEXT = "今回の作業では商用環境、検証環境、開発環境の3環境を対象にします。3環境すべて対応できれば完了です。"
SWAP_TEXT = "検証環境の代わりにステージング環境を対象にします。"
DROP_TEXT = "ステージング環境はやっぱり対象外にします。"
DROP_EVIDENCE = "ステージング環境はやっぱり対象外にします"

PROD = "商用環境を対象とする"
VERIFY = "検証環境を対象とする"
STAGING = "ステージング環境を対象とする"
DEV = "開発環境を対象とする"
ALL_DONE = "3環境すべてに対応できていること"


def add(field, text, evidence=None):
    return {"operation": "add", "field": field, "text": text, "evidence": evidence or text}


def replace(field, target, text, evidence):
    return {"operation": "replace", "field": field, "target": target, "text": text, "evidence": evidence}


def remove(field, target, evidence=DROP_EVIDENCE):
    return {"operation": "remove", "field": field, "target": target, "evidence": evidence}


def staging_working_memory():
    """MVP5 の実Claudeテストで問題が出たときの、remove 直前の状態。"""
    return {
        "mission": [],
        "scope": [item(PROD, "商用環境"), item(STAGING, "ステージング環境"), item(DEV, "開発環境")],
        "acceptance_criteria": [item(ALL_DONE, "3環境すべて対応できれば完了です")],
    }


def scope_texts(working_memory):
    return [i["text"] for i in working_memory["scope"]]


def without_timestamp(events):
    return [{k: v for k, v in e.items() if k != "timestamp"} for e in events]


def test_remove_candidate_can_be_parsed():
    """1. remove 候補を解析できる。"""
    raw = "```json\n" + extraction(remove("scope", STAGING)) + "\n```"

    [parsed] = parse_candidates(raw)

    assert normalize_candidate(parsed) == {
        "operation": "remove",
        "field": "scope",
        "target": STAGING,
        "evidence": DROP_EVIDENCE,
    }


def test_mvp5_candidates_are_normalized_to_add_and_replace():
    assert normalize_candidate(candidate("scope", PROD)) == add("scope", PROD)
    assert normalize_candidate(candidate("scope", STAGING, "ステージング", replaces=VERIFY)) == (
        replace("scope", VERIFY, STAGING, "ステージング")
    )


def test_existing_scope_item_is_removed_and_others_remain(paths):
    """2〜4. 実在する scope 項目を remove でき、ファイルから消え、他の項目は残る。"""
    save_working_memory(paths[2], staging_working_memory())
    llm = FakeLLM(["承知しました。"], [extraction(remove("scope", STAGING))])
    session = ChatSession(llm, *paths)

    outputs = run_cli(session, [DROP_TEXT, "exit"])

    saved = load_working_memory(paths[2])
    assert saved["scope"] == [item(PROD, "商用環境"), item(DEV, "開発環境")]
    assert "ステージング" not in paths[2].read_text(encoding="utf-8")
    assert session.working_memory == saved
    # 完了条件は連動して書き換えない
    assert saved["acceptance_criteria"] == staging_working_memory()["acceptance_criteria"]
    assert [o for o in outputs if o.startswith("[作業記憶]")] == [
        f"[作業記憶] scope: {STAGING} → 削除"
    ]
    assert not any(o.startswith("[警告]") for o in outputs)


@pytest.mark.parametrize(
    "bad, reason",
    [
        # 5. evidence が今回の発言に無い
        (remove("scope", STAGING, "ステージング環境は不要です"), "evidence"),
        # 6. target が存在しない
        (remove("scope", "災害対策環境を対象とする"), "一致しません"),
        # 7. field が違う（scope にある項目を acceptance_criteria から消そうとする）
        (remove("acceptance_criteria", STAGING), "一致しません"),
        (remove("progress", STAGING), "field"),
        # 8. 曖昧な target（部分一致）では消さない
        (remove("scope", "ステージング環境"), "一致しません"),
        (remove("scope", "環境"), "一致しません"),
        (remove("scope", "ステージング環境を対象"), "一致しません"),
        # target が無い・operation が不正
        ({"operation": "remove", "field": "scope", "evidence": DROP_EVIDENCE}, "target"),
        (remove("scope", ""), "target"),
        ({**remove("scope", STAGING), "operation": "delete"}, "operation"),
    ],
)
def test_invalid_remove_is_rejected_and_nothing_is_removed(bad, reason):
    updated, changes, warnings = apply_candidates(staging_working_memory(), [bad], DROP_TEXT)

    assert updated == staging_working_memory()
    assert changes == []
    assert len(warnings) == 1 and reason in warnings[0]


def test_ambiguous_target_matching_several_items_is_not_removed():
    """8. 同じ文面とみなせる項目が複数あれば、どれを消すか特定できないので消さない。"""
    working_memory = staging_working_memory()
    # 手で編集すると、表記だけが違う項目が重なることがある
    working_memory["scope"].append(item(" ステージング環境を対象とする "))

    updated, changes, warnings = apply_candidates(
        working_memory, [remove("scope", STAGING)], DROP_TEXT
    )

    assert updated == working_memory
    assert changes == []
    assert "特定できません" in warnings[0]


def test_remove_ignores_case_width_and_spaces_like_duplicate_check():
    working_memory = empty_working_memory()
    working_memory["scope"] = [item("ＡＷＳ環境を対象とする"), item(DEV)]

    updated, changes, _ = apply_candidates(
        working_memory,
        [remove("scope", " aws環境を対象とする ", "AWS環境は外します")],
        "AWS環境は外します",
    )

    assert updated["scope"] == [item(DEV)]
    # 履歴には実際に消した項目の文面を残す
    assert changes == [
        {
            "operation": "remove",
            "field": "scope",
            "text": "ＡＷＳ環境を対象とする",
            "evidence": "AWS環境は外します",
        }
    ]


def test_remove_does_not_use_text():
    """remove に text が付いていても、否定文を追加も置き換えもしない。"""
    updated, changes, warnings = apply_candidates(
        staging_working_memory(),
        [{**remove("scope", STAGING), "text": "ステージング環境は対象外とする"}],
        DROP_TEXT,
    )

    assert scope_texts(updated) == [PROD, DEV]
    assert changes[0]["text"] == STAGING
    assert warnings == []


def test_same_item_cannot_be_removed_twice_in_one_turn():
    updated, changes, warnings = apply_candidates(
        staging_working_memory(), [remove("scope", STAGING), remove("scope", STAGING)], DROP_TEXT
    )

    assert scope_texts(updated) == [PROD, DEV]
    assert len(changes) == 1
    assert len(warnings) == 1


def test_remove_and_add_in_one_turn_are_applied_in_order():
    text = "ステージング環境は対象外にして、本番DR環境を対象にします"
    updated, changes, warnings = apply_candidates(
        staging_working_memory(),
        [
            remove("scope", STAGING, "ステージング環境は対象外にして"),
            add("scope", "本番DR環境を対象とする", "本番DR環境を対象にします"),
        ],
        text,
    )

    assert scope_texts(updated) == [PROD, DEV, "本番DR環境を対象とする"]
    assert [c["operation"] for c in changes] == ["remove", "add"]
    assert warnings == []


def test_add_replace_remove_events_are_written_to_history(paths):
    """9〜11. add / replace / remove のイベントが履歴へ保存される（手動スモークテストの流れ）。"""
    llm = FakeLLM(
        ["承知しました。", "承知しました。", "承知しました。"],
        [
            extraction(
                add("scope", PROD, "商用環境"),
                add("scope", VERIFY, "検証環境"),
                add("scope", DEV, "開発環境"),
                add("acceptance_criteria", ALL_DONE, "3環境すべて対応できれば完了です"),
            ),
            extraction(
                replace("scope", VERIFY, STAGING, "検証環境の代わりにステージング環境を対象にします")
            ),
            extraction(remove("scope", STAGING)),
        ],
    )
    session = ChatSession(llm, *paths)

    outputs = run_cli(session, [THREE_ENVS_TEXT, SWAP_TEXT, DROP_TEXT, "exit"])

    assert [o for o in outputs if o.startswith("[作業記憶]")] == [
        f"[作業記憶] scope: {PROD} / {VERIFY} / {DEV}",
        f"[作業記憶] acceptance_criteria: {ALL_DONE}",
        f"[作業記憶] scope: {VERIFY} → {STAGING}",
        f"[作業記憶] scope: {STAGING} → 削除",
    ]
    # 現在状態からは消える
    saved = load_working_memory(paths[2])
    assert scope_texts(saved) == [PROD, DEV]
    assert [i["text"] for i in saved["acceptance_criteria"]] == [ALL_DONE]
    # 履歴には「以前存在した」ことと「後から消した」ことが残る
    history = load_working_memory_history(paths[3])
    assert without_timestamp(history) == [
        {"operation": "add", "field": "scope", "text": PROD, "evidence": "商用環境"},
        {"operation": "add", "field": "scope", "text": VERIFY, "evidence": "検証環境"},
        {"operation": "add", "field": "scope", "text": DEV, "evidence": "開発環境"},
        {
            "operation": "add",
            "field": "acceptance_criteria",
            "text": ALL_DONE,
            "evidence": "3環境すべて対応できれば完了です",
        },
        {
            "operation": "replace",
            "field": "scope",
            "target": VERIFY,
            "text": STAGING,
            "evidence": "検証環境の代わりにステージング環境を対象にします",
        },
        {"operation": "remove", "field": "scope", "text": STAGING, "evidence": DROP_EVIDENCE},
    ]
    assert all(isinstance(e["timestamp"], str) and e["timestamp"] for e in history)


def test_history_is_one_json_object_per_line_and_append_only(paths):
    now = datetime(2026, 10, 7, 10, 0, tzinfo=timezone(timedelta(hours=9)))
    removal = {"operation": "remove", "field": "scope", "text": PROD, "evidence": "外す"}

    append_working_memory_history(paths[3], [add("scope", PROD)], now=now)
    append_working_memory_history(paths[3], [removal], now=now)

    lines = paths[3].read_text(encoding="utf-8").splitlines()
    assert [json.loads(line) for line in lines] == [
        {"timestamp": "2026-10-07T10:00:00+09:00", **add("scope", PROD)},
        {"timestamp": "2026-10-07T10:00:00+09:00", **removal},
    ]


def test_nothing_is_written_to_history_when_nothing_changes(paths):
    save_working_memory(paths[2], staging_working_memory())
    llm = FakeLLM(["はい", "はい"], [extraction(remove("scope", "存在しない項目")), "[]"])
    session = ChatSession(llm, *paths)

    run_cli(session, [DROP_TEXT, "今日は眠い", "exit"])

    assert not paths[3].exists()
    assert load_working_memory(paths[2]) == staging_working_memory()


def test_removed_state_is_read_after_restart(paths):
    """12. 再起動後も remove 後の現在状態を読む。"""
    save_working_memory(paths[2], staging_working_memory())
    first = ChatSession(FakeLLM(["承知しました。"], [extraction(remove("scope", STAGING))]), *paths)
    run_cli(first, [DROP_TEXT, "exit"])
    del first

    llm = FakeLLM(["商用環境と開発環境です。"])
    second = ChatSession(llm, *paths)
    run_cli(second, ["この作業の対象環境は？", "exit"])

    assert scope_texts(second.working_memory) == [PROD, DEV]
    assert llm.prompts == [
        "[working memory]\n"
        f"Scope:\n- {PROD}\n- {DEV}\n\n"
        f"Acceptance Criteria:\n- {ALL_DONE}\n"
        "[/working memory]\n\n"
        "[user]\nこの作業の対象環境は？"
    ]


def test_removed_item_and_history_are_not_sent_to_answering_claude(paths):
    """13〜14. 削除済みの項目も履歴全文も、回答用の Claude へ渡さない。"""
    save_working_memory(paths[2], staging_working_memory())
    llm = FakeLLM(
        ["承知しました。", "商用環境と開発環境です。"], [extraction(remove("scope", STAGING))]
    )
    session = ChatSession(llm, *paths)

    run_cli(session, [DROP_TEXT, "この作業の対象環境は？", "exit"])

    assert load_working_memory_history(paths[3])  # 履歴はある
    prompt = llm.prompts[1]
    block = prompt.split("[/working memory]")[0]
    assert STAGING not in block
    assert PROD in block and DEV in block
    # 履歴のイベントは入らない。ステージングが出てくるのは、今回の起動中の会話としてだけ
    assert '"operation"' not in prompt
    assert "remove" not in prompt
    assert "timestamp" not in prompt
    assert prompt.count("ステージング") == prompt.count(DROP_TEXT) == 1


def test_working_memory_wins_over_old_long_term_memory(paths):
    """15. 長期記憶の検索が古い情報を返しても、現在の作業記憶を優先する指示が入っている。"""
    old = {
        "text": "作業の対象はステージング環境を含む",
        "origin": "user",
        "evidence": "検証環境の代わりにステージング環境を対象にします",
    }
    append_memories(paths[1], [old])
    working_memory = staging_working_memory()
    del working_memory["scope"][1]
    save_working_memory(paths[2], working_memory)
    llm = FakeLLM(["商用環境と開発環境です。"], plans=[plan("ステージング")])
    session = ChatSession(llm, *paths)

    run_cli(session, ["この作業の対象環境は？ステージングは入ってる？", "exit"])

    # 古い長期記憶は検索結果としてそのまま渡る（MVP4 の挙動は変えない）
    prompt = llm.prompts[0]
    assert "[retrieved memories]\n- 作業の対象はステージング環境を含む" in prompt
    assert STAGING not in prompt.split("[/working memory]")[0]
    # その上で、作業記憶を現在の状態として優先させる
    system = llm_module.SYSTEM_PROMPT
    assert "現在有効な作業状態" in system
    assert (
        "取得した記憶と [working memory] が矛盾する場合は、[working memory] を現在の状態として優先"
        in system
    )
    assert "不整合を具体的に指摘" in system


@pytest.mark.parametrize(
    "bad_extraction",
    [
        '[{"operation": "remove", "field": "scope", "target": "ステージ',
        '{"operation": "remove", "field": "scope", "target": "ステージング環境を対象とする"}',
        extraction(remove("scope", "ステージング環境")),
        LLMError("接続できません"),
    ],
)
def test_broken_or_failed_remove_does_not_stop_conversation(paths, bad_extraction):
    """16. 不正JSONや remove 失敗で通常会話を止めない。"""
    save_working_memory(paths[2], staging_working_memory())
    llm = FakeLLM(["承知しました。", "二回目の応答"], [bad_extraction, "[]"])
    session = ChatSession(llm, *paths)

    outputs = run_cli(session, [DROP_TEXT, "続けます", "exit"])

    assert "\nAI> 承知しました。" in outputs
    assert any(o.startswith("[警告]") for o in outputs)
    assert "\nAI> 二回目の応答" in outputs
    assert outputs[-1] == "終了します。"
    assert load_working_memory(paths[2]) == staging_working_memory()
    assert not paths[3].exists()


def test_history_save_failure_is_a_warning_and_state_change_is_kept(tmp_path):
    # 親が通常ファイルなのでディレクトリを作れず、履歴の保存に失敗する
    blocker = tmp_path / "blocked"
    blocker.write_text("", encoding="utf-8")
    working_path = tmp_path / "working_memory.json"
    save_working_memory(working_path, staging_working_memory())
    llm = FakeLLM(["承知しました。", "二回目の応答"], [extraction(remove("scope", STAGING))])
    session = ChatSession(
        llm,
        tmp_path / "conversation.jsonl",
        tmp_path / "memories.jsonl",
        working_path,
        blocker / "working_memory_history.jsonl",
    )

    outputs = run_cli(session, [DROP_TEXT, "続けます", "exit"])

    assert f"[作業記憶] scope: {STAGING} → 削除" in outputs
    assert any(o.startswith("[警告] 作業記憶の変更履歴を保存できませんでした") for o in outputs)
    assert "\nAI> 二回目の応答" in outputs
    assert scope_texts(load_working_memory(working_path)) == [PROD, DEV]


def test_state_save_failure_writes_no_history(tmp_path):
    blocker = tmp_path / "blocked"
    blocker.write_text("", encoding="utf-8")
    history_path = tmp_path / "working_memory_history.jsonl"
    llm = FakeLLM(["承知しました。"], [extraction(add("scope", PROD, "商用環境"))])
    session = ChatSession(
        llm,
        tmp_path / "conversation.jsonl",
        tmp_path / "memories.jsonl",
        blocker / "working_memory.json",
        history_path,
    )

    outputs = run_cli(session, ["商用環境を対象にします", "exit"])

    assert any(o.startswith("[警告] 作業記憶を保存できませんでした") for o in outputs)
    assert not history_path.exists()


def test_mvp5_add_and_replace_still_work_in_both_formats(paths):
    """17. MVP5 の add / replace（operation なしの形式を含む）が引き続き動く。"""
    llm = FakeLLM(
        ["はい", "はい", "はい"],
        [
            extraction(candidate("scope", PROD, "商用環境"), add("scope", VERIFY, "検証環境")),
            extraction(candidate("scope", STAGING, "ステージング環境", replaces=VERIFY)),
            extraction(replace("scope", STAGING, "本番DR環境を対象とする", "本番DR環境")),
        ],
    )
    session = ChatSession(llm, *paths)

    outputs = run_cli(
        session,
        [
            "商用環境と検証環境を対象にします",
            "検証環境ではなくステージング環境にします",
            "やはり本番DR環境にします",
            "exit",
        ],
    )

    assert scope_texts(load_working_memory(paths[2])) == [PROD, "本番DR環境を対象とする"]
    assert [o for o in outputs if o.startswith("[作業記憶]")] == [
        f"[作業記憶] scope: {PROD} / {VERIFY}",
        f"[作業記憶] scope: {VERIFY} → {STAGING}",
        f"[作業記憶] scope: {STAGING} → 本番DR環境を対象とする",
    ]
    assert [e["operation"] for e in load_working_memory_history(paths[3])] == [
        "add",
        "add",
        "replace",
        "replace",
    ]


def test_mvp4_search_still_works_with_working_memory_history(paths):
    """18. MVP4 の検索が引き続き動く。"""
    append_memories(paths[1], [NAME, FRUIT])
    save_working_memory(paths[2], staging_working_memory())
    llm = FakeLLM(["テスト太郎さんです。"], plans=[plan("血液型"), plan("名前")])
    session = ChatSession(llm, *paths)

    outputs = run_cli(session, ["私の名前は？", "exit"])

    assert outputs[1:5] == ["[検索] 血液型", "[検索結果] 0件", "[再検索] 名前", "[検索結果] 1件"]
    assert f"[retrieved memories]\n{NAME_LINE}\n[/retrieved memories]" in llm.prompts[0]


def test_extraction_prompt_asks_for_remove_instead_of_negative_items():
    prompt = llm_module.WORKING_MEMORY_SYSTEM_PROMPT
    for word in ("operation", "add", "replace", "remove", "target"):
        assert word in prompt
    assert "対象外にする・不要にする・外すという発言は remove" in prompt
    assert "否定の内容を add" in prompt
    assert "推測で変更しないでください" in prompt
