"""MVP13: 検索で見つかった属性を起点に、その属性の現在値・未解決の保留・変更履歴を Python が漏れなく集める。

データは実際の処理（発言の抽出 → add・保留、/confirm → replace・共存、/revert）で作る。
検索プラン（AIの判断）だけを差し替えて、回答AIへ渡る入力を確かめる。
"""

import itertools
import json

import pytest

from ai_context_memory.chat import ChatSession
from ai_context_memory.llm import SYSTEM_PROMPT, format_memory_history
from ai_context_memory.search import MAX_ATTRIBUTE_HISTORY_EVENTS, MAX_HISTORY_ATTRIBUTES
from ai_context_memory.structured_memory import (
    attribute_history,
    load_memory_history,
    load_memory_state,
    related_memory_pending,
    related_memory_state,
    save_memory_state,
)
from test_memory_confirmation import (
    KAWASAKI,
    NAGOYA_TEXT,
    QUESTION,
    FakeLLM,
    judge,
    make_paths,
    pending_item,
    plan,
    run_cli,
)

KAWASAKI_TEXT = KAWASAKI["evidence"]  # 今は川崎に住んでいます。
EMPLOYER_TEXT = "A社で働いています。"
WHY = "なぜ住まいの記憶が変わったの？"
HOME = "attribute: user.home_location"


@pytest.fixture
def clock(monkeypatch):
    minutes = itertools.count(1)
    monkeypatch.setattr(
        "ai_context_memory.chat.timestamp_now", lambda: f"2026-10-17T10:{next(minutes):02d}:00+09:00"
    )


def candidate(decision, value, evidence, key=None, target_key=None, question=None):
    """長期状態の抽出AIの出力（候補1つ）。"""
    return json.dumps(
        [
            {
                "decision": decision,
                "subject": "user",
                "target_key": target_key,
                "proposed_key": key,
                "proposed_value": value,
                "evidence": evidence,
                "old_value_status": "not_stated" if target_key else None,
                "old_value_evidence": None,
                "temporal_scope": "current",
                "reason": "判断理由です。",
                "confirmation_question": question,
            }
        ],
        ensure_ascii=False,
    )


@pytest.fixture
def paths(tmp_path, clock):
    """発言から 川崎（add）、A社（add）を記憶し、「今は名古屋に住んでいます。」を保留 p001 にした状態。"""
    files = make_paths(tmp_path, state=(), pending=()) + [tmp_path / "data" / "long_term_memory_reverts.jsonl"]
    structured = [
        candidate("coexist", "川崎", KAWASAKI_TEXT, key="home_location"),
        candidate("coexist", "A社", EMPLOYER_TEXT, key="employer"),
        candidate("pending", "名古屋", NAGOYA_TEXT, target_key="home_location", question=QUESTION),
    ]
    run_cli(ChatSession(FakeLLM(structured=structured), *files), [KAWASAKI_TEXT, EMPLOYER_TEXT, NAGOYA_TEXT])
    assert [e["operation"] for e in load_memory_history(files[5])] == ["add", "add"]
    return files


def confirm_replace(paths):
    run_cli(
        ChatSession(FakeLLM([judge("replace", "いいえ、もうありません。")]), *paths),
        ["/confirm p001", "いいえ、もうありません。", "y"],
    )


def confirm_coexist(paths):
    run_cli(ChatSession(FakeLLM([judge("coexist", "はい、残っています。")]), *paths), ["/confirm p001", "はい、残っています。"])


def revert(paths):
    run_cli(ChatSession(FakeLLM(), *paths), ["/revert c001", "y"])


def ask(paths, queries=(), history=(), question=WHY):
    """1ターン実行し、(回答AIへの入力, CLIの出力) を返す。"""
    llm = FakeLLM(plans=[plan(queries, history)])
    outputs = run_cli(ChatSession(llm, *paths), [question])
    return llm.prompts[-1], outputs


def block(prompt, name):
    if f"[{name}]" not in prompt:
        return None
    return prompt.split(f"[{name}]\n")[1].split(f"\n[/{name}]")[0]


