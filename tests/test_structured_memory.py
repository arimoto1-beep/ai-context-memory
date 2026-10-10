"""MVP8: 長期記憶の一部を subject / key / value で持ち、現在値と変更履歴を分けて保存・検索する。"""

import io
import json

import pytest

from ai_context_memory import chat as chat_module
from ai_context_memory import cli as cli_module
from ai_context_memory import llm as llm_module
from ai_context_memory.chat import ChatSession
from ai_context_memory.cli import run
from ai_context_memory.history import load_history
from ai_context_memory.llm import (
    LLMError,
    format_memory_history,
    format_memory_state,
    format_prompt,
    format_search_request,
    format_structured_memory_request,
    format_working_memory_request,
)
from ai_context_memory.memory import append_memories, load_memories
from ai_context_memory.search import (
    MAX_HISTORY_RESULTS,
    MAX_QUERIES,
    MAX_RESULTS,
    SearchPlan,
    SearchPlanError,
    parse_plan,
    search_memory_history,
    search_memory_state,
    search_working_memory_history,
)
from ai_context_memory.structured_memory import (
    StructuredMemoryError,
    append_memory_history,
    apply_candidates,
    load_memory_history,
    load_memory_state,
    parse_candidates,
    save_memory_state,
)
from ai_context_memory.working_memory import load_working_memory, save_working_memory

PEAR_TEXT = "私の好きな果物は梨です。"
APPLE_TEXT = "最近は好きな果物はりんごです。"
COFFEE_TEXT = "好きな飲み物はコーヒーです。"

T1 = "2026-10-09T10:00:00+09:00"
T2 = "2026-10-09T10:01:00+09:00"
T3 = "2026-10-09T10:02:00+09:00"


def candidate(key, value, evidence, subject="user"):
    return {"subject": subject, "key": key, "value": value, "evidence": evidence}


def extraction(*candidates):
    """抽出AIの出力（JSON配列の文字列）を作る。"""
    return json.dumps(list(candidates), ensure_ascii=False)


def item(key, value, evidence, updated_at):
    return {"subject": "user", "key": key, "value": value, "evidence": evidence, "updated_at": updated_at}


PEAR = item("favorite_fruit", "梨", PEAR_TEXT, T1)
APPLE = item("favorite_fruit", "りんご", APPLE_TEXT, T2)
COFFEE = item("favorite_drink", "コーヒー", COFFEE_TEXT, T3)
STATE = [APPLE, COFFEE]

ADD_PEAR = {
    "timestamp": T1,
    "operation": "add",
    "subject": "user",
    "key": "favorite_fruit",
    "value": "梨",
    "evidence": PEAR_TEXT,
}
REPLACE_APPLE = {
    "timestamp": T2,
    "operation": "replace",
    "subject": "user",
    "key": "favorite_fruit",
    "old_value": "梨",
    "value": "りんご",
    "evidence": APPLE_TEXT,
}
ADD_COFFEE = {
    "timestamp": T3,
    "operation": "add",
    "subject": "user",
    "key": "favorite_drink",
    "value": "コーヒー",
    "evidence": COFFEE_TEXT,
}
EVENTS = [ADD_PEAR, REPLACE_APPLE, ADD_COFFEE]

# 従来の長期記憶には、古い値と新しい値がどちらも残っている
OLD_MEMORY = {"text": "ユーザーの好きな果物は梨", "origin": "user", "evidence": PEAR_TEXT}
NEW_MEMORY = {"text": "ユーザーは最近はりんごが好き", "origin": "user", "evidence": APPLE_TEXT}
DRINK_MEMORY = {"text": "ユーザーの好きな飲み物はコーヒー", "origin": "user", "evidence": COFFEE_TEXT}

APPLE_LINE = f"- user.favorite_fruit: りんご (evidence: {APPLE_TEXT}, updated_at: {T2})"
COFFEE_LINE = f"- user.favorite_drink: コーヒー (evidence: {COFFEE_TEXT}, updated_at: {T3})"
APPLE_BLOCK = f"[long-term memory state]\n{APPLE_LINE}\n[/long-term memory state]"
ADD_PEAR_ENTRY = (
    f"- {T1}\n"
    "  operation: add\n"
    "  subject: user\n"
    "  key: favorite_fruit\n"
    "  value: 梨\n"
    f"  evidence: {PEAR_TEXT}"
)
REPLACE_APPLE_ENTRY = (
    f"- {T2}\n"
    "  operation: replace\n"
    "  subject: user\n"
    "  key: favorite_fruit\n"
    "  old_value: 梨\n"
    "  value: りんご\n"
    f"  evidence: {APPLE_TEXT}"
)
FRUIT_HISTORY_BLOCK = (
    "[long-term memory history]\n"
    f"{ADD_PEAR_ENTRY}\n\n{REPLACE_APPLE_ENTRY}\n"
    "[/long-term memory history]"
)

WORKING_MEMORY = {
    "mission": [],
    "scope": [{"text": "商用環境", "evidence": "商用環境"}],
    "acceptance_criteria": [],
}
WORKING_BLOCK = "[working memory]\nScope:\n- 商用環境\n[/working memory]"
WM_EVENT = {
    "timestamp": "2026-10-07T18:48:45+09:00",
    "operation": "remove",
    "field": "scope",
    "text": "ステージング環境",
    "evidence": "ステージング環境はやっぱり対象外にします。",
}


def plan(queries=(), memory_history_queries=(), history_queries=()):
    """検索プランナーの出力（JSONオブジェクトの文字列）を作る。"""
    return json.dumps(
        {
            "needs_memory": bool(queries),
            "queries": list(queries),
            "needs_long_term_memory_history": bool(memory_history_queries),
            "memory_history_queries": list(memory_history_queries),
            "needs_working_memory_history": bool(history_queries),
            "history_queries": list(history_queries),
        },
        ensure_ascii=False,
    )


NOTHING_NEEDED = plan()


