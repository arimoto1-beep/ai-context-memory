"""MVP15: 構造化長期記憶の抽出AIへ渡す現在値の範囲（全量 / key の一覧と一部の値 / 一部の値）と、新しい key の自動追加の安全策。

AIの判断（抽出AIの出力）だけを差し替え、Python が AIへ見せる範囲・検証・保存をどう行うかを確かめる。
検証は、AIへ見せた範囲によらず現在値の全量に対して行われる。
"""

import json

import pytest

from ai_context_memory import llm as llm_module
from ai_context_memory.chat import ChatSession
from ai_context_memory.llm import (
    STRUCTURED_MEMORY_SYSTEM_PROMPT,
    STRUCTURED_MEMORY_VIEW_NOTICE,
    ClaudeCLI,
    format_structured_memory_request,
    format_structured_memory_view_request,
)
from ai_context_memory.structured_memory import (
    COVERAGE_COMPLETE,
    COVERAGE_PARTIAL,
    COVERAGE_UNAVAILABLE,
    EXTRACTION_FULL_MAX_ATTRIBUTES,
    EXTRACTION_KEY_LIST_MAX_ATTRIBUTES,
    EXTRACTION_MAX_VALUE_ATTRIBUTES,
    VIEW_FULL,
    VIEW_KEYS,
    VIEW_SELECTED,
    extraction_view,
    load_memory_history,
    load_memory_pending,
    load_memory_state,
    save_memory_pending,
    save_memory_state,
)
from test_llm import completed, fake_run  # noqa: F401  （fake_run は pytest のフィクスチャ）
from test_memory_confirmation import KAWASAKI, P001, A_COMPANY, judge, run_cli
from test_memory_pending import judgment, pending, replace

T0 = "2026-10-01T09:00:00+09:00"
HOBBY = {"subject": "user", "key": "hobby", "value": "釣り", "evidence": "趣味は釣りです。", "updated_at": T0}
NOW_NAGOYA = "今は名古屋に住んでいます。"
MOVED = "川崎から名古屋に引っ越しました。"
BOTH_MOVED = "川崎から名古屋に引っ越して、A社を辞めてB社に転職しました。"


def filler(count):
    """関係しない属性 count 個（値は互いに部分一致しない）。"""
    return [
        {"subject": "user", "key": f"topic{i:03d}_note", "value": f"メモ{i:03d}番", "evidence": f"メモ{i:03d}番です。", "updated_at": T0}
        for i in range(count)
    ]


def state_of(total, *items):
    """items と、合わせて total 属性になるだけの関係しない属性。"""
    return list(items) + filler(total - len(items))


KEYS_SIZE = EXTRACTION_FULL_MAX_ATTRIBUTES + 50  # key の一覧と一部の値（VIEW_KEYS）になる属性数
OVER_SIZE = EXTRACTION_KEY_LIST_MAX_ATTRIBUTES + 1  # key の一覧を渡さない（VIEW_SELECTED）属性数


def coexist_new(evidence, key, value, **fields):
    return judgment("coexist", evidence, proposed_key=key, proposed_value=value, **fields)


def extraction(*items):
    return json.dumps(list(items), ensure_ascii=False)


def plan(queries=(), attributes=()):
    return json.dumps(
        {
            "needs_memory": bool(queries or attributes),
            "queries": list(queries),
            "attributes": list(attributes),
            "needs_long_term_memory_history": False,
            "memory_history_queries": [],
            "needs_working_memory_history": False,
            "history_queries": [],
        },
        ensure_ascii=False,
    )