def history_entries(prompt):
    """変更履歴のブロックを、見出しの行と、イベントごとの (operation, key, value) に分ける。"""
    lines = []
    for entry in block(prompt, "long-term memory history").split("\n\n"):
        if entry.startswith("- "):
            fields = dict(line.strip().split(": ", 1) for line in entry.splitlines()[1:])
            lines.append((fields["operation"], fields["key"], fields.get("value")))
        else:
            lines.append(entry)
    return lines


def state_lines(prompt):
    text = block(prompt, "long-term memory state")
    return [line.split(" (")[0] for line in text.splitlines()] if text else []


def pending_ids(prompt):
    text = block(prompt, "long-term memory pending")
    return [line.split()[1] for line in text.splitlines() if line.startswith("- ")] if text else []


# --- 属性の変更履歴をすべて集める ---


def test_replace_and_revert_hits_bring_the_whole_history_of_the_attribute(paths):
    """add が検索語に当たらなくても、同じ属性の add・replace・revert がすべて古い順に渡る。"""
    confirm_replace(paths)
    revert(paths)

    prompt, outputs = ask(paths, history=["replace", "revert"])

    assert history_entries(prompt) == [
        f"{HOME} (events: 3 of 3, complete)",
        ("add", "home_location", "川崎"),
        ("replace", "home_location", "名古屋"),
        ("revert", "home_location", "川崎"),
    ]
    history = block(prompt, "long-term memory history")
    # 根拠と識別子はそのまま残る: 最初の発言、確認の質問・回答・承認、取り消しの承認と戻した保留
    assert f"evidence: {KAWASAKI_TEXT}" in history
    assert f"confirmation_question: {QUESTION}" in history
    assert "confirmation_answer: いいえ、もうありません。" in history
    assert "approval_input: y" in history
    assert "reverted_confirmation_id: c001" in history and "reopened_pending_id: p002" in history
    # 検索結果はヒットしたイベントの数、渡したのは属性の全件
    assert outputs[1:3] == ["[長期履歴検索] replace / revert", "[長期履歴検索結果] 2件"]
    assert "[長期履歴] user.home_location 3/3件" in outputs


def test_history_hit_alone_brings_the_current_value_and_unresolved_pending(paths):
    """needs_memory が false で変更履歴だけがヒットしても、同じ属性の現在値と未解決の保留が渡る。"""
    confirm_replace(paths)
    revert(paths)

    prompt, outputs = ask(paths, history=["replace", "revert"])

    assert state_lines(prompt) == ["- user.home_location: 川崎"]
    # 取り消しで戻した p002 だけ。取り消された p001 は渡らない
    assert pending_ids(prompt) == ["p002"]
    assert "  reopened_from: p001" in block(prompt, "long-term memory pending")
    # 現在値・保留・変更履歴・会話の順は変わらない
    assert prompt.index("[long-term memory state]") < prompt.index("[long-term memory pending]")
    assert prompt.index("[/long-term memory pending]") < prompt.index("[long-term memory history]")
    assert "[検索]" not in " ".join(outputs)
    assert "[長期保留検索] home_location / replace / revert" in outputs


def test_pending_hit_brings_the_current_value_of_its_attribute(paths):
    """保留の名古屋だけがヒットしても、その属性の現在値（川崎）へたどれる。

    MVP13 では、プランナーが変更履歴を不要と判断すれば変更履歴を渡さなかった。MVP14 では R4 により、
    検索語（名古屋）が現在値に一致せず保留にだけ一致した属性は、その属性の変更履歴も渡す。
    """
    prompt, outputs = ask(paths, queries=["名古屋"], question="名古屋の件はどうなった？")

    assert state_lines(prompt) == ["- user.home_location: 川崎"]
    assert pending_ids(prompt) == ["p001"]
    assert history_entries(prompt) == [
        f"{HOME} (events: 1 of 1, complete)",
        ("add", "home_location", "川崎"),
    ]
    assert "A社" not in prompt
    assert outputs[1:5] == ["[検索] 名古屋", "[検索結果] 0件", "[長期保留検索] home_location / 名古屋", "[長期保留検索結果] 1件"]
    assert "[長期履歴] 保留・変更履歴だけに一致したため、変更履歴も渡します: user.home_location" in outputs