class FakeLLM:
    """通常回答・検索プラン作成・各種の抽出を別々に記録し、用意された応答を順に返す。"""

    def __init__(self, replies, structured=(), plans=(), extractions=(), working=()):
        self.replies = list(replies)
        self.structured = list(structured)  # 尽きたら「候補なし」を返す
        self.plans = list(plans)  # 尽きたら「検索は不要」を返す
        self.extractions = list(extractions)  # 尽きたら「記憶なし」を返す
        self.working = list(working)  # 尽きたら「候補なし」を返す
        self.prompts = []  # 通常回答でclaudeへ渡されるプロンプト
        self.plan_inputs = []  # 検索プラン作成でclaudeへ渡される入力
        self.structured_inputs = []  # 構造化抽出でclaudeへ渡される入力

    def complete(self, messages, *context):
        self.prompts.append(format_prompt(messages, *context))
        return self.replies.pop(0)

    def plan_search(self, user_text, previous_queries=(), working_memory=None):
        self.plan_inputs.append(format_search_request(user_text, previous_queries, working_memory))
        result = self.plans.pop(0) if self.plans else NOTHING_NEEDED
        if isinstance(result, Exception):
            raise result
        return result

    def extract_memories(self, user_text):
        return self.extractions.pop(0) if self.extractions else "[]"

    def extract_structured_memory(self, user_text, items):
        self.structured_inputs.append(format_structured_memory_request(user_text, items))
        result = self.structured.pop(0) if self.structured else "[]"
        if isinstance(result, Exception):
            raise result
        return result

    def extract_working_memory(self, user_text, working_memory):
        format_working_memory_request(user_text, working_memory)
        return self.working.pop(0) if self.working else "[]"


def write_jsonl(path, records):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "".join(json.dumps(r, ensure_ascii=False) + "\n" for r in records), encoding="utf-8"
    )


@pytest.fixture
def paths(tmp_path):
    """何も保存されていない状態。"""
    data = tmp_path / "data"
    return (
        data / "conversation.jsonl",
        data / "memories.jsonl",
        data / "working_memory.json",
        data / "working_memory_history.jsonl",
        data / "long_term_memory_state.json",
        data / "long_term_memory_history.jsonl",
    )


@pytest.fixture
def saved(paths):
    """再起動後の状態: 梨→りんご、コーヒーまで進めた現在値・変更履歴と、従来の長期記憶が保存済み。"""
    append_memories(paths[1], [OLD_MEMORY, NEW_MEMORY, DRINK_MEMORY])
    save_memory_state(paths[4], STATE)
    write_jsonl(paths[5], EVENTS)
    return paths


@pytest.fixture
def history_searches(monkeypatch):
    """ChatSession からの長期記憶の変更履歴の検索を記録する（検索自体は本物を実行する）。"""
    calls = []

    def spy(events, queries, *args, **kwargs):
        calls.append(list(queries))
        return search_memory_history(events, queries, *args, **kwargs)

    monkeypatch.setattr(chat_module, "search_memory_history", spy)
    return calls


@pytest.fixture
def clock(monkeypatch):
    """変更のたびに T1, T2, T3 を順に返す時計。"""
    times = iter([T1, T2, T3])
    monkeypatch.setattr(chat_module, "timestamp_now", lambda: next(times))


def run_cli(session, inputs):
    it = iter(inputs)
    outputs = []
    run(session, input_fn=lambda prompt: next(it), output_fn=outputs.append)
    return outputs


def block(prompt, name):
    assert f"[{name}]" in prompt
    return prompt.split(f"[{name}]\n")[1].split(f"\n[/{name}]")[0]


# --- 現在値ファイル ---


def test_missing_or_empty_state_file_loads_as_no_items(paths):
    assert load_memory_state(paths[4]) == []
    paths[4].parent.mkdir(parents=True)
    paths[4].write_text("", encoding="utf-8")
    assert load_memory_state(paths[4]) == []


def test_saved_state_can_be_loaded_back(paths):
    """1. 現在値ファイルの読み書き。"""
    save_memory_state(paths[4], STATE)

    assert load_memory_state(paths[4]) == STATE
    assert json.loads(paths[4].read_text(encoding="utf-8")) == {"items": STATE}
    # 人が読める形で保存される
    assert "りんご" in paths[4].read_text(encoding="utf-8")


def test_state_file_without_updated_at_is_accepted(paths):
    paths[4].parent.mkdir(parents=True)
    paths[4].write_text(
        '{"items": [{"subject": "user", "key": "name", "value": "テスト太郎", "evidence": "テスト太郎です"}]}',
        encoding="utf-8",
    )

    assert load_memory_state(paths[4]) == [
        {"subject": "user", "key": "name", "value": "テスト太郎", "evidence": "テスト太郎です"}
    ]


@pytest.mark.parametrize(
    "content",
    [
        "壊れた内容",
        "[]",
        '{"items": {}}',
        '{"items": [{"subject": "user", "key": "name", "evidence": "e"}]}',
        '{"items": [{"subject": "user", "key": "name", "value": 1, "evidence": "e"}]}',
        # 同じ subject + key が複数ある
        '{"items": [{"subject": "user", "key": "name", "value": "a", "evidence": "a"}, '
        '{"subject": "user", "key": "name", "value": "b", "evidence": "b"}]}',
    ],
)
def test_broken_state_file_is_rejected(paths, content):
    paths[4].parent.mkdir(parents=True)
    paths[4].write_text(content, encoding="utf-8")

    with pytest.raises(StructuredMemoryError):
        load_memory_state(paths[4])


def test_state_save_failure_becomes_error(tmp_path):
    blocker = tmp_path / "data"
    blocker.write_text("", encoding="utf-8")

    with pytest.raises(StructuredMemoryError, match="保存できませんでした"):
        save_memory_state(blocker / "long_term_memory_state.json", STATE)


# --- 変更履歴ファイル ---


def test_history_events_are_appended_one_json_object_per_line(paths):
    """2. 変更履歴ファイルの読み書き。追記のみで、1行1イベント。"""
    add = {k: v for k, v in ADD_PEAR.items() if k != "timestamp"}
    replace = {k: v for k, v in REPLACE_APPLE.items() if k != "timestamp"}

    append_memory_history(paths[5], [add], T1)
    append_memory_history(paths[5], [replace], T2)

    assert load_memory_history(paths[5]) == [ADD_PEAR, REPLACE_APPLE]
    lines = paths[5].read_text(encoding="utf-8").splitlines()
    assert [json.loads(line) for line in lines] == [ADD_PEAR, REPLACE_APPLE]
    assert list(json.loads(lines[1])) == [
        "timestamp",
        "operation",
        "subject",
        "key",
        "old_value",
        "value",
        "evidence",
    ]


def test_missing_history_file_loads_as_no_events(paths):
    assert load_memory_history(paths[5]) == []


@pytest.mark.parametrize("content", ["壊れた行\n", '["add"]\n'])
def test_broken_history_file_is_rejected_with_line_number(paths, content):
    write_jsonl(paths[5], [ADD_PEAR])
    with open(paths[5], "a", encoding="utf-8") as f:
        f.write(content)

    with pytest.raises(StructuredMemoryError, match="2 行目"):
        load_memory_history(paths[5])


# --- 候補の解析と検証 ---


def test_candidates_are_parsed_from_json_array_with_or_without_code_fence():
    raw = extraction(candidate("favorite_fruit", "梨", PEAR_TEXT))

    assert parse_candidates(raw) == [candidate("favorite_fruit", "梨", PEAR_TEXT)]
    assert parse_candidates(f"```json\n{raw}\n```") == parse_candidates(raw)
    assert parse_candidates("[]") == []


