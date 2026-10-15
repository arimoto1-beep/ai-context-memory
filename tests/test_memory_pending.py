"""MVP9: AIが既存の値と新しい発言の関係を判断し、Pythonは確かめられる置き換えだけを実行し、残りを保留にする。"""

import io
import json

import pytest

from ai_context_memory import cli as cli_module
from ai_context_memory import llm as llm_module
from ai_context_memory.chat import ChatSession
from ai_context_memory.cli import run
from ai_context_memory.history import load_history
from ai_context_memory.llm import (
    format_memory_pending,
    format_prompt,
    format_search_request,
    format_structured_memory_request,
    format_working_memory_request,
)
from ai_context_memory.memory import append_memories
from ai_context_memory.search import MAX_RESULTS, related_memory_pending, search_memory_pending
from ai_context_memory.structured_memory import (
    ENDING_EXPRESSIONS,
    StructuredMemoryError,
    add_pending,
    apply_candidates,
    load_memory_history,
    load_memory_pending,
    load_memory_state,
    parse_extraction,
    save_memory_pending,
    save_memory_state,
)
from ai_context_memory.working_memory import load_working_memory, save_working_memory

T0 = "2026-10-01T09:00:00+09:00"
T1 = "2026-10-14T10:00:00+09:00"
T2 = "2026-10-14T10:01:00+09:00"
T3 = "2026-10-14T10:02:00+09:00"

KAWASAKI_TEXT = "今は川崎に住んでいます。"
KAWASAKI = {
    "subject": "user",
    "key": "home_location",
    "value": "川崎",
    "evidence": KAWASAKI_TEXT,
    "updated_at": T0,
}
STATE = [KAWASAKI]

NAGOYA_TEXT = "名古屋に住んでいます。"
NOW_NAGOYA_TEXT = "今は名古屋に住んでいます。"
MOVED_TEXT = "川崎の家を引き払って名古屋に引っ越しました。"
BOTH_TEXT = "川崎にも住んでいますが、名古屋にも家があります。"
TWO_PLACES_TEXT = "平日は名古屋、週末は川崎で暮らしています。"
TRIP_TEXT = "今日は出張で札幌にいます。"
BORN_TEXT = "川崎で生まれて、今は名古屋に住んでいます。"


def judgment(decision, evidence, **fields):
    """関係判断AIの出力の1要素。指定しないキーは null（判断に関係しない）にする。"""
    return {
        "decision": decision,
        "subject": "user",
        "target_key": None,
        "proposed_key": None,
        "proposed_value": None,
        "evidence": evidence,
        "old_value_status": None,
        "old_value_evidence": None,
        "temporal_scope": "current",
        "reason": "判断理由です。",
        "confirmation_question": None,
        **fields,
    }


def replace(evidence, value, old_value_evidence, **fields):
    defaults = {
        "target_key": "home_location",
        "proposed_value": value,
        "old_value_status": "ended",
        "old_value_evidence": old_value_evidence,
        "confirmation_question": "川崎には今も住んでいますか？",
    }
    return judgment("replace", evidence, **{**defaults, **fields})


def pending(evidence, value, **fields):
    defaults = {
        "target_key": "home_location",
        "proposed_value": value,
        "old_value_status": "not_stated",
        "confirmation_question": "川崎には今も住んでいますか？",
    }
    return judgment("pending", evidence, **{**defaults, **fields})


def coexist(evidence, key, value, **fields):
    defaults = {
        "target_key": "home_location",
        "proposed_key": key,
        "proposed_value": value,
        "old_value_status": "continues",
    }
    return judgment("coexist", evidence, **{**defaults, **fields})


def extraction(*judgments):
    return json.dumps(list(judgments), ensure_ascii=False)


def plan(queries=()):
    return json.dumps(
        {
            "needs_memory": bool(queries),
            "queries": list(queries),
            "needs_long_term_memory_history": False,
            "memory_history_queries": [],
            "needs_working_memory_history": False,
            "history_queries": [],
        },
        ensure_ascii=False,
    )


NOTHING_NEEDED = plan()

NAGOYA_PENDING = {
    "id": "p001",
    "subject": "user",
    "target_key": "home_location",
    "target_value": "川崎",
    "proposed_key": None,
    "proposed_value": "名古屋",
    "evidence": NOW_NAGOYA_TEXT,
    "decision": "replace",
    "hold_reason": "old_value_evidence に既存の値「川崎」が含まれていません",
    "reason": "判断理由です。",
    "confirmation_question": "川崎には今も住んでいますか？",
    "status": "unresolved",
    "created_at": T1,
}


class FakeLLM:
    """通常回答・検索プラン作成・各種の抽出を別々に記録し、用意された応答を順に返す。"""

    def __init__(self, replies, structured=(), plans=(), working=()):
        self.replies = list(replies)
        self.structured = list(structured)  # 尽きたら「扱う属性なし」を返す
        self.plans = list(plans)  # 尽きたら「検索は不要」を返す
        self.working = list(working)  # 尽きたら「候補なし」を返す
        self.prompts = []
        self.plan_inputs = []
        self.structured_inputs = []

    def complete(self, messages, *context):
        self.prompts.append(format_prompt(messages, *context))
        return self.replies.pop(0)

    def plan_search(self, user_text, previous_queries=(), working_memory=None):
        self.plan_inputs.append(format_search_request(user_text, previous_queries, working_memory))
        return self.plans.pop(0) if self.plans else NOTHING_NEEDED

    def extract_memories(self, user_text):
        return "[]"

    def extract_structured_memory(self, user_text, items):
        self.structured_inputs.append(format_structured_memory_request(user_text, items))
        return self.structured.pop(0) if self.structured else "[]"

    def extract_working_memory(self, user_text, working_memory):
        format_working_memory_request(user_text, working_memory)
        return self.working.pop(0) if self.working else "[]"


@pytest.fixture
def paths(tmp_path):
    """現在値が home_location = 川崎 だけの状態。保留と変更履歴はまだ無い。"""
    data = tmp_path / "data"
    files = (
        data / "conversation.jsonl",
        data / "memories.jsonl",
        data / "working_memory.json",
        data / "working_memory_history.jsonl",
        data / "long_term_memory_state.json",
        data / "long_term_memory_history.jsonl",
        data / "long_term_memory_pending.json",
    )
    save_memory_state(files[4], STATE)
    return files