def test_pending_hit_after_revert_brings_the_current_value_and_history(paths):
    confirm_replace(paths)
    revert(paths)

    prompt, _ = ask(paths, queries=["名古屋"], history=["名古屋"], question="名古屋の件はどうなった？")

    assert state_lines(prompt) == ["- user.home_location: 川崎"]
    assert pending_ids(prompt) == ["p002"]
    assert [e[0] for e in history_entries(prompt)[1:]] == ["add", "replace", "revert"]


def test_coexisting_numbered_key_is_part_of_the_same_attribute(paths):
    """共存で追加した home_location_2 は、home_location と同じ属性として現在値・変更履歴が集まる。"""
    confirm_coexist(paths)

    prompt, _ = ask(paths, history=["名古屋"], question="住まいの記憶はどう変わった？")

    assert state_lines(prompt) == ["- user.home_location: 川崎", "- user.home_location_2: 名古屋"]
    assert history_entries(prompt) == [
        f"{HOME} (events: 2 of 2, complete)",
        ("add", "home_location", "川崎"),
        ("add", "home_location_2", "名古屋"),
    ]
    # 解決済みの保留は渡らない
    assert "[long-term memory pending]" not in prompt


def test_reverted_coexist_is_linked_to_the_attribute(paths):
    """共存の確定を取り消した後も、取り消しのイベントは同じ属性の経緯として渡り、外した key は現在値に無い。"""
    confirm_coexist(paths)
    revert(paths)

    prompt, _ = ask(paths, history=["revert"])

    assert history_entries(prompt) == [
        f"{HOME} (events: 3 of 3, complete)",
        ("add", "home_location", "川崎"),
        ("add", "home_location_2", "名古屋"),
        ("revert", "home_location_2", None),
    ]
    assert state_lines(prompt) == ["- user.home_location: 川崎"]
    assert pending_ids(prompt) == ["p002"]


def test_current_value_hit_brings_history_when_history_is_needed(paths):
    """変更履歴の検索語が当たらなくても、現在値でヒットした属性の変更履歴を渡す（key の推測違い）。"""
    confirm_replace(paths)
    revert(paths)

    prompt, outputs = ask(paths, queries=["住ん"], history=["residence", "address"])

    assert "[長期履歴検索結果] 0件" in outputs
    assert [e[0] for e in history_entries(prompt)[1:]] == ["add", "replace", "revert"]


def test_history_is_not_sent_when_not_needed(paths):
    """変更履歴が不要という判断なら、属性がヒットしても変更履歴は渡さない（MVP12 の現在値の質問と同じ）。"""
    confirm_replace(paths)
    revert(paths)

    prompt, outputs = ask(paths, queries=["住ん"], question="今の住まいは？")

    assert "[long-term memory history]" not in prompt
    assert not any(o.startswith("[長期履歴") for o in outputs)
    assert state_lines(prompt) == ["- user.home_location: 川崎"]
    assert pending_ids(prompt) == ["p002"]


# --- 混入させない ---


def test_other_attributes_and_subjects_are_not_mixed(tmp_path, clock):
    other_subject = {**KAWASAKI, "subject": "partner", "value": "札幌", "evidence": "妻は札幌に住んでいます。"}
    employer = {**KAWASAKI, "key": "employer", "value": "A社", "evidence": EMPLOYER_TEXT}
    hobby_1 = {**KAWASAKI, "key": "home_location_1", "value": "仙台", "evidence": "仙台"}
    files = make_paths(tmp_path, state=(KAWASAKI, employer, other_subject, hobby_1), pending=())
    events = [
        {"timestamp": "t1", "operation": "add", "subject": "user", "key": "home_location", "value": "川崎",
         "evidence": KAWASAKI_TEXT},
        {"timestamp": "t2", "operation": "add", "subject": "user", "key": "employer", "value": "A社",
         "evidence": EMPLOYER_TEXT},
        {"timestamp": "t3", "operation": "add", "subject": "partner", "key": "home_location", "value": "札幌",
         "evidence": "妻は札幌に住んでいます。"},
        {"timestamp": "t4", "operation": "add", "subject": "user", "key": "home_location_1", "value": "仙台",
         "evidence": "仙台"},
    ]
    files[5].write_text("".join(json.dumps(e, ensure_ascii=False) + "\n" for e in events), encoding="utf-8")

    prompt, _ = ask(files, history=["川崎"])

    assert history_entries(prompt) == [f"{HOME} (events: 1 of 1, complete)", ("add", "home_location", "川崎")]
    assert state_lines(prompt) == ["- user.home_location: 川崎"]
    for word in ("A社", "札幌", "partner", "仙台"):
        assert word not in prompt