@pytest.mark.parametrize("raw", ["候補はありません。", '{"subject": "user"}', '[{"subject": "user"'])
def test_extraction_that_is_not_a_json_array_is_rejected(raw):
    with pytest.raises(StructuredMemoryError):
        parse_candidates(raw)


def test_user_subject_is_saved():
    """3・8. subject=user を保存できる。現在値が無ければ add。"""
    updated, changes, warnings = apply_candidates(
        [], [candidate("favorite_fruit", "梨", PEAR_TEXT)], PEAR_TEXT, T1
    )

    assert updated == [PEAR]
    assert changes == [{k: v for k, v in ADD_PEAR.items() if k != "timestamp"}]
    assert warnings == []


@pytest.mark.parametrize("subject", ["assistant", "車", "User", "", None])
def test_subject_other_than_user_is_rejected(subject):
    """4."""
    updated, changes, warnings = apply_candidates(
        [], [candidate("favorite_fruit", "梨", PEAR_TEXT, subject=subject)], PEAR_TEXT, T1
    )

    assert (updated, changes) == ([], [])
    assert len(warnings) == 1 and "subject が不正です" in warnings[0]


def test_candidate_without_subject_is_rejected():
    bad = {"key": "favorite_fruit", "value": "梨", "evidence": PEAR_TEXT}

    assert apply_candidates([], [bad], PEAR_TEXT, T1)[0] == []


@pytest.mark.parametrize("key", ["", "   ", None])
def test_empty_key_is_rejected(key):
    """5."""
    updated, changes, warnings = apply_candidates(
        [], [candidate(key, "梨", PEAR_TEXT)], PEAR_TEXT, T1
    )

    assert (updated, changes) == ([], [])
    assert "key がありません" in warnings[0]


@pytest.mark.parametrize(
    "key", ["Favorite_Fruit", "favorite-fruit", "favorite fruit", "好きな果物", "1st_fruit", "_fruit", "a" * 65]
)
def test_key_that_is_not_snake_case_is_rejected(key):
    updated, changes, warnings = apply_candidates(
        [], [candidate(key, "梨", PEAR_TEXT)], PEAR_TEXT, T1
    )

    assert (updated, changes) == ([], [])
    assert "key の形式が不正です" in warnings[0]


@pytest.mark.parametrize("value", ["", "   ", None, 3])
def test_empty_value_is_rejected(value):
    """6."""
    updated, changes, warnings = apply_candidates(
        [], [candidate("favorite_fruit", value, PEAR_TEXT)], PEAR_TEXT, T1
    )

    assert (updated, changes) == ([], [])
    assert "value がありません" in warnings[0]


@pytest.mark.parametrize("evidence", ["私の好きな果物は梨である", "好きな果物は、梨です。", "", None])
def test_evidence_that_is_not_in_the_utterance_is_rejected(evidence):
    """7. evidence は今回の発言に一字一句含まれていなければならない。"""
    updated, changes, warnings = apply_candidates(
        [], [candidate("favorite_fruit", "梨", evidence)], PEAR_TEXT, T1
    )

    assert (updated, changes) == ([], [])
    assert "evidence" in warnings[0]


def test_non_object_candidate_is_rejected_and_others_are_still_applied():
    updated, changes, warnings = apply_candidates(
        [], ["favorite_fruit=梨", candidate("favorite_fruit", "梨", PEAR_TEXT)], PEAR_TEXT, T1
    )

    assert updated == [PEAR]
    assert len(changes) == 1
    assert warnings == ["長期状態の候補を保存しませんでした: 形式が不正です"]


# --- 現在値の状態遷移 ---


def test_new_value_for_same_subject_and_key_replaces_current_value():
    """9・10・11. 同じ subject + key に別の value が来たら replace。現在値には新しい value だけが残る。"""
    updated, changes, warnings = apply_candidates(
        [PEAR], [candidate("favorite_fruit", "りんご", APPLE_TEXT)], APPLE_TEXT, T2
    )

    assert updated == [APPLE]
    assert "梨" not in json.dumps(updated, ensure_ascii=False)
    # 古い値は変更（＝履歴のイベント）の old_value に残る
    assert changes == [{k: v for k, v in REPLACE_APPLE.items() if k != "timestamp"}]
    assert warnings == []


def test_apply_candidates_does_not_modify_the_original():
    original = [dict(PEAR)]

    apply_candidates(original, [candidate("favorite_fruit", "りんご", APPLE_TEXT)], APPLE_TEXT, T2)

    assert original == [PEAR]


@pytest.mark.parametrize("value", ["りんご", " りんご ", "りんご\n"])
def test_same_value_changes_nothing(value):
    """12. 同じ subject / key / value なら何もしない。evidence も updated_at も変えない。"""
    text = "好きな果物はりんごです。"

    updated, changes, warnings = apply_candidates(
        [APPLE], [candidate("favorite_fruit", value, text)], text, T3
    )

    assert updated == [APPLE]
    assert (changes, warnings) == ([], [])


def test_same_value_ignores_case_and_character_width_only():
    current = [item("favorite_language", "Python", "Pythonが好き", T1)]

    same = apply_candidates(
        current, [candidate("favorite_language", "ｐｙｔｈｏｎ", "ｐｙｔｈｏｎが好き")], "ｐｙｔｈｏｎが好き", T2
    )
    different = apply_candidates(
        [APPLE], [candidate("favorite_fruit", "リンゴ", "リンゴが好き")], "リンゴが好き", T2
    )

    assert same[1] == []
    # 表記が違うだけで同じ意味かどうかは、Pythonでは判断しない
    assert [c["operation"] for c in different[1]] == ["replace"]


def test_different_key_is_added_independently():
    """13. 別の key は別の項目として追加され、既存の項目は変わらない。"""
    updated, changes, warnings = apply_candidates(
        [APPLE], [candidate("favorite_drink", "コーヒー", COFFEE_TEXT)], COFFEE_TEXT, T3
    )

    assert updated == [APPLE, COFFEE]
    assert changes == [{k: v for k, v in ADD_COFFEE.items() if k != "timestamp"}]
    assert warnings == []


def test_several_keys_in_one_utterance_are_all_applied():
    text = "好きな果物は梨で、好きな飲み物は緑茶です。"

    updated, changes, _ = apply_candidates(
        [APPLE],
        [candidate("favorite_fruit", "梨", "好きな果物は梨"), candidate("favorite_drink", "緑茶", "好きな飲み物は緑茶")],
        text,
        T3,
    )

    assert [(i["key"], i["value"]) for i in updated] == [("favorite_fruit", "梨"), ("favorite_drink", "緑茶")]
    assert [c["operation"] for c in changes] == ["replace", "add"]


