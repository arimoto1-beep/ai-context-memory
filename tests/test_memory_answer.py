"""MVP12: 回答AIが、保存済みの記憶（Current）と確認待ちの発言（Pending）を区別して、記憶の状態を説明する。

データ構造・更新処理・検索は変えていない。変えたのは回答AIへの指示だけなので、ここでは
- 指示に、状態を区別して説明するための原則が入っていること
- 状態 A〜F のそれぞれで、回答AIに渡る現在値・保留が状態どおりであること（住まい以外の属性でも）
を確かめる。回答文そのものの評価は、実際の Claude で行う（docs/mvp-12.md）。
"""

import itertools
from collections import namedtuple

import pytest

from ai_context_memory.chat import ChatSession
from ai_context_memory.llm import SYSTEM_PROMPT
from test_memory_confirmation import FakeLLM, judge, make_paths, pending_item, plan, run_cli

Attribute = namedtuple(
    "Attribute", "name key old old_text new new_text other other_text question query user_question"
)

ATTRIBUTES = [
    Attribute(
        "住まい", "home_location", "川崎", "今は川崎に住んでいます。", "名古屋", "今は名古屋に住んでいます。",
        "大阪", "大阪にも部屋を借りています。", "川崎の住まいも残っていますか？", "住", "今の住まいは？",
    ),
    Attribute(
        "勤務先", "employer", "A社", "A社で働いています。", "B社", "今はB社で働いています。",
        "C社", "C社でも週に1回働いています。", "A社にも今も勤めていますか？", "働", "今の勤務先は？",
    ),
    Attribute(
        "趣味", "hobby", "テニス", "趣味はテニスです。", "釣り", "最近の趣味は釣りです。",
        "将棋", "将棋も好きでよく指します。", "テニスも今も続けていますか？", "趣味", "今の趣味は？",
    ),
]
T0 = "2026-10-01T09:00:00+09:00"


@pytest.fixture(params=ATTRIBUTES, ids=lambda a: a.key)
def attr(request):
    return request.param


@pytest.fixture
def clock(monkeypatch):
    minutes = itertools.count(1)
    monkeypatch.setattr(
        "ai_context_memory.chat.timestamp_now", lambda: f"2026-10-17T10:{next(minutes):02d}:00+09:00"
    )


def current(a, key=None, value=None, evidence=None):
    return {
        "subject": "user",
        "key": key or a.key,
        "value": value or a.old,
        "evidence": evidence or a.old_text,
        "updated_at": T0,
    }


def new_pending(a, pending_id="p001", value=None, evidence=None):
    return pending_item(pending_id, a.key, a.old, value or a.new, evidence or a.new_text, a.question)


def paths_for(tmp_path, state=(), pending=()):
    return make_paths(tmp_path, state, pending) + [tmp_path / "data" / "long_term_memory_reverts.jsonl"]


def ask(paths, a):
    """再起動して質問し、回答AIへ渡した入力を返す。"""
    llm = FakeLLM(plans=[plan([a.query])])
    run_cli(ChatSession(llm, *paths), [a.user_question, "exit"])
    assert len(llm.prompts) == 1
    return llm.prompts[0]


def block(prompt, name):
    assert f"[{name}]" in prompt
    return prompt.split(f"[{name}]\n")[1].split(f"\n[/{name}]")[0]


def entries(prompt, name):
    return block(prompt, name).split("\n\n")


# --- 回答AIへの指示 ---


def test_current_state_is_described_as_saved_memory_not_reality():
    assert "このシステムがユーザーについて保存している長期的な情報（保存済みの記憶）" in SYSTEM_PROMPT
    assert "保存済みの記憶は記録の状態であって、ユーザーの現実の今の状態を保証するものではありません。" in SYSTEM_PROMPT
    assert "保存済みの記憶の値を、ユーザーの現実の今の状態として言い切らないでください。" in SYSTEM_PROMPT
    # 現在値の説明で「現在有効」と言い切らず、保留の説明と食い違わないようにした
    assert "これは現在有効な値です。" not in SYSTEM_PROMPT
    assert "関係する保留があるときの伝え方は、[long-term memory pending] の説明に従ってください。" in SYSTEM_PROMPT


def test_updated_at_is_not_the_time_of_the_statement():
    """取り消しなどで、updated_at は発言の日時より後になることがある。"""
    assert "updated_at は記憶がその値に更新された日時で、ユーザーがその発言をした日時とは限りません。" in SYSTEM_PROMPT


def test_pending_is_a_real_statement_whose_reflection_is_on_hold():
    assert "発言の内容が疑わしいという意味ではありません。" in SYSTEM_PROMPT
    assert "保留になっているのは、その発言を記憶にどう反映するか" in SYSTEM_PROMPT
    assert "発言があったことや、発言の内容そのものではありません。" in SYSTEM_PROMPT


def test_three_states_are_distinguished_without_a_fixed_order_or_a_two_way_choice():
    assert "関係する保留があるときは、記憶の状態として次の3つを区別して伝えてください。" in SYSTEM_PROMPT
    for part in ("1つ目は保存済みの記憶", "2つ目は確認待ちの発言", "3つ目はまだ確定していない点"):
        assert part in SYSTEM_PROMPT
    assert "どちらか一方だけが正しいという二者択一として説明しないでください。" in SYSTEM_PROMPT
    assert "両方が今も有効なこともあります。" in SYSTEM_PROMPT
    assert "3つを述べる順序は決まっていません。" in SYSTEM_PROMPT
    # 順序を決めない代わりに、MVP9 で防いだ「新しい発言を補足扱いにする」ことは、どの順でも禁じる
    assert "どの順で述べる場合も、確認待ちの発言を、保存済みの記憶への補足や注記のように軽く扱わないでください。" in SYSTEM_PROMPT