@pytest.fixture
def clock(monkeypatch):
    times = iter([T1, T2, T3])
    monkeypatch.setattr("ai_context_memory.chat.timestamp_now", lambda: next(times))


def run_cli(session, inputs):
    it = iter(inputs)
    outputs = []
    run(session, input_fn=lambda prompt: next(it), output_fn=outputs.append)
    return outputs


def block(prompt, name):
    assert f"[{name}]" in prompt
    return prompt.split(f"[{name}]\n")[1].split(f"\n[/{name}]")[0]


def apply(judgments, text, items=STATE):
    return apply_candidates(items, judgments, text, T1)


def operations(changes):
    return [c["operation"] for c in changes]


# --- 状態遷移 ---


def test_explicit_replace_replaces_current_value():
    """1. 旧値の終了が発言で確かめられる replace は、現在値を置き換えて old_value を残す。"""
    text = "川崎の家を引き払って、名古屋に引っ越しました。"

    updated, changes, warnings = apply([replace(text, "名古屋", "川崎の家を引き払って")], text)

    assert updated == [
        {"subject": "user", "key": "home_location", "value": "名古屋", "evidence": text, "updated_at": T1}
    ]
    assert changes == [
        {
            "operation": "replace",
            "subject": "user",
            "key": "home_location",
            "old_value": "川崎",
            "value": "名古屋",
            "evidence": text,
        }
    ]
    assert warnings == []


def test_coexist_adds_a_new_key_and_keeps_the_current_value():
    """2."""
    updated, changes, warnings = apply(
        [coexist(TWO_PLACES_TEXT, "weekday_residence", "名古屋")], TWO_PLACES_TEXT
    )

    assert updated == [
        KAWASAKI,
        {
            "subject": "user",
            "key": "weekday_residence",
            "value": "名古屋",
            "evidence": TWO_PLACES_TEXT,
            "updated_at": T1,
        },
    ]
    assert changes == [
        {
            "operation": "add",
            "subject": "user",
            "key": "weekday_residence",
            "value": "名古屋",
            "evidence": TWO_PLACES_TEXT,
        }
    ]
    assert warnings == []


def test_pending_keeps_the_current_value_and_holds_the_statement():
    """3."""
    updated, changes, warnings = apply([pending(NAGOYA_TEXT, "名古屋")], NAGOYA_TEXT)

    assert updated == STATE
    assert changes == [
        {
            "operation": "pending",
            "subject": "user",
            "target_key": "home_location",
            "target_value": "川崎",
            "proposed_key": None,
            "proposed_value": "名古屋",
            "evidence": NAGOYA_TEXT,
            "decision": "pending",
            "hold_reason": None,
            "reason": "判断理由です。",
            "confirmation_question": "川崎には今も住んでいますか？",
        }
    ]
    assert warnings == []


@pytest.mark.parametrize(
    "text, judged",
    [
        (TRIP_TEXT, judgment("no_change", TRIP_TEXT, target_key="home_location", temporal_scope="temporary")),
        ("今も川崎に住んでいます。", judgment("no_change", "今も川崎に住んでいます。", target_key="home_location")),
    ],
)
def test_no_change_changes_nothing(text, judged):
    """4. 一時的な滞在や再確認では、現在値も保留も変えない。"""
    assert apply([judged], text) == (STATE, [], [])


def test_replace_with_the_current_value_changes_nothing():
    text = "今も川崎に住んでいます。"

    assert apply([replace(text, "川崎", None)], text) == (STATE, [], [])


def test_coexist_on_an_existing_key_does_not_overwrite():
    """5. coexist でも既存の key と衝突したら上書きしない。違う値なら保留にする。"""
    updated, changes, warnings = apply(
        [coexist(NAGOYA_TEXT, "home_location", "名古屋")], NAGOYA_TEXT
    )

    assert updated == STATE
    assert operations(changes) == ["pending"]
    assert changes[0]["target_value"] == "川崎"
    assert changes[0]["proposed_value"] == "名古屋"
    assert "上書きしません" in changes[0]["hold_reason"]
    assert warnings == []


def test_coexist_on_an_existing_key_with_the_same_value_changes_nothing():
    text = "住まいは川崎です。"

    assert apply([coexist(text, "home_location", "川崎")], text) == (STATE, [], [])


@pytest.mark.parametrize("subject", ["assistant", "User", "", None])
def test_invalid_subject_is_rejected(subject):
    """6."""
    for judged in (
        replace(MOVED_TEXT, "名古屋", "川崎の家を引き払って", subject=subject),
        pending(NAGOYA_TEXT, "名古屋", subject=subject),
    ):
        updated, changes, warnings = apply([judged], judged["evidence"] + MOVED_TEXT)

        assert (updated, changes) == (STATE, [])
        assert len(warnings) == 1 and "subject が不正です" in warnings[0]


@pytest.mark.parametrize("evidence", ["名古屋に住んでいる", "名古屋に住んでいます。。", "", None])
def test_evidence_that_is_not_in_the_utterance_is_rejected(evidence):
    """7. replace でも pending でも、根拠が今回の発言に一字一句無ければ反映しない（保留にもしない）。"""
    for judged in (replace(evidence, "名古屋", "川崎の家を引き払って"), pending(evidence, "名古屋")):
        updated, changes, warnings = apply([judged], NAGOYA_TEXT)

        assert (updated, changes) == (STATE, [])
        assert len(warnings) == 1 and "evidence" in warnings[0]


@pytest.mark.parametrize(
    "raw",
    [
        "判断できません。",
        json.dumps(pending(NAGOYA_TEXT, "名古屋"), ensure_ascii=False),  # 配列ではない
        '[{"decision": "pending"',
        "",
    ],
)
def test_invalid_json_is_rejected(raw):
    """8."""
    with pytest.raises(StructuredMemoryError):
        parse_extraction(raw)