def test_same_key_twice_in_one_extraction_uses_only_the_first():
    text = "好きな果物は梨とりんごです。"

    updated, changes, warnings = apply_candidates(
        [],
        [candidate("favorite_fruit", "梨", "好きな果物は梨"), candidate("favorite_fruit", "りんご", "りんご")],
        text,
        T1,
    )

    assert [(i["key"], i["value"]) for i in updated] == [("favorite_fruit", "梨")]
    assert [c["operation"] for c in changes] == ["add"]
    assert len(warnings) == 1 and "user.favorite_fruit の候補が複数" in warnings[0]


def test_python_has_no_keyword_table_and_uses_whatever_key_the_ai_chose():
    # AIが別の key を選べば、Pythonは同じ意味かどうかを判断せず別の項目として追加する
    text = "最近一番好きなフルーツはりんごです。"

    updated, changes, _ = apply_candidates(
        [APPLE], [candidate("most_favorite_fruit", "りんご", text)], text, T3
    )

    assert [i["key"] for i in updated] == ["favorite_fruit", "most_favorite_fruit"]
    assert [c["operation"] for c in changes] == ["add"]


# --- 会話の中での抽出・保存・表示 ---


def test_pear_then_apple_then_coffee(paths, clock):
    """成功条件1〜3: 梨で add、りんごで replace、コーヒーは別の key として add。"""
    llm = FakeLLM(
        ["梨ですね。", "りんごに変わったんですね。", "コーヒーですね。"],
        [
            extraction(candidate("favorite_fruit", "梨", PEAR_TEXT)),
            extraction(candidate("favorite_fruit", "りんご", APPLE_TEXT)),
            extraction(candidate("favorite_drink", "コーヒー", COFFEE_TEXT)),
        ],
    )
    session = ChatSession(llm, *paths)

    outputs = run_cli(session, [PEAR_TEXT, APPLE_TEXT, COFFEE_TEXT, "exit"])

    assert [o for o in outputs if o.startswith("[長期状態]")] == [
        "[長期状態] user.favorite_fruit: 梨",
        "[長期状態] user.favorite_fruit: 梨 → りんご",
        "[長期状態] user.favorite_drink: コーヒー",
    ]
    assert load_memory_state(paths[4]) == STATE
    assert "梨" not in paths[4].read_text(encoding="utf-8")
    assert load_memory_history(paths[5]) == EVENTS
    assert not any(o.startswith("[警告]") for o in outputs)


def test_extractor_receives_existing_keys_and_the_utterance_only(paths, clock):
    """既存の key を再利用できるよう、抽出AIへ現在の subject / key / value を見せる。"""
    llm = FakeLLM(
        ["梨ですね。", "りんごに変わったんですね。"],
        [extraction(candidate("favorite_fruit", "梨", PEAR_TEXT)), "[]"],
    )
    session = ChatSession(llm, *paths)

    run_cli(session, [PEAR_TEXT, APPLE_TEXT, "exit"])

    assert llm.structured_inputs == [
        f"[long-term memory state]\n(empty)\n[/long-term memory state]\n\n[utterance]\n{PEAR_TEXT}",
        "[long-term memory state]\n- user.favorite_fruit: 梨\n[/long-term memory state]\n\n"
        f"[utterance]\n{APPLE_TEXT}",
    ]
    # assistantの発言、evidence、日時は渡さない
    for unwanted in ("梨ですね。", "evidence", T1):
        assert unwanted not in llm.structured_inputs[1]


def test_same_value_again_shows_nothing_and_writes_nothing(saved):
    """成功条件6: 同じ値をもう一度述べても、現在値も履歴も変えない。"""
    text = "好きな果物はりんごです。"
    llm = FakeLLM(["りんごですね。"], [extraction(candidate("favorite_fruit", "りんご", text))])
    session = ChatSession(llm, *saved)
    state_before = saved[4].read_bytes()
    history_before = saved[5].read_bytes()

    outputs = run_cli(session, [text, "exit"])

    assert not any(o.startswith("[長期状態]") or o.startswith("[警告]") for o in outputs)
    assert saved[4].read_bytes() == state_before
    assert saved[5].read_bytes() == history_before
    assert session.memory_state == STATE


@pytest.mark.parametrize("text", ["1+1は？", "今日は眠いです"])
def test_utterance_without_candidates_saves_nothing(paths, text):
    """成功条件7: 候補が無ければ何も作らない。"""
    llm = FakeLLM(["はい。"], ["[]"])
    session = ChatSession(llm, *paths)

    outputs = run_cli(session, [text, "exit"])

    assert not any(o.startswith("[長期状態]") for o in outputs)
    assert not paths[4].exists()
    assert not paths[5].exists()


def test_rejected_candidate_is_a_warning_and_valid_ones_are_saved(paths, clock):
    llm = FakeLLM(
        ["梨ですね。"],
        [
            extraction(
                candidate("favorite_fruit", "梨", PEAR_TEXT),
                candidate("favorite_fruit", "梨", PEAR_TEXT, subject="assistant"),
                candidate("好きな果物", "梨", PEAR_TEXT),
                candidate("favorite_color", "青", "好きな色は青です"),
            )
        ],
    )
    session = ChatSession(llm, *paths)

    outputs = run_cli(session, [PEAR_TEXT, "exit"])

    assert load_memory_state(paths[4]) == [PEAR]
    assert len([o for o in outputs if o.startswith("[警告] 長期状態の候補を保存しませんでした")]) == 3
    assert outputs[-1] == "終了します。"


def test_current_state_is_read_after_restart(paths, clock):
    """14. 再起動後も現在値を読める。"""
    llm = FakeLLM(
        ["梨ですね。", "りんごに変わったんですね。"],
        [
            extraction(candidate("favorite_fruit", "梨", PEAR_TEXT)),
            extraction(candidate("favorite_fruit", "りんご", APPLE_TEXT)),
        ],
    )
    run_cli(ChatSession(llm, *paths), [PEAR_TEXT, APPLE_TEXT, "exit"])

    restarted = ChatSession(FakeLLM([]), *paths)

    assert restarted.memory_state == [APPLE]
    assert restarted.messages == []


def test_session_without_state_path_does_not_extract(paths):
    llm = FakeLLM(["梨ですね。"], [extraction(candidate("favorite_fruit", "梨", PEAR_TEXT))])
    session = ChatSession(llm, *paths[:4])

    run_cli(session, [PEAR_TEXT, "exit"])

    assert llm.structured_inputs == []
    assert session.update_memory_state(PEAR_TEXT) == ([], [])