class ViewLLM:
    """抽出AIへの入力を、本番と同じ整形関数で記録する。ほかの呼び出しは何もしない応答を返す。"""

    def __init__(self, structured=(), plans=(), judgments=()):
        self.structured = list(structured)
        self.plans = list(plans)
        self.judgments = list(judgments)
        self.structured_inputs = []  # (system_prompt, prompt, view)

    def complete(self, messages, *context):
        return "はい。"

    def plan_search(self, user_text, previous_queries=(), working_memory=None, attribute_list=None):
        return self.plans.pop(0) if self.plans else plan()

    def extract_memories(self, user_text):
        return "[]"

    def extract_structured_memory(self, user_text, items, view=None):
        if view is None or view["mode"] == VIEW_FULL:
            self.structured_inputs.append(
                (STRUCTURED_MEMORY_SYSTEM_PROMPT, format_structured_memory_request(user_text, items), view)
            )
        else:
            self.structured_inputs.append(
                (
                    STRUCTURED_MEMORY_SYSTEM_PROMPT + STRUCTURED_MEMORY_VIEW_NOTICE,
                    format_structured_memory_view_request(user_text, view),
                    view,
                )
            )
        return self.structured.pop(0) if self.structured else "[]"

    def extract_working_memory(self, user_text, working_memory):
        return "[]"

    def judge_confirmation(self, pending_item, current, question, answer):
        return self.judgments.pop(0)


def make_files(tmp_path, state, pending_items=()):
    data = tmp_path / "data"
    files = [
        data / "conversation.jsonl",
        data / "memories.jsonl",
        data / "working_memory.json",
        data / "working_memory_history.jsonl",
        data / "long_term_memory_state.json",
        data / "long_term_memory_history.jsonl",
        data / "long_term_memory_pending.json",
        data / "long_term_memory_confirmations.jsonl",
        data / "long_term_memory_reverts.jsonl",
    ]
    save_memory_state(files[4], list(state))
    save_memory_pending(files[6], list(pending_items))
    return files


@pytest.fixture
def clock(monkeypatch):
    minutes = iter(range(1, 60))
    monkeypatch.setattr(
        "ai_context_memory.chat.timestamp_now", lambda: f"2026-10-20T10:{next(minutes):02d}:00+09:00"
    )


def turn(tmp_path, state, text, structured=(), plans=(), pending_items=()):
    files = make_files(tmp_path, state, pending_items)
    llm = ViewLLM(structured, plans)
    session = ChatSession(llm, *files)
    outputs = run_cli(session, [text])
    return files, llm, session, outputs


def stored(files):
    return load_memory_state(files[4])


def stored_pending(files):
    return load_memory_pending(files[6])


def block(prompt, name):
    assert f"[{name}]" in prompt
    return prompt.split(f"[{name}]\n")[1].split(f"\n[/{name}]")[0]


# --- 退行の防止: 安全策は自動の反映を減らすだけで、増やさない ---

CANDIDATE_SETS = [
    [replace(MOVED, "名古屋", "川崎から名古屋に引っ越しました")],  # 検証を通る replace
    [replace(NOW_NAGOYA, "名古屋", "今は")],  # 検証を通らない replace
    [coexist_new(NOW_NAGOYA, "residence", "名古屋")],  # 新しい key
    [coexist_new(NOW_NAGOYA, "home_location", "名古屋")],  # 既存の key との衝突
    [pending(NOW_NAGOYA, "名古屋")],
    [judgment("no_change", NOW_NAGOYA)],
    [replace(BOTH_MOVED, "名古屋", "川崎から名古屋に引っ越して"), coexist_new(BOTH_MOVED, "new_company", "B社")],
]


@pytest.mark.parametrize("candidates", CANDIDATE_SETS)
@pytest.mark.parametrize("holds", [{"new_key_hold": "一覧なし"}, {"write_hold": "読めない"}, {"new_key_hold": "x", "write_hold": "y"}])
def test_holds_never_apply_anything_that_would_otherwise_be_held(candidates, holds):
    from ai_context_memory.structured_memory import apply_candidates

    text = candidates[0]["evidence"]
    _, before, _ = apply_candidates([KAWASAKI, A_COMPANY], candidates, text, T0)
    _, after, _ = apply_candidates([KAWASAKI, A_COMPANY], candidates, text, T0, **holds)

    def applied(changes):
        return [c for c in changes if c["operation"] != "pending"]

    assert all(c in applied(before) for c in applied(after))
    # 保留だったものは保留のまま（減らない）
    assert len([c for c in after if c["operation"] == "pending"]) >= len([c for c in before if c["operation"] == "pending"])
    assert len(after) == len(before)