@pytest.mark.parametrize("decision", ["update", "delete", "REPLACE", "", None, 1])
def test_unknown_decision_is_rejected(decision):
    """9."""
    judged = {**pending(NAGOYA_TEXT, "名古屋"), "decision": decision}

    updated, changes, warnings = apply([judged], NAGOYA_TEXT)

    assert (updated, changes) == (STATE, [])
    assert len(warnings) == 1 and "decision が不正です" in warnings[0]


def test_pending_save_failure_is_a_warning_and_the_conversation_continues(tmp_path, clock):
    """10. 保留の保存に失敗しても、会話は止まらず、現在値も変わらない。"""
    state_path = tmp_path / "long_term_memory_state.json"
    save_memory_state(state_path, STATE)
    blocker = tmp_path / "blocked"
    blocker.write_text("", encoding="utf-8")
    llm = FakeLLM(["名古屋なんですね。", "2です。"], [extraction(pending(NAGOYA_TEXT, "名古屋"))])
    session = ChatSession(
        llm,
        tmp_path / "conversation.jsonl",
        memory_state_path=state_path,
        memory_pending_path=blocker / "pending.json",
    )

    outputs = run_cli(session, [NAGOYA_TEXT, "1+1は？", "exit"])

    assert any(o.startswith("[警告] 長期保留を保存できませんでした") for o in outputs)
    assert not any(o.startswith("[長期保留]") for o in outputs)
    assert "\nAI> 名古屋なんですね。" in outputs
    assert "\nAI> 2です。" in outputs
    assert session.memory_pending == []
    assert load_memory_state(state_path) == STATE
    assert len(load_history(tmp_path / "conversation.jsonl")) == 4


def test_failed_relation_judgment_is_a_warning_and_changes_nothing(paths):
    llm = FakeLLM(["名古屋なんですね。"], ["判断できません。"])

    outputs = run_cli(ChatSession(llm, *paths), [NAGOYA_TEXT, "exit"])

    assert any(o.startswith("[警告] 長期状態を抽出できませんでした") for o in outputs)
    assert load_memory_state(paths[4]) == STATE
    assert not paths[6].exists()
    assert outputs[-1] == "終了します。"


# --- 上書き保護 ---


@pytest.mark.parametrize(
    "judged",
    [
        pending(NAGOYA_TEXT, "名古屋"),
        # AIが誤って replace と判断し、終了も自己申告した場合
        replace(NAGOYA_TEXT, "名古屋", NAGOYA_TEXT),
    ],
)
def test_living_in_nagoya_does_not_remove_kawasaki(judged):
    """11. 「名古屋に住んでいます。」で川崎を消さない。"""
    updated, changes, _ = apply([judged], NAGOYA_TEXT)

    assert updated == STATE
    assert [(c["operation"], c["proposed_value"]) for c in changes] == [("pending", "名古屋")]


@pytest.mark.parametrize("old_value_evidence", ["今は名古屋に住んでいます", None, ""])
def test_now_living_in_nagoya_does_not_remove_kawasaki_even_if_ai_says_ended(old_value_evidence):
    """12. 事前実験で 5/5 replace になった「今は名古屋に住んでいます。」でも、川崎を無条件に消さない。"""
    judged = replace(NOW_NAGOYA_TEXT, "名古屋", old_value_evidence)

    updated, changes, _ = apply([judged], NOW_NAGOYA_TEXT)

    assert updated == STATE
    assert operations(changes) == ["pending"]
    assert changes[0]["decision"] == "replace"
    assert changes[0]["hold_reason"]


@pytest.mark.parametrize(
    "judged",
    [
        # MVP8 で起きた誤置換と同じ判断を、MVP9 の形でAIが返した場合
        replace("単身赴任で、平日は名古屋に住んでいます。", "平日は名古屋", "単身赴任で、平日は名古屋に住んでいます。"),
        coexist("単身赴任で、平日は名古屋に住んでいます。", "home_location", "平日は名古屋"),
    ],
)
def test_mvp8_misreplacement_no_longer_removes_kawasaki(judged):
    """MVP8 で川崎が「平日は名古屋」に置き換わった発言でも、AIが同じ誤りをしても川崎を消さない。"""
    text = "単身赴任で、平日は名古屋に住んでいます。"

    updated, changes, _ = apply([judged], text)

    assert updated == STATE
    assert [(c["operation"], c["proposed_value"]) for c in changes] == [("pending", "平日は名古屋")]


def test_moving_out_of_kawasaki_can_replace():
    """13. 「川崎の家を引き払って名古屋に引っ越しました。」は条件を満たせば置き換えられる。"""
    updated, changes, warnings = apply([replace(MOVED_TEXT, "名古屋", "川崎の家を引き払って")], MOVED_TEXT)

    assert [i["value"] for i in updated] == ["名古屋"]
    assert [(c["operation"], c["old_value"], c["value"]) for c in changes] == [("replace", "川崎", "名古屋")]
    assert warnings == []


def test_living_in_both_places_does_not_replace():
    """14. 川崎が発言に現れても、終了・訂正の表現が無ければ置き換えない。"""
    judged = replace(BOTH_TEXT, "名古屋", "川崎にも住んでいます")

    updated, changes, _ = apply([judged], BOTH_TEXT)

    assert updated == STATE
    assert operations(changes) == ["pending"]
    assert changes[0]["hold_reason"] == "old_value_evidence に終了・変更・訂正を表す表現がありません"


@pytest.mark.parametrize(
    "fields, reason",
    [
        ({"old_value_evidence": None}, "old_value_evidence）がありません"),
        ({"old_value_evidence": "名古屋に引っ越しました"}, "既存の値「川崎」が含まれていません"),
        ({"old_value_evidence": "川崎の家を引き払った"}, "元の発言に含まれていません"),
        ({"old_value_status": "not_stated"}, "old_value_status: not_stated"),
        ({"old_value_status": None}, "old_value_status: None"),
        ({"temporal_scope": "future"}, "temporal_scope: future"),
        ({"target_key": "residence"}, "user.residence が現在の長期状態にありません"),
        ({"target_key": None}, "置き換える既存の項目（target_key）がありません"),
        ({"proposed_value": None}, "proposed_value）がありません"),
        # 新しい値もユーザーが述べたものに限る（AIが作った値では置き換えない）
        ({"proposed_value": "横浜"}, "置き換え後の値「横浜」が元の発言に含まれていません"),
    ],
)
def test_ai_self_report_alone_cannot_authorize_replace(fields, reason):
    """15. AIが ended と申告しても、発言の原文で確かめられる条件を満たさなければ置き換えない。

    逆に、AIの申告（old_value_status / temporal_scope）が条件を満たさないときは、原文の条件を満たしていても止める。
    """
    judged = {**replace(MOVED_TEXT, "名古屋", "川崎の家を引き払って"), **fields}

    updated, changes, _ = apply([judged], MOVED_TEXT)

    assert updated == STATE
    assert operations(changes) == ["pending"]
    assert reason in changes[0]["hold_reason"]