def test_no_structured_extraction_when_reply_failed(paths):
    class Failing(FakeLLM):
        def complete(self, messages, *context):
            raise LLMError("接続できません")

    llm = Failing([], [extraction(candidate("favorite_fruit", "梨", PEAR_TEXT))])

    outputs = run_cli(ChatSession(llm, *paths), [PEAR_TEXT, "exit"])

    assert "[エラー] 接続できません" in outputs
    assert llm.structured_inputs == []
    assert not paths[4].exists()


def test_legacy_memory_extraction_still_runs_alongside(paths, clock):
    """従来の memories.jsonl への抽出は変わらず、同じ内容が両方に入ることを許容する。"""
    llm = FakeLLM(
        ["梨ですね。"],
        [extraction(candidate("favorite_fruit", "梨", PEAR_TEXT))],
        extractions=[json.dumps([{"text": OLD_MEMORY["text"], "evidence": PEAR_TEXT}], ensure_ascii=False)],
    )
    session = ChatSession(llm, *paths)

    outputs = run_cli(session, [PEAR_TEXT, "exit"])

    assert "[記憶] ユーザーの好きな果物は梨" in outputs
    assert "[長期状態] user.favorite_fruit: 梨" in outputs
    assert load_memories(paths[1]) == [OLD_MEMORY]
    assert load_memory_state(paths[4]) == [PEAR]


# --- 失敗しても会話を止めない ---


@pytest.mark.parametrize(
    "bad_extraction",
    ["候補はありません。", '{"subject": "user"}', '[{"subject": "user"', LLMError("接続できません")],
)
def test_failed_extraction_does_not_stop_the_conversation(paths, bad_extraction):
    """30. 抽出の失敗・不正なJSON。"""
    llm = FakeLLM(["梨ですね。", "2です。"], [bad_extraction])
    session = ChatSession(llm, *paths)

    outputs = run_cli(session, [PEAR_TEXT, "1+1は？", "exit"])

    assert any(o.startswith("[警告] 長期状態を抽出できませんでした") for o in outputs)
    assert "\nAI> 梨ですね。" in outputs
    assert "\nAI> 2です。" in outputs
    assert outputs[-1] == "終了します。"
    assert not paths[4].exists()
    assert len(load_history(paths[0])) == 4


def test_state_save_failure_is_a_warning_and_nothing_changes(tmp_path):
    """30. 現在値の保存に失敗したら、現在値も履歴も変えない。"""
    blocker = tmp_path / "blocked"
    blocker.write_text("", encoding="utf-8")
    history_path = tmp_path / "long_term_memory_history.jsonl"
    llm = FakeLLM(["梨ですね。"], [extraction(candidate("favorite_fruit", "梨", PEAR_TEXT))])
    session = ChatSession(
        llm, memory_state_path=blocker / "state.json", memory_history_path=history_path
    )

    outputs = run_cli(session, [PEAR_TEXT, "exit"])

    assert any(o.startswith("[警告] 長期状態を保存できませんでした") for o in outputs)
    assert not any(o.startswith("[長期状態]") for o in outputs)
    assert session.memory_state == []
    assert not history_path.exists()
    assert "\nAI> 梨ですね。" in outputs


def test_history_save_failure_is_a_warning_and_state_change_is_kept(tmp_path):
    """30. 履歴の追記だけ失敗したら、現在値の変更は有効なまま。"""
    blocker = tmp_path / "blocked"
    blocker.write_text("", encoding="utf-8")
    state_path = tmp_path / "long_term_memory_state.json"
    llm = FakeLLM(["梨ですね。"], [extraction(candidate("favorite_fruit", "梨", PEAR_TEXT))])
    session = ChatSession(
        llm, memory_state_path=state_path, memory_history_path=blocker / "history.jsonl"
    )

    outputs = run_cli(session, [PEAR_TEXT, "exit"])

    assert any(o.startswith("[警告] 長期状態の変更履歴を保存できませんでした") for o in outputs)
    assert "[長期状態] user.favorite_fruit: 梨" in outputs
    assert [i["value"] for i in load_memory_state(state_path)] == ["梨"]


# --- 現在値の検索 ---


def test_state_search_matches_subject():
    """15・16."""
    assert search_memory_state(STATE, ["user"]) == STATE


def test_state_search_matches_key():
    """17."""
    assert search_memory_state(STATE, ["favorite_fruit"]) == [APPLE]
    assert search_memory_state(STATE, ["favorite"]) == STATE


def test_state_search_matches_value():
    """18. 「コーヒー」は evidence にもあるので、value にだけ現れる語で確かめる。"""
    items = [item("favorite_fruit", "紅玉", "好きな果物はこれです", T1)]

    assert search_memory_state(items, ["紅玉"]) == items
    assert search_memory_state(STATE, ["コーヒー"]) == [COFFEE]


def test_state_search_matches_evidence():
    """19. 「果物」は evidence にだけ現れる。"""
    assert search_memory_state(STATE, ["果物"]) == [APPLE]


def test_state_search_is_and_within_a_query_or_across_queries_and_ordered_by_score():
    assert search_memory_state(STATE, ["favorite_fruit コーヒー"]) == []
    assert search_memory_state(STATE, ["果物", "飲み物"]) == STATE
    # コーヒーは2つの検索語にヒットするので、保存順が後でも先に来る
    assert search_memory_state(STATE, ["果物", "飲み物", "コーヒー"]) == [COFFEE, APPLE]


def test_state_search_ignores_case_and_width_and_does_not_search_updated_at():
    assert search_memory_state(STATE, ["FAVORITE_ＦＲＵＩＴ"]) == [APPLE]
    assert search_memory_state(STATE, ["2026-10-09"]) == []


def test_state_search_does_not_exceed_limit():
    items = [item(f"pet_{i}", f"タマ{i}", f"猫{i}号はタマ{i}", T1) for i in range(MAX_RESULTS + 3)]

    assert search_memory_state(items, ["pet"]) == items[:MAX_RESULTS]
    assert search_memory_state(items, ["pet"], limit=2) == items[:2]


def test_state_block_format():
    assert format_memory_state([APPLE, COFFEE]) == (
        f"[long-term memory state]\n{APPLE_LINE}\n{COFFEE_LINE}\n[/long-term memory state]"
    )
    assert format_memory_state([]) == ""


# --- 変更履歴の検索 ---


def test_history_search_finds_events():
    """20. 検索対象は operation / subject / key / value / old_value / evidence。"""
    assert search_memory_history(EVENTS, ["replace"]) == [REPLACE_APPLE]
    assert search_memory_history(EVENTS, ["user"]) == EVENTS
    assert search_memory_history(EVENTS, ["favorite_fruit"]) == [ADD_PEAR, REPLACE_APPLE]
    assert search_memory_history(EVENTS, ["コーヒー"]) == [ADD_COFFEE]
    assert search_memory_history(EVENTS, ["最近"]) == [REPLACE_APPLE]  # evidence にだけ現れる