def test_values_in_the_utterance_are_matched_like_the_replace_check():
    """置き換えの検証（replace_hold_reason）と同じく、大文字小文字・全角半角の違いをそろえて照合する。"""
    from ai_context_memory.structured_memory import attributes_in_text

    company = {**A_COMPANY, "value": "ABC社"}
    assert attributes_in_text([company, KAWASAKI], "ａｂｃ社を辞めました。") == [("user", "employer")]


# --- 範囲の決め方 ---


def test_view_thresholds_are_separate_for_full_values_and_the_key_list():
    """全量は EXTRACTION_FULL_MAX_ATTRIBUTES まで、key の一覧は EXTRACTION_KEY_LIST_MAX_ATTRIBUTES まで。番号付きの key は1属性。"""
    assert EXTRACTION_FULL_MAX_ATTRIBUTES < EXTRACTION_KEY_LIST_MAX_ATTRIBUTES
    cases = {
        EXTRACTION_FULL_MAX_ATTRIBUTES: (VIEW_FULL, COVERAGE_COMPLETE),
        EXTRACTION_FULL_MAX_ATTRIBUTES + 1: (VIEW_KEYS, COVERAGE_COMPLETE),
        EXTRACTION_KEY_LIST_MAX_ATTRIBUTES: (VIEW_KEYS, COVERAGE_COMPLETE),
        EXTRACTION_KEY_LIST_MAX_ATTRIBUTES + 1: (VIEW_SELECTED, COVERAGE_PARTIAL),
    }
    for count, expected in cases.items():
        view = extraction_view(filler(count), "こんにちは")
        assert (view["mode"], view["key_coverage"]) == expected, count
    numbered = filler(EXTRACTION_FULL_MAX_ATTRIBUTES) + [
        {**HOBBY, "key": "topic000_note_2", "value": "別の値"},
    ]
    assert extraction_view(numbered, "こんにちは")["mode"] == VIEW_FULL


def test_store_problem_makes_the_coverage_unavailable_in_every_mode():
    for count in (3, KEYS_SIZE, OVER_SIZE):
        view = extraction_view(filler(count), "こんにちは", store_problem="読み込めません")
        assert view["key_coverage"] == COVERAGE_UNAVAILABLE
        assert view["coverage_reason"] == "読み込めません"


def test_value_attributes_are_capped_but_values_in_the_utterance_are_always_shown():
    items = state_of(KEYS_SIZE, KAWASAKI)
    selected = [("user", f"topic{i:03d}_note") for i in range(EXTRACTION_MAX_VALUE_ATTRIBUTES + 5)]
    view = extraction_view(items, MOVED, selected=selected)
    assert view["value_attributes"][0] == ("user", "home_location")
    assert len(view["value_attributes"]) == EXTRACTION_MAX_VALUE_ATTRIBUTES
    assert view["omitted_value_attributes"] == 6
    # 発言に値が現れる属性は、上限を超えても渡す
    many = [{**KAWASAKI, "key": f"place{i:02d}_name", "value": f"地名{i:02d}"} for i in range(20)]
    text = "、".join(f"地名{i:02d}" for i in range(20))
    view = extraction_view(many + filler(KEYS_SIZE), text)
    assert len(view["value_attributes"]) == 20


# --- Current が空・小規模（従来どおり） ---


def test_empty_current_adds_a_new_key(tmp_path, clock):
    text = "好きな映画はローマの休日です。"
    files, llm, session, outputs = turn(tmp_path, [], text, [extraction(coexist_new(text, "favorite_movie", "ローマの休日"))])

    assert [(i["key"], i["value"]) for i in stored(files)] == [("favorite_movie", "ローマの休日")]
    assert session.last_extraction_view["mode"] == VIEW_FULL
    assert session.last_extraction_view["key_coverage"] == COVERAGE_COMPLETE
    assert not any(o.startswith("[長期状態の入力]") for o in outputs)