def test_old_value_merely_appearing_in_the_utterance_does_not_replace():
    """16. 旧値が文章に含まれるだけでは置き換えない。"""
    judged = replace(BORN_TEXT, "名古屋", "川崎で生まれて")

    updated, changes, _ = apply([judged], BORN_TEXT)

    assert updated == STATE
    assert operations(changes) == ["pending"]


NOT_MOVED_TEXT = "川崎の家は引き払っていません。"
QUIT_JOB_TEXT = "川崎の職場を辞めました。"
MOVED_WITH_COMMA_TEXT = "川崎の家を引き払って、名古屋に引っ越しました。"


@pytest.mark.parametrize(
    "text, value, old_value_evidence",
    [
        # A. 否定。AIが誤って replace と判断し、発言に無い新しい値を作った場合も、値なしの場合も
        (NOT_MOVED_TEXT, "名古屋", "川崎の家は引き払っていません"),
        (NOT_MOVED_TEXT, None, "川崎の家は引き払っていません"),
        # B. 住まいではない別のものの終了
        (QUIT_JOB_TEXT, "名古屋", "川崎の職場を辞めました"),
        (QUIT_JOB_TEXT, None, "川崎の職場を辞めました"),
        # C. 両方に住んでいる。抜き出しが狭くても、発言全体でも
        (BOTH_TEXT, "名古屋", "川崎にも住んでいます"),
        (BOTH_TEXT, "名古屋", BOTH_TEXT),
    ],
)
def test_mistaken_replace_does_not_overwrite_the_home(text, value, old_value_evidence):
    """MVP9 仕上げ A〜C. AIが誤って replace + ended を返しても、住まいの現在値を上書きしない。"""
    updated, changes, _ = apply([replace(text, value, old_value_evidence)], text)

    assert updated == STATE
    assert operations(changes) == ["pending"]


def test_explicit_move_with_a_comma_can_replace():
    """MVP9 仕上げ D. 明確な更新は置き換えられる。"""
    updated, changes, _ = apply(
        [replace(MOVED_WITH_COMMA_TEXT, "名古屋", "川崎の家を引き払って")], MOVED_WITH_COMMA_TEXT
    )

    assert [i["value"] for i in updated] == ["名古屋"]
    assert operations(changes) == ["replace"]


@pytest.mark.xfail(
    strict=True,
    reason="既知の限界: 新しい値が発言にあり、抜き出しに旧値と終了の表現があれば、"
    "否定や別のものの終了でも Python では止められない（docs/mvp-09.md）",
)
@pytest.mark.parametrize(
    "text, old_value_evidence",
    [
        ("川崎の家は引き払っていません。名古屋にも部屋を借りました。", "川崎の家は引き払っていません"),
        ("川崎の職場を辞めて、名古屋の会社に移りました。", "川崎の職場を辞めて"),
    ],
)
def test_known_limit_mistaken_replace_with_a_stated_new_value(text, old_value_evidence):
    """保証できない範囲を明示するテスト。これが通るようになったら xfail を外す。"""
    updated, _, _ = apply([replace(text, "名古屋", old_value_evidence)], text)

    assert updated == STATE


def test_downgraded_replace_keeps_the_new_statement(paths, clock):
    """17. 格下げしても、新しい値・根拠・AIの判断・確認質問は保留として残る。"""
    llm = FakeLLM(["名古屋なんですね。"], [extraction(replace(NOW_NAGOYA_TEXT, "名古屋", "今は名古屋に住んでいます"))])

    outputs = run_cli(ChatSession(llm, *paths), [NOW_NAGOYA_TEXT, "exit"])

    assert load_memory_pending(paths[6]) == [NAGOYA_PENDING]
    assert load_memory_state(paths[4]) == STATE
    assert [o for o in outputs if o.startswith("[長期")] == [
        "[長期保留] user.home_location: 名古屋（現在の値「川崎」は変更していません）",
        "[長期保留の理由] AIの判断は replace でしたが、自動では反映しませんでした: "
        "old_value_evidence に既存の値「川崎」が含まれていません",
        "[長期保留の確認事項] 川崎には今も住んでいますか？（表示のみです。回答しても保留は自動では確定しません）",
    ]


def test_downgraded_replace_without_a_question_gets_a_neutral_one():
    judged = {**replace(NOW_NAGOYA_TEXT, "名古屋", None), "confirmation_question": None}

    _, changes, _ = apply([judged], NOW_NAGOYA_TEXT)

    assert changes[0]["confirmation_question"] == "以前の「川崎」は今も有効ですか？"


def test_moving_without_a_new_address_is_held_without_inventing_a_value():
    """新しい値が発言に無い「引っ越しました。」は、値なしの保留にする。"""
    text = "引っ越しました。"

    updated, changes, _ = apply([pending(text, None, confirmation_question="引っ越し先はどちらですか？")], text)

    assert updated == STATE
    assert [(c["operation"], c["proposed_value"]) for c in changes] == [("pending", None)]


def test_same_key_cannot_be_changed_twice_in_one_utterance():
    text = "川崎の家を引き払って名古屋に引っ越しました。住まいは名古屋です。"
    judgments = [
        replace(text, "名古屋", "川崎の家を引き払って"),
        coexist("住まいは名古屋です。", "home_location", "名古屋"),
    ]

    updated, changes, warnings = apply(judgments, text)

    assert operations(changes) == ["replace"]
    assert "user.home_location の候補が複数" in warnings[0]