def test_multiple_pending_and_pending_without_a_saved_value_are_explained():
    assert "同じ属性に関係する保留が複数あるときは、それぞれを別の発言として伝え" in SYSTEM_PROMPT
    assert "target_value が無い保留は、保留した時点で関係する保存済みの値が無かったものです。" in SYSTEM_PROMPT
    assert "その属性について保存済みの記憶は見つかっておらず、確認待ちの発言があることを伝えてください。" in SYSTEM_PROMPT


def test_coexisting_saved_values_are_not_pending():
    assert "すべてを現在の値として伝えてください。" in SYSTEM_PROMPT
    assert "これらはどれも保存済みの値で、確認待ちの保留ではありません。" in SYSTEM_PROMPT


def test_instructions_are_not_tied_to_a_specific_attribute_or_value():
    """指示の本文に、評価に使う具体的な値を書き込んでいない（住まいの key は番号付き key の説明の例だけ）。"""
    for a in ATTRIBUTES:
        for value in (a.old, a.new, a.other):
            assert value not in SYSTEM_PROMPT


# --- 状態ごとに回答AIへ渡るもの（属性ごと） ---


def test_a_current_only(tmp_path, clock, attr):
    prompt = ask(paths_for(tmp_path, [current(attr)]), attr)

    assert block(prompt, "long-term memory state") == (
        f"- user.{attr.key}: {attr.old} (evidence: {attr.old_text}, updated_at: {T0})"
    )
    assert "[long-term memory pending]" not in prompt


def test_b_current_and_a_conflicting_pending(tmp_path, clock, attr):
    prompt = ask(paths_for(tmp_path, [current(attr)], [new_pending(attr)]), attr)

    assert block(prompt, "long-term memory state").startswith(f"- user.{attr.key}: {attr.old} ")
    (pending,) = entries(prompt, "long-term memory pending")
    assert f"  target_value: {attr.old}" in pending
    assert f"  proposed_value: {attr.new}" in pending
    assert f"  evidence: {attr.new_text}" in pending
    assert "reopened_from" not in pending


def test_c_reverted_confirmation_reopens_the_statement_as_pending(tmp_path, clock, attr):
    paths = paths_for(tmp_path, [current(attr)], [new_pending(attr)])
    run_cli(ChatSession(FakeLLM([judge("replace", "いいえ")]), *paths), ["/confirm p001", "いいえ", "y"])
    run_cli(ChatSession(FakeLLM(), *paths), ["/revert c001", "y"])

    prompt = ask(paths, attr)

    # 保存済みの記憶は確認の前の値に戻り、発言は確認待ちに戻っている（撤回されていない）
    assert block(prompt, "long-term memory state").startswith(f"- user.{attr.key}: {attr.old} ")
    (pending,) = entries(prompt, "long-term memory pending")
    assert pending.startswith("- p002 ")
    assert f"  proposed_value: {attr.new}" in pending
    assert f"  evidence: {attr.new_text}" in pending
    assert "  reopened_from: p001" in pending
    # 取り消した確認の質問と回答は、今の状態の根拠として渡さない（会話は再起動で戻さない）
    assert attr.question not in prompt
    assert prompt.endswith(f"[user]\n{attr.user_question}")


def test_d_confirmed_coexistence_is_saved_values_not_pending(tmp_path, clock, attr):
    paths = paths_for(tmp_path, [current(attr)], [new_pending(attr)])
    run_cli(ChatSession(FakeLLM([judge("coexist", "はい")]), *paths), ["/confirm p001", "はい"])

    prompt = ask(paths, attr)

    lines = block(prompt, "long-term memory state").split("\n")
    assert [line.split(" (")[0] for line in lines] == [
        f"- user.{attr.key}: {attr.old}",
        f"- user.{attr.key}_2: {attr.new}",
    ]
    assert f"same_attribute_as: {attr.key}" in lines[1]
    # 確認で解決した保留は、確認待ちとして渡さない
    assert "[long-term memory pending]" not in prompt


def test_e_pending_without_a_saved_value(tmp_path, clock, attr):
    held = pending_item(
        "p001", None, None, attr.new, attr.new_text, attr.question,
        proposed_key=attr.key, target_updated_at=None, decision="pending", hold_reason=None,
    )
    prompt = ask(paths_for(tmp_path, [], [held]), attr)

    assert "[long-term memory state]" not in prompt
    (pending,) = entries(prompt, "long-term memory pending")
    assert "target_value" not in pending
    assert f"  proposed_key: {attr.key}" in pending
    assert f"  proposed_value: {attr.new}" in pending


def test_f_current_and_several_pending(tmp_path, clock, attr):
    pending = [new_pending(attr), new_pending(attr, "p002", attr.other, attr.other_text)]
    prompt = ask(paths_for(tmp_path, [current(attr)], pending), attr)

    assert block(prompt, "long-term memory state").startswith(f"- user.{attr.key}: {attr.old} ")
    first, second = entries(prompt, "long-term memory pending")
    assert first.startswith("- p001 ") and f"  proposed_value: {attr.new}" in first
    assert second.startswith("- p002 ") and f"  proposed_value: {attr.other}" in second