def test_small_memory_sends_exactly_the_same_input_as_before(tmp_path, clock, fake_run):
    """全量の範囲では、抽出AIの指示と入力は MVP14 と1文字も変わらない（100属性ちょうどまで）。"""
    fake_run.result = completed(result="[]")
    items = state_of(EXTRACTION_FULL_MAX_ATTRIBUTES, KAWASAKI, A_COMPANY, HOBBY)
    files = make_files(tmp_path, items)
    session = ChatSession(ClaudeCLI(), *files)

    session.update_memory_state(NOW_NAGOYA)

    command = fake_run.command
    assert command[command.index("--system-prompt") + 1] == STRUCTURED_MEMORY_SYSTEM_PROMPT
    assert fake_run.kwargs["input"] == format_structured_memory_request(NOW_NAGOYA, items)


def test_small_memory_coexist_still_adds_a_new_key(tmp_path, clock):
    text = "副業で翻訳もしています。"
    files, *_ = turn(
        tmp_path, [A_COMPANY], text, [extraction(coexist_new(text, "side_job", "翻訳", target_key="employer"))]
    )
    assert [(i["key"], i["value"]) for i in stored(files)] == [("employer", "A社"), ("side_job", "翻訳")]


# --- key の一覧と一部の値（VIEW_KEYS） ---


def test_keys_view_lists_every_attribute_and_only_the_values_found(tmp_path, clock):
    """関係する属性が見つかる（プランナーが選ぶ）と、その値だけが渡り、key はすべて渡る。"""
    items = state_of(KEYS_SIZE, KAWASAKI, A_COMPANY)
    files, llm, session, outputs = turn(
        tmp_path, items, "私はどこに住んでいますか？", plans=[plan(["住"], ["user.home_location"])]
    )

    system, prompt, view = llm.structured_inputs[0]
    assert system == STRUCTURED_MEMORY_SYSTEM_PROMPT + STRUCTURED_MEMORY_VIEW_NOTICE
    assert view["mode"] == VIEW_KEYS and view["key_coverage"] == COVERAGE_COMPLETE
    listed = block(prompt, "stored attributes").splitlines()
    assert len(listed) == KEYS_SIZE
    assert "- user.employer" in listed and f"- user.topic{KEYS_SIZE - 3:03d}_note" in listed
    assert block(prompt, "long-term memory state") == "- user.home_location: 川崎"
    assert "A社" not in prompt and "メモ000番" not in prompt
    assert block(prompt, "extraction view").splitlines() == [
        f"attributes stored: {KEYS_SIZE}",
        f"attribute list: complete (all {KEYS_SIZE} attributes are listed in [stored attributes])",
        f"values shown: 1 of {KEYS_SIZE} attributes",
    ]
    assert f"[長期状態の入力] 抽出に渡した範囲: 全{KEYS_SIZE}属性の key の一覧と、1属性の値" in outputs


def test_explicit_replace_shows_the_old_value_and_replaces(tmp_path, clock):
    """既存の値が発言にある属性は、検索で見つからなくても値を渡す。置き換えは全量の現在値で検証する。"""
    items = state_of(KEYS_SIZE, KAWASAKI)
    files, llm, session, _ = turn(tmp_path, items, MOVED, [extraction(replace(MOVED, "名古屋", "川崎から名古屋に引っ越しました"))])

    _, prompt, _ = llm.structured_inputs[0]
    assert block(prompt, "long-term memory state") == "- user.home_location: 川崎"
    after = stored(files)
    assert after[0]["value"] == "名古屋" and len(after) == KEYS_SIZE
    assert [e["operation"] for e in load_memory_history(files[5])] == ["replace"]