def test_ending_expressions_are_a_short_attribute_independent_list():
    """Pythonの一覧は意味の判定ではなく、自動で置き換えるための最小限の必要条件。"""
    assert len(ENDING_EXPRESSIONS) <= 15
    assert not any("川崎" in e or "名古屋" in e for e in ENDING_EXPRESSIONS)


# --- 永続化 ---


def test_pending_is_saved_and_displayed(paths, clock):
    """18."""
    llm = FakeLLM(["名古屋なんですね。"], [extraction(pending(NAGOYA_TEXT, "名古屋"))])

    outputs = run_cli(ChatSession(llm, *paths), [NAGOYA_TEXT, "exit"])

    saved = json.loads(paths[6].read_text(encoding="utf-8"))
    assert saved == {
        "items": [
            {
                **NAGOYA_PENDING,
                "evidence": NAGOYA_TEXT,
                "decision": "pending",
                "hold_reason": None,
            }
        ]
    }
    assert "[長期保留] user.home_location: 名古屋（現在の値「川崎」は変更していません）" in outputs
    assert not any(o.startswith("[長期保留の理由]") for o in outputs)
    assert not any(o.startswith("[長期状態]") for o in outputs)


def test_pending_is_read_after_restart(paths, clock):
    """19."""
    llm = FakeLLM(["名古屋なんですね。"], [extraction(replace(NOW_NAGOYA_TEXT, "名古屋", "今は名古屋に住んでいます"))])
    run_cli(ChatSession(llm, *paths), [NOW_NAGOYA_TEXT, "exit"])

    restarted = ChatSession(FakeLLM([]), *paths)

    assert restarted.memory_pending == [NAGOYA_PENDING]
    assert restarted.memory_state == STATE
    assert restarted.messages == []


def test_duplicate_pending_is_not_saved_twice(paths, clock):
    """20. 同じ保留が繰り返し出ても、完全一致相当なら1件だけ。理由や質問の言い回しの違いは見ない。"""
    llm = FakeLLM(
        ["はい。", "はい。", "はい。"],
        [
            extraction(pending(NAGOYA_TEXT, "名古屋")),
            extraction(pending(NAGOYA_TEXT, " 名古屋 ", reason="別の理由", confirmation_question="別の質問ですか？")),
            extraction(pending(NOW_NAGOYA_TEXT, "名古屋")),
        ],
    )
    session = ChatSession(llm, *paths)

    outputs = run_cli(session, [NAGOYA_TEXT, NAGOYA_TEXT, NOW_NAGOYA_TEXT, "exit"])

    items = load_memory_pending(paths[6])
    # 違う発言は別の保留として残す
    assert [(p["id"], p["evidence"]) for p in items] == [("p001", NAGOYA_TEXT), ("p002", NOW_NAGOYA_TEXT)]
    assert len([o for o in outputs if o.startswith("[長期保留] ")]) == 2


def test_pending_ids_continue_from_saved_items():
    existing = [NAGOYA_PENDING, {**NAGOYA_PENDING, "id": "p009", "evidence": "別の発言"}]
    held = apply([pending(NAGOYA_TEXT, "名古屋")], NAGOYA_TEXT)[1]

    updated, added = add_pending(existing, held, T2)

    assert [p["id"] for p in added] == ["p010"]
    assert updated[:2] == existing


def test_pending_does_not_change_current_state_or_history(paths, clock):
    """21."""
    before = paths[4].read_bytes()
    llm = FakeLLM(["はい。"], [extraction(pending(NAGOYA_TEXT, "名古屋"))])
    session = ChatSession(llm, *paths)

    run_cli(session, [NAGOYA_TEXT, "exit"])

    assert paths[4].read_bytes() == before
    assert not paths[5].exists()
    assert session.memory_state == STATE


def test_current_and_history_keep_the_mvp8_format(paths, clock):
    """22. replace と add は MVP8 と同じ形で現在値と履歴に残る。保留は履歴に入らない。"""
    llm = FakeLLM(
        ["はい。", "はい。"],
        [
            extraction(
                replace(MOVED_TEXT, "名古屋", "川崎の家を引き払って"),
                pending(MOVED_TEXT, "名古屋", target_key="workplace"),
            ),
            extraction(coexist("実家は大阪です。", "parents_home_location", "大阪", target_key=None)),
        ],
    )

    run_cli(ChatSession(llm, *paths), [MOVED_TEXT, "実家は大阪です。", "exit"])

    assert load_memory_state(paths[4]) == [
        {"subject": "user", "key": "home_location", "value": "名古屋", "evidence": MOVED_TEXT, "updated_at": T1},
        {
            "subject": "user",
            "key": "parents_home_location",
            "value": "大阪",
            "evidence": "実家は大阪です。",
            "updated_at": T2,
        },
    ]
    assert load_memory_history(paths[5]) == [
        {
            "timestamp": T1,
            "operation": "replace",
            "subject": "user",
            "key": "home_location",
            "old_value": "川崎",
            "value": "名古屋",
            "evidence": MOVED_TEXT,
        },
        {
            "timestamp": T2,
            "operation": "add",
            "subject": "user",
            "key": "parents_home_location",
            "value": "大阪",
            "evidence": "実家は大阪です。",
        },
    ]
    assert [p["target_key"] for p in load_memory_pending(paths[6])] == ["workplace"]


def test_missing_or_empty_pending_file_loads_as_no_items(tmp_path):
    path = tmp_path / "pending.json"
    assert load_memory_pending(path) == []
    path.write_text("", encoding="utf-8")
    assert load_memory_pending(path) == []


@pytest.mark.parametrize(
    "content",
    [
        "壊れた内容",
        "[]",
        '{"items": [{"id": "p001"}]}',
        '{"items": [{"id": "p001", "subject": "user", "evidence": "x", "created_at": "t", "proposed_value": 3}]}',
        '{"items": ['
        '{"id": "p001", "subject": "user", "evidence": "x", "created_at": "t"},'
        '{"id": "p001", "subject": "user", "evidence": "y", "created_at": "t"}]}',
    ],
)
def test_broken_pending_file_is_rejected(tmp_path, content):
    path = tmp_path / "pending.json"
    path.write_text(content, encoding="utf-8")

    with pytest.raises(StructuredMemoryError, match="長期保留ファイル"):
        load_memory_pending(path)