def test_history_search_matches_old_value():
    """21. 「梨」は replace のイベントでは old_value にだけ現れる。"""
    assert search_memory_history([REPLACE_APPLE, ADD_COFFEE], ["梨"]) == [REPLACE_APPLE]


def test_history_search_results_keep_timestamp_and_are_in_time_order():
    """22."""
    found = search_memory_history(EVENTS, ["果物", "replace"])

    # スコアは replace のほうが高いが、結果は古い順に戻す
    assert found == [ADD_PEAR, REPLACE_APPLE]
    assert [e["timestamp"] for e in found] == [T1, T2]


def test_history_search_does_not_search_timestamp_and_handles_empty_input():
    assert search_memory_history(EVENTS, ["2026-10-09"]) == []
    assert search_memory_history([], ["果物"]) == []
    assert search_memory_history(EVENTS, []) == []


def test_history_search_does_not_exceed_limit_and_prefers_newer_events():
    events = [
        {"timestamp": f"2026-10-09T10:{i:02d}:00+09:00", "operation": "replace", "subject": "user",
         "key": "mood_color", "old_value": f"色{i}", "value": f"色{i + 1}", "evidence": f"色{i + 1}にします"}
        for i in range(MAX_HISTORY_RESULTS + 4)
    ]

    found = search_memory_history(events, ["mood_color"])

    assert len(found) == MAX_HISTORY_RESULTS
    assert found == events[4:]


def test_history_block_format():
    assert format_memory_history([ADD_PEAR, REPLACE_APPLE]) == FRUIT_HISTORY_BLOCK
    assert format_memory_history([]) == ""


# --- 検索プランの解析 ---


def test_plan_with_long_term_memory_history_keys_is_parsed():
    raw = (
        '{"needs_memory": true, "queries": ["果物"], '
        '"needs_long_term_memory_history": true, "memory_history_queries": ["favorite_fruit", "果物"], '
        '"needs_working_memory_history": false, "history_queries": []}'
    )

    assert parse_plan(raw) == SearchPlan(["果物"], [], ["favorite_fruit", "果物"])


def test_all_three_sources_can_be_planned_at_once():
    assert parse_plan(plan(["果物"], ["梨"], ["scope"])) == SearchPlan(["果物"], ["scope"], ["梨"])


def test_mvp7_plan_format_means_long_term_memory_history_is_not_needed():
    """28. MVP7 形式（長期記憶の変更履歴のキーが無い）も、そのまま解析できる。"""
    raw = (
        '{"needs_memory": false, "queries": [], '
        '"needs_working_memory_history": true, "history_queries": ["scope"]}'
    )

    assert parse_plan(raw) == SearchPlan([], ["scope"], [])
    assert parse_plan('{"needs_memory": true, "queries": ["名前"]}') == SearchPlan(["名前"], [])


def test_memory_history_queries_are_ignored_unless_explicitly_needed():
    assert parse_plan(
        '{"needs_long_term_memory_history": false, "memory_history_queries": ["果物"]}'
    ).memory_history_queries == []
    assert parse_plan('{"memory_history_queries": ["果物"]}').memory_history_queries == []


def test_memory_history_queries_are_cleaned_deduplicated_and_capped():
    queries = parse_plan(
        plan(memory_history_queries=["果物", " 果物 ", "", "favorite_fruit　梨", "梨", "りんご"])
    ).memory_history_queries

    assert queries == ["果物", "favorite_fruit 梨", "梨"]
    assert len(queries) == MAX_QUERIES


@pytest.mark.parametrize(
    "raw",
    [
        '{"needs_long_term_memory_history": true, "memory_history_queries": "果物"}',
        '{"needs_long_term_memory_history": true, "memory_history_queries": ["果物", 1]}',
    ],
)
def test_invalid_memory_history_queries_are_rejected(raw):
    with pytest.raises(SearchPlanError):
        parse_plan(raw)


def test_planner_prompt_explains_all_sources():
    prompt = llm_module.SEARCH_PLAN_SYSTEM_PROMPT

    for word in (
        "needs_memory",
        "queries",
        "needs_long_term_memory_history",
        "memory_history_queries",
        "needs_working_memory_history",
        "history_queries",
        "old_value",
        "snake_case",
    ):
        assert word in prompt


# --- 質問ごとの使い分け ---


def test_current_value_question_gets_current_state_and_does_not_search_history(saved, history_searches):
    """23・成功条件4: 現在値の質問では現在値を検索し、変更履歴は検索しない。"""
    llm = FakeLLM(["りんごです。"], plans=[plan(["果物"])])
    session = ChatSession(llm, *saved)

    outputs = run_cli(session, ["今、私の好きな果物は？", "exit"])

    assert history_searches == []
    # 検索プランナーへは現在値も履歴も渡さない
    assert llm.plan_inputs == ["[question]\n今、私の好きな果物は？"]
    # 現在値のりんごと、従来の記憶（古い梨を含む）の両方が、同じ検索語で取得される
    assert llm.prompts == [
        f"{APPLE_BLOCK}\n\n"
        "[retrieved memories]\n"
        f"- ユーザーの好きな果物は梨 (origin: user, evidence: {PEAR_TEXT})\n"
        f"- ユーザーは最近はりんごが好き (origin: user, evidence: {APPLE_TEXT})\n"
        "[/retrieved memories]\n\n"
        "[user]\n今、私の好きな果物は？"
    ]
    assert "[long-term memory history]" not in llm.prompts[0]
    assert outputs[1:3] == ["[検索] 果物", "[検索結果] 3件"]
    assert not any("長期履歴検索" in o for o in outputs)


def test_only_hit_items_are_sent_not_the_whole_state(saved):
    llm = FakeLLM(["コーヒーです。"], plans=[plan(["飲み物"])])
    session = ChatSession(llm, *saved)

    run_cli(session, ["私の好きな飲み物は？", "exit"])

    assert block(llm.prompts[0], "long-term memory state") == COFFEE_LINE
    assert "りんご" not in llm.prompts[0]