def test_missed_attribute_in_keys_view_cannot_be_overwritten_or_duplicated_by_key(tmp_path, clock):
    """検索で属性を取りこぼしても（値は渡らない）、key は一覧にあり、検証は全量で行う。

    旧値の終了を確かめられない replace は保留、既存の key と同じ key での coexist も保留になる（MVP9 と同じ）。
    """
    items = state_of(KEYS_SIZE, KAWASAKI)
    files, llm, session, _ = turn(
        tmp_path,
        items,
        NOW_NAGOYA,
        [extraction(replace(NOW_NAGOYA, "名古屋", "今は"), coexist_new(NOW_NAGOYA, "home_location", "名古屋"))],
    )

    _, prompt, view = llm.structured_inputs[0]
    assert view["value_attributes"] == []
    assert "- user.home_location" in block(prompt, "stored attributes").splitlines()
    assert stored(files) == items
    held = stored_pending(files)
    assert [(p["target_key"], p["proposed_key"], p["proposed_value"]) for p in held] == [
        ("home_location", None, "名古屋"),
        ("home_location", "home_location", "名古屋"),
    ]


def test_same_key_value_change_without_an_explicit_end_is_held(tmp_path, clock):
    items = state_of(KEYS_SIZE, KAWASAKI)
    files, *_ = turn(tmp_path, items, NOW_NAGOYA, [extraction(pending(NOW_NAGOYA, "名古屋"))])
    assert stored(files) == items
    assert stored_pending(files)[0]["target_value"] == "川崎"


def test_coexist_with_a_new_key_is_added_when_the_key_list_is_complete(tmp_path, clock):
    text = "副業で翻訳もしています。"
    items = state_of(KEYS_SIZE, A_COMPANY)
    files, *_ = turn(tmp_path, items, text, [extraction(coexist_new(text, "side_job", "翻訳", target_key="employer"))])
    assert stored(files)[-1]["key"] == "side_job"
    assert stored_pending(files) == []


def test_unresolved_pending_attributes_get_their_values_shown(tmp_path, clock):
    items = state_of(KEYS_SIZE, KAWASAKI, HOBBY)
    files, llm, *_ = turn(tmp_path, items, "最近は登山も始めました。", pending_items=[P001])
    _, prompt, view = llm.structured_inputs[0]
    assert ("user", "home_location") in view["value_attributes"]
    assert "- user.home_location: 川崎" in block(prompt, "long-term memory state").splitlines()


def test_multi_attribute_statement_replaces_both_when_both_old_values_are_stated(tmp_path, clock):
    items = state_of(KEYS_SIZE, KAWASAKI, A_COMPANY)
    output = extraction(
        replace(BOTH_MOVED, "名古屋", "川崎から名古屋に引っ越して"),
        replace(BOTH_MOVED, "B社", "A社を辞めてB社に転職しました", target_key="employer"),
    )
    files, llm, *_ = turn(tmp_path, items, BOTH_MOVED, [output])

    _, prompt, _ = llm.structured_inputs[0]
    assert block(prompt, "long-term memory state").splitlines() == ["- user.home_location: 川崎", "- user.employer: A社"]
    assert [(i["key"], i["value"]) for i in stored(files)[:2]] == [("home_location", "名古屋"), ("employer", "B社")]


def test_multi_attribute_statement_without_old_values_is_held(tmp_path, clock):
    text = "名古屋に引っ越して、勤め先もB社に変わりました。"
    items = state_of(KEYS_SIZE, KAWASAKI, A_COMPANY)
    output = extraction(
        replace(text, "名古屋", "名古屋に引っ越して"),
        replace(text, "B社", "勤め先もB社に変わりました", target_key="employer"),
    )
    files, *_ = turn(tmp_path, items, text, [output])
    assert stored(files) == items
    assert [p["target_key"] for p in stored_pending(files)] == ["home_location", "employer"]


# --- key の一覧を渡さない（VIEW_SELECTED）: 取りこぼした属性の重複を確定させない ---