# --- 件数の上限と完全性 ---


def many_attributes(tmp_path, others=13):
    """home_location の add の後に、ほかの属性（attr_a〜。番号付きの key にならない名前）の add が others 件、
    最後に home_location の replace と revert がある。"""
    state = [KAWASAKI] + [
        {"subject": "user", "key": f"attr_{chr(ord('a') + n)}", "value": f"値{n}", "evidence": f"発言{n}", "updated_at": "t"}
        for n in range(others)
    ]
    files = make_paths(tmp_path, state=state, pending=())
    events = [{"timestamp": "t00", "operation": "add", "subject": "user", "key": "home_location", "value": "川崎",
               "evidence": KAWASAKI_TEXT}]
    events += [
        {"timestamp": f"t{n + 1:02d}", "operation": "add", "subject": "user", "key": f"attr_{chr(ord('a') + n)}",
         "value": f"値{n}", "evidence": f"発言{n}"}
        for n in range(others)
    ]
    events += [
        {"timestamp": "t50", "operation": "replace", "subject": "user", "key": "home_location", "old_value": "川崎",
         "value": "名古屋", "evidence": NAGOYA_TEXT},
        {"timestamp": "t51", "operation": "revert", "subject": "user", "key": "home_location", "old_value": "名古屋",
         "value": "川崎"},
    ]
    files[5].write_text("".join(json.dumps(e, ensure_ascii=False) + "\n" for e in events), encoding="utf-8")
    return files


def test_attribute_found_by_value_keeps_its_oldest_add_among_many_attributes(tmp_path, clock):
    """13属性の add があっても、値でヒットした属性の最初の add は件数の上限で落ちない。"""
    files = many_attributes(tmp_path)

    prompt, _ = ask(files, queries=["川崎"], history=["add", "replace", "revert"])

    entries = history_entries(prompt)
    home = entries[: entries.index(next(e for e in entries[1:] if isinstance(e, str)))]
    assert home == [
        f"{HOME} (events: 3 of 3, complete)",
        ("add", "home_location", "川崎"),
        ("replace", "home_location", "名古屋"),
        ("revert", "home_location", "川崎"),
    ]


def test_too_many_attributes_are_capped_and_the_omission_is_stated(tmp_path, clock):
    """属性を特定できない検索語でも、渡す属性は上限まで（新しく変わった属性から）。渡さなかった属性の数を伝える。"""
    files = many_attributes(tmp_path)

    prompt, outputs = ask(files, history=["add", "replace", "revert"], question="記憶が変わった経緯を教えて")

    headers = [e for e in history_entries(prompt) if isinstance(e, str)]
    assert headers == [
        f"{HOME} (events: 3 of 3, complete)",
        "attribute: user.attr_m (events: 1 of 1, complete)",
        "attribute: user.attr_l (events: 1 of 1, complete)",
        f"other matching attributes not included: {14 - MAX_HISTORY_ATTRIBUTES}",
    ]
    assert len(headers) - 1 == MAX_HISTORY_ATTRIBUTES
    assert f"[長期履歴] ほかに {14 - MAX_HISTORY_ATTRIBUTES} 属性の変更履歴は渡していません" in outputs
    # 変更履歴を渡した属性の現在値はすべて渡る。変更履歴だけでヒットし、変更履歴を渡さない属性の現在値は渡らない
    assert state_lines(prompt) == ["- user.home_location: 川崎", "- user.attr_l: 値11", "- user.attr_m: 値12"]