def test_past_value_question_searches_history_and_sends_only_hits(saved, history_searches):
    """24・成功条件5: 過去値の質問では変更履歴を検索し、ヒットしたイベントだけを渡す。"""
    queries = ["favorite_fruit", "果物"]
    llm = FakeLLM(["以前は梨で、現在はりんごです。"], plans=[plan(["果物"], queries)])
    session = ChatSession(llm, *saved)

    outputs = run_cli(session, ["前に好きだった果物は？", "exit"])

    assert history_searches == [queries]
    prompt = llm.prompts[0]
    assert block(prompt, "long-term memory history") == f"{ADD_PEAR_ENTRY}\n\n{REPLACE_APPLE_ENTRY}"
    # 保存されている timestamp と old_value がそのまま渡る
    assert T1 in prompt and T2 in prompt
    assert "  old_value: 梨" in prompt
    # ヒットしなかったイベント（履歴の全文）は渡らない
    assert "favorite_drink" not in prompt
    assert prompt.index(APPLE_BLOCK) < prompt.index("[long-term memory history]")
    assert prompt.index("[/long-term memory history]") < prompt.index("[retrieved memories]")
    assert outputs[1:5] == [
        "[長期履歴検索] favorite_fruit / 果物",
        "[長期履歴検索結果] 2件",
        "[検索] 果物",
        "[検索結果] 3件",
    ]
    # イベントの中身はCLIへ表示しない
    assert not any("old_value" in o for o in outputs)
    # 検索結果は会話履歴には入らない
    assert [m["content"] for m in session.messages] == ["前に好きだった果物は？", "以前は梨で、現在はりんごです。"]


def test_general_question_searches_nothing(saved, history_searches):
    llm = FakeLLM(["2です。"], plans=[NOTHING_NEEDED])
    session = ChatSession(llm, *saved)

    outputs = run_cli(session, ["1+1は？", "exit"])

    assert history_searches == []
    assert len(llm.plan_inputs) == 1
    assert llm.prompts == ["[user]\n1+1は？"]
    assert not any("検索" in o for o in outputs)


def test_current_state_alone_is_searchable_without_legacy_memories(saved):
    saved[1].unlink()
    llm = FakeLLM(["りんごです。"], plans=[plan(["favorite_fruit"]), plan(["果物"])])
    session = ChatSession(llm, *saved)

    outputs = run_cli(session, ["今、私の好きな果物は？", "exit"])

    # 現在値がヒットすれば再検索しない
    assert len(llm.plan_inputs) == 1
    assert llm.prompts == [f"{APPLE_BLOCK}\n\n[user]\n今、私の好きな果物は？"]
    assert outputs[1:3] == ["[検索] favorite_fruit", "[検索結果] 1件"]


def test_retry_happens_only_when_both_long_term_sources_have_no_hits(saved, history_searches):
    """29. 0件時の再検索は維持する。変更履歴の検索は1回目のプランの分だけ。"""
    llm = FakeLLM(
        ["りんごです。"],
        plans=[plan(["フルーツ"], ["フルーツ"]), plan(["果物"], ["梨"])],
    )
    session = ChatSession(llm, *saved)

    outputs = run_cli(session, ["今、私の好きなフルーツは？", "exit"])

    assert history_searches == [["フルーツ"]]
    assert outputs[1:7] == [
        "[長期履歴検索] フルーツ",
        "[長期履歴検索結果] 0件",
        "[検索] フルーツ",
        "[検索結果] 0件",
        "[再検索] 果物",
        "[検索結果] 3件",
    ]
    assert llm.plan_inputs[1] == (
        "[question]\n今、私の好きなフルーツは？\n\n[previous queries: 0 hits]\n- フルーツ"
    )
    assert "[long-term memory history]" not in llm.prompts[0]
    assert APPLE_BLOCK in llm.prompts[0]


def test_planner_is_not_called_when_there_is_nothing_to_search(paths):
    llm = FakeLLM(["はじめまして"])
    session = ChatSession(llm, *paths)

    run_cli(session, ["前に好きだった果物は？", "exit"])

    assert llm.plan_inputs == []
    assert llm.prompts == ["[user]\n前に好きだった果物は？"]


def test_conversation_continues_without_history_file_or_hits(saved, history_searches):
    saved[5].unlink()
    llm = FakeLLM(
        ["履歴からは確認できません。", "履歴からは確認できません。"],
        plans=[plan([], ["果物"]), plan([], ["住所"])],
    )
    session = ChatSession(llm, *saved)

    outputs = run_cli(session, ["前に好きだった果物は？", "exit"])
    write_jsonl(saved[5], EVENTS)
    outputs += run_cli(session, ["前に住んでいた場所は？", "exit"])

    assert outputs.count("[長期履歴検索結果] 0件") == 2
    assert all("[long-term memory history]" not in p for p in llm.prompts)
    assert outputs.count("\nAI> 履歴からは確認できません。") == 2
    assert outputs[-1] == "終了します。"


def test_broken_history_file_is_a_warning_and_conversation_continues(saved):
    saved[5].write_text("壊れた行\n", encoding="utf-8")
    llm = FakeLLM(["りんごです。"], plans=[plan(["果物"], ["果物"])])
    session = ChatSession(llm, *saved)

    outputs = run_cli(session, ["前に好きだった果物は？", "exit"])

    assert any(o.startswith("[警告] 長期状態の変更履歴がJSONとして読めません") for o in outputs)
    assert "[long-term memory history]" not in llm.prompts[0]
    assert APPLE_BLOCK in llm.prompts[0]
    assert "\nAI> りんごです。" in outputs


def test_broken_plan_searches_nothing_and_conversation_continues(saved, history_searches):
    llm = FakeLLM(["分かりません。"], plans=["検索します。"])
    session = ChatSession(llm, *saved)

    outputs = run_cli(session, ["前に好きだった果物は？", "exit"])

    assert history_searches == []
    assert any(o.startswith("[警告] 記憶を検索できませんでした") for o in outputs)
    assert llm.prompts == ["[user]\n前に好きだった果物は？"]


# --- 作業記憶と一緒に動く ---


def test_all_blocks_are_sent_in_order_with_working_memory(saved):
    """27・28. 作業記憶とその変更履歴の検索も、同じ1回の検索プランで引き続き動く。"""
    save_working_memory(saved[2], WORKING_MEMORY)
    write_jsonl(saved[3], [WM_EVENT])
    llm = FakeLLM(["お答えします。"], plans=[plan(["果物"], ["favorite_fruit"], ["remove"])])
    session = ChatSession(llm, *saved)

    outputs = run_cli(session, ["果物の好みと対象環境は、それぞれどう変わってきた？", "exit"])

    assert len(llm.plan_inputs) == 1
    assert llm.plan_inputs[0].startswith(f"{WORKING_BLOCK}\n\n[question]\n")
    assert llm.prompts[0] == (
        f"{WORKING_BLOCK}\n\n"
        "[working memory history]\n"
        "- 2026-10-07T18:48:45+09:00\n"
        "  operation: remove\n"
        "  field: scope\n"
        "  text: ステージング環境\n"
        "  evidence: ステージング環境はやっぱり対象外にします。\n"
        "[/working memory history]\n\n"
        f"{APPLE_BLOCK}\n\n"
        f"{FRUIT_HISTORY_BLOCK}\n\n"
        "[retrieved memories]\n"
        f"- ユーザーの好きな果物は梨 (origin: user, evidence: {PEAR_TEXT})\n"
        f"- ユーザーは最近はりんごが好き (origin: user, evidence: {APPLE_TEXT})\n"
        "[/retrieved memories]\n\n"
        "[user]\n果物の好みと対象環境は、それぞれどう変わってきた？"
    )
    assert outputs[1:7] == [
        "[履歴検索] remove",
        "[履歴検索結果] 1件",
        "[長期履歴検索] favorite_fruit",
        "[長期履歴検索結果] 2件",
        "[検索] 果物",
        "[検索結果] 3件",
    ]