def test_saved_pending_can_be_loaded_back(tmp_path):
    path = tmp_path / "pending.json"

    save_memory_pending(path, [NAGOYA_PENDING])

    assert load_memory_pending(path) == [NAGOYA_PENDING]


# --- 回答時の取得 ---


@pytest.fixture
def with_pending(paths):
    save_memory_pending(paths[6], [NAGOYA_PENDING])
    return paths


def test_pending_related_to_the_current_value_is_sent_with_it(with_pending):
    """23・F. 現在値がヒットしたら、その key の保留も取得して、現在値の直後に渡す。"""
    llm = FakeLLM(["名古屋に住んでいると伺っています。"], plans=[plan(["住"])])

    outputs = run_cli(ChatSession(llm, *with_pending), ["今どこに住んでいるんでしたっけ？", "exit"])

    prompt = llm.prompts[0]
    assert block(prompt, "long-term memory state") == (
        f"- user.home_location: 川崎 (evidence: {KAWASAKI_TEXT}, updated_at: {T0})"
    )
    assert block(prompt, "long-term memory pending") == (
        f"- p001 (created_at: {T1})\n"
        "  subject: user\n"
        "  target_key: home_location\n"
        "  target_value: 川崎\n"
        "  proposed_value: 名古屋\n"
        f"  evidence: {NOW_NAGOYA_TEXT}"
    )
    assert prompt.index("[/long-term memory state]") < prompt.index("[long-term memory pending]")
    assert outputs[1:5] == [
        "[検索] 住",
        "[検索結果] 1件",
        "[長期保留検索] home_location / 住",
        "[長期保留検索結果] 1件",
    ]


def test_pending_linked_by_key_is_sent_even_if_the_query_only_hits_the_current_value(with_pending):
    llm = FakeLLM(["はい。"], plans=[plan(["home_location"])])

    run_cli(ChatSession(llm, *with_pending), ["私の住まいは？", "exit"])

    assert "[long-term memory pending]" in llm.prompts[0]


def test_pending_hit_alone_is_sent_and_stops_the_retry(paths):
    save_memory_pending(paths[6], [{**NAGOYA_PENDING, "target_key": "weekday_residence", "target_value": None}])
    llm = FakeLLM(["はい。"], plans=[plan(["名古屋"]), plan(["別の語"])])

    outputs = run_cli(ChatSession(llm, *paths), ["名古屋のことを覚えてる？", "exit"])

    assert len(llm.plan_inputs) == 1
    assert "[long-term memory state]" not in llm.prompts[0]
    assert "  target_key: weekday_residence" in block(llm.prompts[0], "long-term memory pending")
    assert outputs[1:5] == ["[検索] 名古屋", "[検索結果] 0件", "[長期保留検索] 名古屋", "[長期保留検索結果] 1件"]


def test_system_prompt_does_not_let_old_current_value_decide_alone():
    """24. 関係する保留があるときは、古い現在値だけで断定しない指示が入る。"""
    system = llm_module.SYSTEM_PROMPT

    assert "[long-term memory pending]" in system
    assert "関係する保留があるときは、[long-term memory state] の値だけで断定しないでください。" in system
    assert "古い情報や重要度の低い情報として無視しないでください。" in system
    assert "発言の内容が疑わしいという意味ではありません。" in system


def test_system_prompt_separates_the_new_statement_the_old_value_and_the_unknown_relation():
    """MVP9 仕上げ A. 新しい発言・以前の値・未確認の関係を区別し、以前の値を先に強調しない。"""
    system = llm_module.SYSTEM_PROMPT

    assert "関係する保留があるときは、次の3つを区別して伝えてください。" in system
    assert "保留の内容は、ユーザー自身の発言として伝えてください（例: 「〜と伺っています」）。" in system
    assert "ユーザーが新しく述べたことそのものではありません。" in system
    assert "以前から記録されている値を確かな情報として先に強調し、新しい発言を補足のように扱うことはしないでください。" in system
    # 保留を常に優先する指示にはしない
    assert "保留の値を、確定した現在の値として扱わないでください。" in system


def test_system_prompt_forbids_inventing_circumstances_for_the_unknown_relation():
    """MVP9 仕上げ B. 未確認の関係を説明するために、発言に無い事情を補わない・例にも挙げない。"""
    system = llm_module.SYSTEM_PROMPT

    assert "発言に無い生活の事情（理由、家族、仕事、住まい方など）を推測して補わないでください。" in system
    assert "例として挙げることもしないでください。" in system


def test_system_prompt_forbids_promising_to_resolve_pending():
    """MVP9 仕上げ C. 保留を確定・更新する機能は無いので、それを約束しない。"""
    system = llm_module.SYSTEM_PROMPT

    assert "このシステムには、保留を確認して記録を確定・更新する機能はまだありません。" in system
    assert "ユーザーが答えれば記録が更新される、確定するといった約束をしないでください。" in system
    assert "未確定な関係を説明するところまでにしてください。" in system


def test_system_prompt_does_not_treat_pending_as_confirmed():
    """25. 保留を確定した現在値として扱わず、保留だけで現在値を無効にもしない指示が入る。"""
    system = llm_module.SYSTEM_PROMPT

    assert "保留の値を、確定した現在の値として扱わないでください。" in system
    assert "保留があることだけを理由に、[long-term memory state] の値を無効になったものとして扱わないでください。" in system
    assert "[retrieved memories] に保留と同じ内容があっても、それだけで確定した値として扱わないでください。" in system


def test_pending_is_not_placed_in_the_current_state_block(with_pending):
    """25. 保留の値は現在値のブロックに混ざらない。"""
    llm = FakeLLM(["はい。"], plans=[plan(["住"])])
    session = ChatSession(llm, *with_pending)

    run_cli(session, ["今どこに住んでいるんでしたっけ？", "exit"])

    assert "名古屋" not in block(llm.prompts[0], "long-term memory state")
    assert session.memory_state == STATE