def test_long_history_of_one_attribute_keeps_the_newest_events_and_says_so(tmp_path, clock):
    total = MAX_ATTRIBUTE_HISTORY_EVENTS + 4
    files = make_paths(tmp_path, state=({**KAWASAKI, "value": f"町{total - 1}"},), pending=())
    events = [
        {"timestamp": f"t{n:02d}", "operation": "add" if n == 0 else "replace", "subject": "user",
         "key": "home_location", "value": f"町{n}", "evidence": f"今は町{n}に住んでいます。",
         **({"old_value": f"町{n - 1}"} if n else {})}
        for n in range(total)
    ]
    files[5].write_text("".join(json.dumps(e, ensure_ascii=False) + "\n" for e in events), encoding="utf-8")

    prompt, outputs = ask(files, history=["home_location"])

    entries = history_entries(prompt)
    assert entries[0] == f"{HOME} (events: {MAX_ATTRIBUTE_HISTORY_EVENTS} of {total}, 4 older events omitted)"
    # 新しいほうを残し、古い順に並べる。最初の add は省かれている
    assert [e[2] for e in entries[1:]] == [f"町{n}" for n in range(4, total)]
    assert ("add", "home_location", "町0") not in entries
    assert f"[長期履歴] user.home_location {MAX_ATTRIBUTE_HISTORY_EVENTS}/{total}件" in outputs


# --- 属性が見つからない ---


def test_attribute_that_cannot_be_found_sends_nothing_and_claims_nothing(paths):
    """日本語の属性名が内部の key と一致しないと起点が見つからない。その場合は何も渡さず、記録が無いとも書かない。"""
    confirm_replace(paths)
    revert(paths)

    prompt, outputs = ask(paths, queries=["住まい"], history=["住まい"], question="いつ住まいを変更した？")

    # 記憶のブロックは無い。属性を特定できず、検索したが一致しなかったことだけを伝え、none_stored とは書かない（MVP14）
    assert prompt.endswith("[/retrieval status]\n\n[user]\nいつ住まいを変更した？")
    status = block(prompt, "retrieval status")
    assert "attributes identified: none" in status
    assert "long-term memory state: no_match" in status
    assert "long-term memory history: no_match" in status
    # 長期記憶（現在値・保留・変更履歴）は保存されているので、none_stored とは書かない
    assert not any(
        line.startswith("long-term memory") and "none_stored" in line for line in status.splitlines()
    )
    assert "[長期履歴検索結果] 0件" in outputs
    assert not any(o.startswith("[長期履歴]") for o in outputs)


def test_attribute_without_recorded_events_is_not_described_as_having_none(tmp_path, clock):
    """変更履歴に1件も無い属性は見出しを付けない（「履歴が無い」とは書かない）。"""
    files = make_paths(tmp_path, state=(KAWASAKI,), pending=())

    prompt, _ = ask(files, queries=["川崎"], history=["home_location"])

    assert "[long-term memory history]" not in prompt
    assert state_lines(prompt) == ["- user.home_location: 川崎"]


def test_unreadable_history_is_not_treated_as_empty(paths):
    confirm_replace(paths)
    paths[5].write_text("壊れた行\n", encoding="utf-8")

    prompt, outputs = ask(paths, queries=["住ん"], history=["home_location"])

    assert any(o.startswith("[警告] 長期状態の変更履歴がJSONとして読めません") for o in outputs)
    assert "[long-term memory history]" not in prompt
    assert "attribute:" not in prompt


# --- 部品 ---


def test_attribute_helpers_follow_the_numbered_key_rule():
    events = [
        {"subject": "user", "key": "hobby", "value": "釣り"},
        {"subject": "user", "key": "hobby_2", "value": "登山"},
        {"subject": "user", "key": "hobby_1", "value": "別"},  # _1 は番号付きの key ではない
        {"subject": "user", "key": "hobby_extra", "value": "別"},
        {"subject": "partner", "key": "hobby", "value": "別"},
        {"subject": "user"},  # key の無い壊れたイベントは無視する
    ]

    assert attribute_history(events, ("user", "hobby"), 10) == (events[:2], 2)
    assert attribute_history(events, ("user", "hobby"), 1) == (events[1:2], 2)
    items = [{"subject": e["subject"], "key": e["key"]} for e in events[:5]]
    assert related_memory_state(items, [], [("user", "hobby")]) == items[:2]