def test_missed_attribute_over_the_key_limit_is_held_instead_of_added(tmp_path, clock):
    """関係する属性（home_location）を検索で取りこぼし、AIが同じ意味の別の key（residence）を作っても、追加しない。"""
    items = state_of(OVER_SIZE, KAWASAKI)
    files, llm, session, outputs = turn(
        tmp_path, items, NOW_NAGOYA, [extraction(coexist_new(NOW_NAGOYA, "residence", "名古屋"))]
    )

    _, prompt, view = llm.structured_inputs[0]
    assert view["mode"] == VIEW_SELECTED and view["key_coverage"] == COVERAGE_PARTIAL
    assert "[stored attributes]" not in prompt
    assert f"attribute list: not provided ({OVER_SIZE} attributes, more than the limit for listing)" in prompt
    assert stored(files) == items  # residence は追加されない
    held = stored_pending(files)
    assert [(p["proposed_key"], p["proposed_value"], p["target_key"], p["evidence"]) for p in held] == [
        ("residence", "名古屋", None, NOW_NAGOYA)
    ]
    assert "既存の属性の一覧をすべては確認できていない" in held[0]["hold_reason"]
    assert any(o.startswith("[長期保留] user.residence: 名古屋") for o in outputs)
    assert any("新しい属性は自動では追加しません" in o for o in outputs)


@pytest.mark.parametrize(
    "total, added",
    [
        (EXTRACTION_FULL_MAX_ATTRIBUTES, True),
        (EXTRACTION_FULL_MAX_ATTRIBUTES + 1, True),
        (EXTRACTION_KEY_LIST_MAX_ATTRIBUTES, True),
        (EXTRACTION_KEY_LIST_MAX_ATTRIBUTES + 1, False),
    ],
)
def test_new_key_at_the_boundaries_is_added_only_while_the_key_list_is_complete(tmp_path, clock, total, added):
    """100→101 属性（全量→一覧）では新しい key の追加は変わらず、300→301 属性（一覧→一覧なし）で保留に変わる。"""
    items = state_of(total, HOBBY)
    statement = "好きな映画はローマの休日です。"
    files, *_ = turn(tmp_path, items, statement, [extraction(coexist_new(statement, "favorite_movie", "ローマの休日"))])

    keys = [i["key"] for i in stored(files)]
    assert ("favorite_movie" in keys) is added
    assert len(stored_pending(files)) == (0 if added else 1)


def test_replace_over_the_key_limit_is_still_validated_against_all_current_values(tmp_path, clock):
    items = state_of(OVER_SIZE, KAWASAKI, A_COMPANY)
    output = extraction(
        replace(BOTH_MOVED, "名古屋", "川崎から名古屋に引っ越して"),
        replace(BOTH_MOVED, "B社", "A社を辞めてB社に転職しました", target_key="employer"),
    )
    files, llm, *_ = turn(tmp_path, items, BOTH_MOVED, [output])
    _, prompt, _ = llm.structured_inputs[0]
    assert block(prompt, "long-term memory state").splitlines() == ["- user.home_location: 川崎", "- user.employer: A社"]
    assert [(i["key"], i["value"]) for i in stored(files)[:2]] == [("home_location", "名古屋"), ("employer", "B社")]


def test_known_limit_a_synonym_key_is_added_when_the_key_list_is_complete(tmp_path, clock):
    """限界（解決していない）: key の一覧が完全でも、AIが同じ意味の別の key を作れば、Python は意味の重複を見分けられない。"""
    files, *_ = turn(tmp_path, [KAWASAKI], NOW_NAGOYA, [extraction(coexist_new(NOW_NAGOYA, "residence", "名古屋"))])
    assert [i["key"] for i in stored(files)] == ["home_location", "residence"]


# --- 保存されている現在値を確かめられない ---


def test_unreadable_store_holds_new_keys_and_replacements_and_is_not_overwritten(tmp_path, clock):
    files = make_files(tmp_path, [KAWASAKI])
    llm = ViewLLM([extraction(replace(MOVED, "名古屋", "川崎から名古屋に引っ越しました"), coexist_new(MOVED, "moving_reason", "名古屋"))])
    session = ChatSession(llm, *files)
    files[4].write_text("{broken", encoding="utf-8")

    outputs = run_cli(session, [MOVED])

    assert session.last_extraction_view["key_coverage"] == COVERAGE_UNAVAILABLE
    assert files[4].read_text(encoding="utf-8") == "{broken"
    assert session.memory_state == [KAWASAKI]
    held = stored_pending(files)
    assert [(p["target_key"], p["proposed_key"]) for p in held] == [("home_location", None), (None, "moving_reason")]
    assert "保存されている長期状態を確かめられない" in held[0]["hold_reason"]
    assert any("保存されている長期状態を読み込めませんでした" in o for o in outputs)


