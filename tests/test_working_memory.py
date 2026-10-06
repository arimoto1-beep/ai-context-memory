import json

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
    apply_candidates,
    count_items,
    empty_working_memory,
    load_working_memory,
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

    def complete(self, messages, memories=(), working_memory=None):
        self.prompts.append(format_prompt(messages, memories, working_memory))
        return self.replies.pop(0)

    def plan_search(self, user_text, previous_queries=()):
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
    return data / "conversation.jsonl", data / "memories.jsonl", data / "working_memory.json"


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

    assert changes == [{"field": field, "text": text, "replaced": None}]
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
        def complete(self, messages, memories=(), working_memory=None):
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