def test_working_memory_history_search_is_unaffected_by_long_term_history(saved, monkeypatch):
    save_working_memory(saved[2], WORKING_MEMORY)
    write_jsonl(saved[3], [WM_EVENT])
    calls = []

    def spy(events, queries, *args, **kwargs):
        calls.append(list(queries))
        return search_working_memory_history(events, queries, *args, **kwargs)

    monkeypatch.setattr(chat_module, "search_working_memory_history", spy)
    llm = FakeLLM(["18時48分です。", "りんごです。"], plans=[plan(history_queries=["ステージング環境"]), plan(["果物"])])
    session = ChatSession(llm, *saved)

    run_cli(session, ["ステージング環境を外したのはいつ？", "今、私の好きな果物は？", "exit"])

    assert calls == [["ステージング環境"]]
    assert "[working memory history]" in llm.prompts[0]
    assert "[long-term memory" not in llm.prompts[0]
    assert "[working memory history]" not in llm.prompts[1]


def test_working_memory_and_structured_memory_are_updated_in_the_same_turn(paths, clock):
    text = "今回の作業では商用環境を対象にします。ちなみに私の好きな果物は梨です。"
    llm = FakeLLM(
        ["承知しました。"],
        [extraction(candidate("favorite_fruit", "梨", "私の好きな果物は梨です"))],
        working=[
            json.dumps(
                [{"operation": "add", "field": "scope", "text": "商用環境", "evidence": "商用環境を対象にします"}],
                ensure_ascii=False,
            )
        ],
    )
    session = ChatSession(llm, *paths)

    outputs = run_cli(session, [text, "exit"])

    assert "[長期状態] user.favorite_fruit: 梨" in outputs
    assert "[作業記憶] scope: 商用環境" in outputs
    assert [i["text"] for i in load_working_memory(paths[2])["scope"]] == ["商用環境"]
    assert [i["key"] for i in load_memory_state(paths[4])] == ["favorite_fruit"]
    # 作業の条件は長期状態には入らない（AIの判断）。Pythonは2つの状態を混ぜない
    assert "商用環境" not in paths[4].read_text(encoding="utf-8")


# --- Claudeへの指示 ---


def test_system_prompt_says_current_state_wins_over_legacy_memories():
    """25. 現在値と従来の記憶が矛盾したら、現在値を優先する指示が入る。"""
    system = llm_module.SYSTEM_PROMPT

    assert "[long-term memory state]" in system
    assert "これは現在有効な値です。" in system
    assert "その後に値が変わる前の古い情報が混ざっていることがあります。" in system
    assert (
        "[long-term memory state] と [retrieved memories] が矛盾する場合は、"
        "[long-term memory state] を現在の値として優先してください。"
    ) in system
    # 現在の発言と作業記憶の優先は変わらない
    assert "現在の会話でユーザーが別の値を述べた場合は、現在の発言を優先してください。" in system
    assert "現在の状態については、常に [working memory] を優先してください。" in system


def test_system_prompt_says_history_values_are_not_current():
    """26. 変更履歴の古い値を現在値として扱わない指示が入る。"""
    system = llm_module.SYSTEM_PROMPT

    assert "[long-term memory history]" in system
    assert "これらは過去の値の変更の記録であって、現在の値ではありません。" in system
    assert "old_value や、後のイベントで置き換えられた value を、現在の値として扱わないでください。" in system
    assert "現在の値については、[long-term memory state] を優先してください。" in system
    assert "推定としてではなく、履歴を根拠として答えてください。" in system
    assert "そこに無い変更を補って述べないでください。" in system


def test_extraction_prompt_leaves_state_transition_to_python():
    prompt = llm_module.STRUCTURED_MEMORY_SYSTEM_PROMPT

    for word in ("subject", "key", "value", "evidence", "snake_case", "[long-term memory state]"):
        assert word in prompt
    assert "新しい key を作らず、その key を一字一句そのまま使ってください。" in prompt
    assert "既存のどの key とも意味が違う属性なら、新しい key を作ってください。" in prompt
    assert "値を追加するのか置き換えるのかを判断する必要はありません。" in prompt
    assert "候補がなければ [] だけを返してください。" in prompt
    assert "operation" not in prompt


# --- 起動 ---


class FakeStdin(io.StringIO):
    def reconfigure(self, **kwargs):
        pass


@pytest.fixture
def main_env(tmp_path, monkeypatch):
    """main() を実データ領域にも実際の claude にも触れさせずに動かす。"""
    names = {
        "ACM_HISTORY_FILE": "conversation.jsonl",
        "ACM_MEMORY_FILE": "memories.jsonl",
        "ACM_WORKING_MEMORY_FILE": "working_memory.json",
        "ACM_WORKING_MEMORY_HISTORY_FILE": "working_memory_history.jsonl",
        "ACM_LONG_TERM_MEMORY_STATE_FILE": "state.json",
        "ACM_LONG_TERM_MEMORY_HISTORY_FILE": "state_history.jsonl",
    }
    for name, filename in names.items():
        monkeypatch.setenv(name, str(tmp_path / filename))
    monkeypatch.setattr(cli_module, "ClaudeCLI", lambda: FakeLLM([]))
    monkeypatch.setattr(cli_module.sys, "stdin", FakeStdin("exit\n"))
    monkeypatch.setattr(cli_module.sys.stdout, "reconfigure", lambda **kwargs: None, raising=False)
    return tmp_path / "state.json"


def test_main_reports_loaded_state(main_env, capsys):
    save_memory_state(main_env, STATE)

    assert cli_module.main() == 0
    assert "長期状態を読み込みました（2項目）" in capsys.readouterr().out


def test_main_exits_with_error_on_broken_state_file(main_env, capsys):
    main_env.write_text("壊れた内容", encoding="utf-8")

    assert cli_module.main() == 1
    out = capsys.readouterr().out
    assert "[エラー] 長期状態ファイル" in out
    assert "チャットを開始します" not in out