def test_pending_is_linked_by_attribute_including_numbered_target_keys():
    linked = pending_item("p001", key="home_location_3")
    proposed = pending_item("p002", key=None, proposed_key="home_location_2")
    other = pending_item("p003", key="employer", old="A社", new="B社")

    chosen = related_memory_pending([linked, proposed, other], [("user", "home_location")])

    assert chosen == [linked, proposed]


def test_history_block_without_scope_keeps_the_old_format():
    event = {"timestamp": "t1", "operation": "add", "subject": "user", "key": "home_location", "value": "川崎"}

    assert format_memory_history([event]) == (
        "[long-term memory history]\n- t1\n  operation: add\n  subject: user\n  key: home_location\n  value: 川崎\n"
        "[/long-term memory history]"
    )


def test_history_block_states_coverage_per_attribute():
    events = [
        {"timestamp": "t1", "operation": "add", "subject": "user", "key": "home_location", "value": "川崎"},
        {"timestamp": "t2", "operation": "add", "subject": "user", "key": "home_location_2", "value": "名古屋"},
        {"timestamp": "t3", "operation": "replace", "subject": "user", "key": "employer", "value": "B社"},
    ]
    scope = {
        "attributes": [
            {"subject": "user", "key": "home_location", "shown": 2, "total": 2},
            {"subject": "user", "key": "employer", "shown": 1, "total": 3},
        ],
        "omitted_attributes": 2,
    }

    assert format_memory_history(events, scope).splitlines() == [
        "[long-term memory history]",
        "attribute: user.home_location (events: 2 of 2, complete)",
        "",
        "- t1", "  operation: add", "  subject: user", "  key: home_location", "  value: 川崎",
        "",
        "- t2", "  operation: add", "  subject: user", "  key: home_location_2", "  value: 名古屋",
        "",
        "attribute: user.employer (events: 1 of 3, 2 older events omitted)",
        "",
        "- t3", "  operation: replace", "  subject: user", "  key: employer", "  value: B社",
        "",
        "other matching attributes not included: 2",
        "[/long-term memory history]",
    ]


# --- 回答AIへの指示 ---


@pytest.mark.parametrize(
    "instruction",
    [
        # 履歴だけから今の保存状態を言い切らない
        "変更履歴の最後のイベントの value や reopened_pending_id から推定して言い切らないでください",
        # 省いた履歴があるときに、全経緯がそろっているように説明しない
        "渡したイベントだけで経緯がすべてそろっているように説明せず",
        "complete は、その属性について記録されている変更履歴をすべて渡していることを表します",
        # 記録されていない理由を推測しない
        "記録に無い理由や生活の事情を推測して補わないでください",
        # 発言・確認・承認・取り消しを区別する
        "変更の実行の承認、取り消しを、それぞれ別の出来事として区別してください",
        # 確認による変更の日時は発言の日時ではない
        "元の発言の日時とは限りません",
        # 見つからないことを、存在しないことと取り違えない
        "記録が無いと断定せず、見つけられなかったと伝えてください",
        # MVP11・MVP12 の指示は残っている
        "取り消しは、ユーザーの発言を撤回したものでもありません",
        "記憶の状態として次の3つを区別して伝えてください",
    ],
)
def test_system_prompt_explains_attribute_history(instruction):
    assert instruction in SYSTEM_PROMPT


def test_new_instructions_do_not_name_evaluation_values():
    """評価に使う値（川崎・名古屋・A社など）を指示に書かない。"""
    for word in ("川崎", "名古屋", "A社", "B社"):
        assert word not in SYSTEM_PROMPT


def test_state_file_round_trip_is_unchanged(tmp_path):
    """データ形式は変えていない（現在値の保存形式の互換性）。"""
    path = tmp_path / "state.json"
    save_memory_state(path, [KAWASAKI])
    assert load_memory_state(path) == [KAWASAKI]