def test_store_changed_outside_the_session_is_not_overwritten(tmp_path, clock):
    files = make_files(tmp_path, [KAWASAKI])
    llm = ViewLLM([extraction(replace(MOVED, "名古屋", "川崎から名古屋に引っ越しました"))])
    session = ChatSession(llm, *files)
    save_memory_state(files[4], [KAWASAKI, A_COMPANY])

    run_cli(session, [MOVED])

    assert load_memory_state(files[4]) == [KAWASAKI, A_COMPANY]
    assert stored_pending(files)[0]["target_key"] == "home_location"
    assert "このセッションの外で変更されています" in stored_pending(files)[0]["hold_reason"]


# --- 保留した新しい key のその後、確認・取り消し ---


def test_held_new_key_cannot_be_confirmed_and_stays_pending(tmp_path, clock):
    """target_key の無い保留は、MVP10 からの規則で /confirm できない。状態は変えず、保留も消さない。"""
    items = state_of(OVER_SIZE, KAWASAKI)
    files = make_files(tmp_path, items)
    llm = ViewLLM([extraction(coexist_new(NOW_NAGOYA, "residence", "名古屋"))])
    session = ChatSession(llm, *files)

    outputs = run_cli(session, [NOW_NAGOYA, "/pending", "/confirm p001"])

    assert any("確認できません: 既存の項目（target_key）に関係しない保留" in o for o in outputs)
    assert any(o.startswith("[確認] p001 は確認できません") for o in outputs)
    assert stored(files) == items
    assert stored_pending(files)[0]["status"] == "unresolved"


def test_unresolvable_held_new_key_is_shown_as_such_and_survives_a_restart(tmp_path, clock):
    """保留を作ったときに /confirm で確定できないことを示し（確認できるとは案内しない）、再起動後も /pending で見える。"""
    items = state_of(OVER_SIZE, KAWASAKI)
    files = make_files(tmp_path, items)
    question = "今の住まいは名古屋ですか？"
    llm = ViewLLM([extraction(coexist_new(NOW_NAGOYA, "residence", "名古屋", confirmation_question=question))])

    outputs = run_cli(ChatSession(llm, *files), [NOW_NAGOYA])

    assert f"[長期保留の確認事項] {question}" in outputs
    assert not any("/confirm p001 で確認できます" in o for o in outputs)
    assert any(o.startswith("[長期保留] p001 は既存の項目に関係しないため、/confirm では確定できません") for o in outputs)
    [held] = stored_pending(files)
    assert (held["evidence"], held["proposed_key"], held["proposed_value"]) == (NOW_NAGOYA, "residence", "名古屋")
    assert "既存の属性の一覧をすべては確認できていない" in held["hold_reason"]

    restarted = run_cli(ChatSession(ViewLLM(), *files), ["/pending"])

    assert any(o.startswith("[長期保留] p001 user.residence: 名古屋") and NOW_NAGOYA in o for o in restarted)
    assert any("確認できません: 既存の項目（target_key）に関係しない保留" in o for o in restarted)
    assert stored(files) == items


def test_confirm_and_revert_work_unchanged_in_the_keys_view(tmp_path, clock):
    items = state_of(KEYS_SIZE, KAWASAKI)
    files = make_files(tmp_path, items, [P001])
    llm = ViewLLM(judgments=[judge("replace", "もう住んでいません")])
    session = ChatSession(llm, *files)

    run_cli(session, ["/confirm p001", "いいえ、もう住んでいません。", "y"])
    assert stored(files)[0]["value"] == "名古屋"

    outputs = run_cli(ChatSession(ViewLLM(), *files), ["/revert c001", "y"])
    after = stored(files)
    assert after[0]["value"] == "川崎" and len(after) == KEYS_SIZE
    assert any(o.startswith("[長期状態] user.home_location: 名古屋 → 川崎") for o in outputs)
    assert [p["status"] for p in stored_pending(files)] == ["reverted", "unresolved"]