def test_unrelated_question_gets_no_pending(with_pending):
    """26・G. 長期記憶が不要な質問では、保留を検索も送信もしない。"""
    llm = FakeLLM(["2です。"], plans=[NOTHING_NEEDED])

    outputs = run_cli(ChatSession(llm, *with_pending), ["1+1は？", "exit"])

    assert llm.prompts == ["[user]\n1+1は？"]
    assert not any("長期保留検索" in o for o in outputs)


def test_pending_unrelated_to_the_searched_topic_is_not_sent(with_pending):
    """26. 別の話題の検索では、ヒットせず key でもつながらない保留は送らない。"""
    llm = FakeLLM(["分かりません。"], plans=[plan(["果物"])])

    outputs = run_cli(ChatSession(llm, *with_pending), ["好きな果物は？", "exit"])

    assert "[long-term memory pending]" not in llm.prompts[0]
    assert "[長期保留検索結果] 0件" in outputs


def test_legacy_memories_current_and_pending_are_sent_together(with_pending):
    """27. 従来の長期記憶の検索と共存する。並びは現在値 → 保留 → 従来の記憶。"""
    append_memories(
        with_pending[1],
        [{"text": "ユーザーは今は名古屋に住んでいる", "origin": "user", "evidence": NOW_NAGOYA_TEXT}],
    )
    llm = FakeLLM(["はい。"], plans=[plan(["住"])])

    outputs = run_cli(ChatSession(llm, *with_pending), ["今どこに住んでいるんでしたっけ？", "exit"])

    prompt = llm.prompts[0]
    assert (
        prompt.index("[long-term memory state]")
        < prompt.index("[long-term memory pending]")
        < prompt.index("[retrieved memories]")
        < prompt.index("[user]")
    )
    # 従来の記憶と現在値の件数は、MVP8 と同じく [検索結果] に合わせて表示する
    assert "[検索結果] 2件" in outputs


def test_working_memory_still_works_alongside_pending(with_pending, clock):
    """28. 作業記憶は常に先頭に渡り、同じターンの作業記憶の抽出も保留の保存と一緒に動く。"""
    save_working_memory(
        with_pending[2],
        {"mission": [], "scope": [{"text": "商用環境", "evidence": "商用環境"}], "acceptance_criteria": []},
    )
    text = "今回は検証環境も対象にします。ちなみに名古屋に住んでいます。"
    llm = FakeLLM(
        ["承知しました。"],
        [extraction(pending("名古屋に住んでいます。", "名古屋"))],
        plans=[plan(["住"])],
        working=[
            json.dumps(
                [{"operation": "add", "field": "scope", "text": "検証環境", "evidence": "検証環境も対象にします"}],
                ensure_ascii=False,
            )
        ],
    )

    outputs = run_cli(ChatSession(llm, *with_pending), [text, "exit"])

    assert llm.prompts[0].startswith("[working memory]\nScope:\n- 商用環境\n[/working memory]\n\n")
    assert "[作業記憶] scope: 検証環境" in outputs
    assert [i["text"] for i in load_working_memory(with_pending[2])["scope"]] == ["商用環境", "検証環境"]
    assert [p["id"] for p in load_memory_pending(with_pending[6])] == ["p001", "p002"]


def test_pending_search_matches_values_keys_and_evidence():
    items = [NAGOYA_PENDING, {**NAGOYA_PENDING, "id": "p002", "proposed_value": "大阪", "evidence": "実家は大阪です。"}]

    assert search_memory_pending(items, ["名古屋"]) == [NAGOYA_PENDING]
    assert search_memory_pending(items, ["home_location"]) == items
    assert search_memory_pending(items, ["川崎"]) == items  # 保留した時点の値でもヒットする
    assert search_memory_pending(items, ["実家"]) == [items[1]]
    assert search_memory_pending(items, ["unresolved", "p001"]) == []  # 状態と id は検索しない


def test_related_pending_prefers_linked_items_and_is_capped():
    linked = [{**NAGOYA_PENDING, "id": f"p{n:03d}", "evidence": f"発言{n}"} for n in range(1, 8)]
    other = {**NAGOYA_PENDING, "id": "p100", "target_key": "favorite_fruit", "evidence": "別"}

    chosen = related_memory_pending([other] + linked, STATE, found=[other])

    assert len(chosen) == MAX_RESULTS
    assert other not in chosen
    # 上限を超えたら新しく保存された保留を残し、保存順に並べる
    assert [p["id"] for p in chosen] == ["p003", "p004", "p005", "p006", "p007"]


def test_pending_block_format_omits_missing_values():
    item = {**NAGOYA_PENDING, "proposed_value": None, "evidence": "引っ越しました。"}

    assert format_memory_pending([item]) == (
        "[long-term memory pending]\n"
        f"- p001 (created_at: {T1})\n"
        "  subject: user\n"
        "  target_key: home_location\n"
        "  target_value: 川崎\n"
        "  evidence: 引っ越しました。\n"
        "[/long-term memory pending]"
    )
    assert format_memory_pending([]) == ""


# --- JSON の解析 ---


GOOD = extraction(pending(NAGOYA_TEXT, "名古屋"))


def test_plain_json_is_parsed_without_recovery():
    """29."""
    assert parse_extraction(GOOD) == ([pending(NAGOYA_TEXT, "名古屋")], False)
    assert parse_extraction(f"```json\n{GOOD}\n```") == ([pending(NAGOYA_TEXT, "名古屋")], False)


@pytest.mark.parametrize(
    "raw",
    [
        f"<reasoning>名古屋とだけ述べている。川崎の終了は不明。</reasoning>\n\n{GOOD}",
        # 31. 閉じていないreasoningタグ
        f"<reasoning>The utterance mentions 名古屋 only. pending.\n{GOOD}",
        f"<thinking_mode>[long-term memory state] を見ると川崎。{{判断}}は pending。\n{GOOD}\n",
    ],
)
def test_json_after_a_preamble_is_recovered(raw):
    """30・31. JSONの前に推論文があっても、末尾にある唯一のJSONなら取り出す。"""
    assert parse_extraction(raw) == ([pending(NAGOYA_TEXT, "名古屋")], True)


@pytest.mark.parametrize(
    "raw, message",
    [
        # 32. 下書きのJSONと最終的なJSONがある
        (f'<reasoning>{json.dumps(replace(NAGOYA_TEXT, "名古屋", None), ensure_ascii=False)}</reasoning>\n{GOOD}', "複数"),
        (f"<reasoning>候補が無ければ [] を返す。</reasoning>\n{GOOD}", "複数"),
        # JSONの後ろに説明が続く
        (f"<reasoning>考えます。</reasoning>\n{GOOD}\n以上です。", "後ろに別の出力"),
        # 34. 解析できない
        ("<reasoning>判断できません。", "見つかりません"),
        ("<reasoning>途中で切れた</reasoning>\n[{\"decision\": \"pending\", ", "見つかりません"),
    ],
)
def test_ambiguous_or_broken_output_is_not_recovered(raw, message):
    """32・34. JSONの候補が複数ある・末尾に無い・完結していない場合は、選ばず・直さずに失敗にする。"""
    with pytest.raises(StructuredMemoryError, match=message):
        parse_extraction(raw)


def test_truncated_array_does_not_recover_an_inner_object():
    """配列が途中で切れていても、中の要素だけを取り出して使わない。"""
    first = json.dumps(pending(NAGOYA_TEXT, "名古屋"), ensure_ascii=False)
    raw = f"<reasoning>x</reasoning>\n[{first}, {{\"decision\": \"pen"

    with pytest.raises(StructuredMemoryError):
        parse_extraction(raw)


def test_recovered_json_still_goes_through_all_checks(paths, clock):
    """33. 取り出したJSONでも、evidence の原文一致などの検証と replace の条件を省かない。"""
    raw = "<reasoning>考えます。</reasoning>\n" + extraction(
        pending("名古屋に住んでいる", "名古屋"),
        replace(NAGOYA_TEXT, "名古屋", NAGOYA_TEXT),
    )
    llm = FakeLLM(["はい。"], [raw])

    outputs = run_cli(ChatSession(llm, *paths), [NAGOYA_TEXT, "exit"])

    warnings = [o for o in outputs if o.startswith("[警告]")]
    assert warnings[0] == "[警告] 長期状態の抽出結果にJSON以外の前置きがあったため、末尾のJSONだけを解析しました"
    assert "evidence が元の発言に含まれていません" in warnings[1]
    # replace は条件を満たさないので保留になり、現在値は変わらない
    assert load_memory_state(paths[4]) == STATE
    assert [p["decision"] for p in load_memory_pending(paths[6])] == ["replace"]


def test_unrecoverable_output_changes_nothing(paths):
    """34."""
    llm = FakeLLM(["はい。"], [f"<reasoning>迷う</reasoning>\n{GOOD}\n以上です。"])

    outputs = run_cli(ChatSession(llm, *paths), [NAGOYA_TEXT, "exit"])

    assert any(o.startswith("[警告] 長期状態を抽出できませんでした") for o in outputs)
    assert load_memory_state(paths[4]) == STATE
    assert not paths[6].exists()


# --- AIへの指示 ---


def test_relation_prompt_explains_the_decisions_and_fields():
    prompt = llm_module.STRUCTURED_MEMORY_SYSTEM_PROMPT

    for word in (
        "replace",
        "coexist",
        "pending",
        "no_change",
        "target_key",
        "proposed_key",
        "proposed_value",
        "old_value_status",
        "old_value_evidence",
        "temporal_scope",
        "confirmation_question",
    ):
        assert word in prompt
    assert "新しい情報が述べられたというだけで replace を選ばないでください。" in prompt
    assert "曖昧さがあるというだけで pending を選ばないでください。" in prompt
    # 事前実験で使った地名・期待結果を例として載せない
    for place in ("川崎", "名古屋", "横浜", "単身赴任", "home_location"):
        assert place not in prompt


def test_planner_is_told_that_pending_is_searched_with_the_same_queries():
    assert "保留" in llm_module.SEARCH_PLAN_SYSTEM_PROMPT


# --- 起動 ---


class FakeStdin(io.StringIO):
    def reconfigure(self, **kwargs):
        pass


@pytest.fixture
def main_env(tmp_path, monkeypatch):
    names = {
        "ACM_HISTORY_FILE": "conversation.jsonl",
        "ACM_MEMORY_FILE": "memories.jsonl",
        "ACM_WORKING_MEMORY_FILE": "working_memory.json",
        "ACM_WORKING_MEMORY_HISTORY_FILE": "working_memory_history.jsonl",
        "ACM_LONG_TERM_MEMORY_STATE_FILE": "state.json",
        "ACM_LONG_TERM_MEMORY_HISTORY_FILE": "state_history.jsonl",
        "ACM_LONG_TERM_MEMORY_PENDING_FILE": "pending.json",
    }
    for name, filename in names.items():
        monkeypatch.setenv(name, str(tmp_path / filename))
    monkeypatch.setattr(cli_module, "ClaudeCLI", lambda: FakeLLM([]))
    monkeypatch.setattr(cli_module.sys, "stdin", FakeStdin("exit\n"))
    monkeypatch.setattr(cli_module.sys.stdout, "reconfigure", lambda **kwargs: None, raising=False)
    return tmp_path / "pending.json"


def test_main_reports_loaded_pending(main_env, capsys):
    """19. 再起動時に保留を読み込んだことを表示する。"""
    save_memory_pending(main_env, [NAGOYA_PENDING])

    assert cli_module.main() == 0
    assert "長期保留を読み込みました（1件）" in capsys.readouterr().out


def test_main_starts_without_a_pending_file(main_env, capsys):
    assert cli_module.main() == 0
    out = capsys.readouterr().out
    assert "チャットを開始します" in out
    assert "長期保留" not in out


def test_main_exits_with_error_on_broken_pending_file(main_env, capsys):
    main_env.write_text("壊れた内容", encoding="utf-8")

    assert cli_module.main() == 1
    out = capsys.readouterr().out
    assert "[エラー] 長期保留ファイル" in out
    assert "チャットを開始します" not in out
