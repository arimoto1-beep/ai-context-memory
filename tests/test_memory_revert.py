"""MVP11: /revert で、保留の確認（MVP10）による確定1件を、記録を消さずに取り消す。"""

import itertools
import json

import pytest

from ai_context_memory import chat as chat_module
from ai_context_memory import cli as cli_module
from ai_context_memory import revert as revert_module
from ai_context_memory import structured_memory as structured_module
from ai_context_memory.chat import ChatSession
from ai_context_memory.confirmation import ConfirmationError, load_confirmations
from ai_context_memory.history import load_history
from ai_context_memory.llm import SEARCH_PLAN_SYSTEM_PROMPT, SYSTEM_PROMPT
from ai_context_memory.revert import (
    RevertError,
    append_revert,
    apply_to_state,
    complete_revert,
    load_reverts,
    plan_revert,
    revert_history_event,
)
from ai_context_memory.structured_memory import (
    append_memory_history,
    load_memory_history,
    load_memory_pending,
    load_memory_state,
    save_memory_pending,
    save_memory_state,
)
from test_memory_confirmation import (
    KAWASAKI,
    NAGOYA_TEXT,
    P001,
    QUESTION,
    T0,
    FakeLLM,
    FakeStdin,
    judge,
    make_paths,
    pending_item,
    plan,
    run_cli,
)

REVERT_TIME = "2026-10-18T09:00:00+09:00"


@pytest.fixture
def clock(monkeypatch):
    minutes = itertools.count(1)
    monkeypatch.setattr(
        "ai_context_memory.chat.timestamp_now", lambda: f"2026-10-17T10:{next(minutes):02d}:00+09:00"
    )


def make_revert_paths(tmp_path, state=(KAWASAKI,), pending=(P001,)):
    return make_paths(tmp_path, state, pending) + [tmp_path / "data" / "long_term_memory_reverts.jsonl"]


@pytest.fixture
def paths(tmp_path, clock):
    """現在値 home_location = 川崎、保留 p001「今は名古屋に住んでいます。」。取り消しログのパスつき。"""
    return make_revert_paths(tmp_path)


def state(paths):
    return load_memory_state(paths[4])


def history(paths):
    return load_memory_history(paths[5]) if paths[5].exists() else []


def pending(paths, pending_id="p001"):
    return next(p for p in load_memory_pending(paths[6]) if p["id"] == pending_id)


def revert_events(paths):
    return [e["event"] for e in load_reverts(paths[8])]


def confirm_replace(paths):
    """確認 c001 で 川崎 → 名古屋 を確定する。"""
    run_cli(
        ChatSession(FakeLLM([judge("replace", "いいえ、もうありません。")]), *paths),
        ["/confirm p001", "いいえ、もうありません。", "y"],
    )
    assert state(paths)[0]["value"] == "名古屋"


def confirm_coexist(paths):
    """確認 c001 で home_location_2 = 名古屋 を追加する。"""
    run_cli(ChatSession(FakeLLM([judge("coexist", "はい、残っています。")]), *paths), ["/confirm p001", "はい、残っています。"])
    assert [i["key"] for i in state(paths)] == ["home_location", "home_location_2"]


def file_bytes(paths):
    """会話原文・変更履歴・確認ログの、今の中身。取り消しの後も先頭がこのままであること（追記だけ）を確かめる。"""
    return {i: paths[i].read_bytes() for i in (0, 5, 7)}


def assert_only_appended(paths, before):
    for index, content in before.items():
        assert paths[index].read_bytes().startswith(content), paths[index]


# --- 置換の取り消し ---


def test_revert_of_replace_restores_the_old_value_and_reopens_the_statement(paths):
    confirm_replace(paths)
    confirmed_history = history(paths)
    before = file_bytes(paths)
    llm = FakeLLM()

    outputs = run_cli(ChatSession(llm, *paths), ["/revert c001", "y"])

    # Claude は呼ばない
    assert llm.calls == []
    # 元の値と根拠の発言に戻し、更新時刻は取り消した時刻にする
    [item] = state(paths)
    assert (item["value"], item["evidence"]) == ("川崎", "今は川崎に住んでいます。")
    assert item["updated_at"] != T0
    # 変更履歴は追記だけ。取り消しは operation: revert で、元の confirmation_id のキーを持たない
    events = history(paths)
    assert events[:1] == confirmed_history
    revert_event = events[1]
    assert revert_event["operation"] == "revert"
    assert "confirmation_id" not in revert_event
    assert {k: revert_event[k] for k in (
        "key", "old_value", "value", "revert_id", "reverted_confirmation_id", "reverted_pending_id",
        "reverted_operation", "reopened_pending_id",
    )} == {
        "key": "home_location", "old_value": "名古屋", "value": "川崎", "revert_id": "r001",
        "reverted_confirmation_id": "c001", "reverted_pending_id": "p001", "reverted_operation": "replace",
        "reopened_pending_id": "p002",
    }
    assert revert_event["timestamp"] == item["updated_at"]
    # 元の保留は reverted。解決の記録は残す
    original = pending(paths)
    assert original["status"] == "reverted"
    assert (original["resolution"], original["confirmation_id"]) == ("replace", "c001")
    assert (original["reverted_by"], original["reverted_at"]) == ("r001", item["updated_at"])
    # 同じ発言の新しい保留。比べる相手は戻した項目（更新時刻は取り消した時刻）
    reopened = pending(paths, "p002")
    assert reopened["status"] == "unresolved"
    assert (reopened["reopened_from"], reopened["reopened_by"]) == ("p001", "r001")
    assert (reopened["target_value"], reopened["target_updated_at"]) == ("川崎", item["updated_at"])
    assert (reopened["proposed_value"], reopened["evidence"]) == ("名古屋", NAGOYA_TEXT)
    assert reopened["confirmation_question"] == QUESTION
    assert reopened["resolution"] is None and reopened["confirmation_id"] is None
    # 取り消しログ。確認ログ・会話原文・変更履歴の既存部分は変わらない
    assert revert_events(paths) == ["prepared", "presented", "approval", "commit", "applied"]
    assert_only_appended(paths, before)
    # 表示
    prompt = next(o for o in outputs if "実行しますか？（y/N）" in o)
    assert "user.home_location を「名古屋」から、確認の前の値「川崎」" in prompt
    assert "新しい保留 p002 として残ります" in prompt and "/confirm p002" in prompt
    assert "[長期状態] user.home_location: 名古屋 → 川崎（r001 で確認 c001 を取り消し。p001 は reverted、発言は保留 p002 に戻しました）" in outputs


def test_revert_of_coexist_removes_only_the_added_key(paths):
    confirm_coexist(paths)
    before = file_bytes(paths)

    outputs = run_cli(ChatSession(FakeLLM(), *paths), ["/revert c001", "y"])

    assert state(paths) == [KAWASAKI]
    revert_event = history(paths)[-1]
    assert (revert_event["operation"], revert_event["key"], revert_event["old_value"]) == ("revert", "home_location_2", "名古屋")
    assert "value" not in revert_event
    assert revert_event["reverted_operation"] == "add"
    # 共存の取り消しでは、既存の項目を変えていないので、元の保留の比べる相手をそのまま使う
    reopened = pending(paths, "p002")
    assert (reopened["target_key"], reopened["target_value"], reopened["target_updated_at"]) == ("home_location", "川崎", T0)
    assert pending(paths)["status"] == "reverted"
    assert_only_appended(paths, before)
    assert any("確認で追加した user.home_location_2「名古屋」を現在の値から外します" in o for o in outputs)
    assert any("user.home_location_2: 名古屋 → 現在の値から外しました" in o for o in outputs)


def test_reopened_pending_can_be_confirmed_again_and_reverted_again(paths):
    confirm_replace(paths)
    run_cli(ChatSession(FakeLLM(), *paths), ["/revert c001", "y"])

    outputs = run_cli(
        ChatSession(FakeLLM([judge("replace", "いいえ", "p002")]), *paths), ["/confirm p002", "いいえ", "y", "/revert c002", "y"]
    )

    assert any("user.home_location: 川崎 → 名古屋（確認 c002 で p002 を解決）" in o for o in outputs)
    assert state(paths)[0]["value"] == "川崎"
    assert [e["operation"] for e in history(paths)] == ["replace", "revert", "replace", "revert"]
    assert [(p["id"], p["status"]) for p in load_memory_pending(paths[6])] == [
        ("p001", "reverted"), ("p002", "reverted"), ("p003", "unresolved")
    ]
    assert pending(paths, "p003")["reopened_from"] == "p002"


@pytest.mark.parametrize("answer", ["N", "n", "yes", "はい", "/revert c001", "/confirm p002", "取り消してください"])
def test_revert_is_not_executed_without_y(paths, answer):
    confirm_replace(paths)
    before_state, before_pending, before_history = state(paths), load_memory_pending(paths[6]), history(paths)
    llm = FakeLLM()

    outputs = run_cli(ChatSession(llm, *paths), ["/revert c001", answer])

    assert (state(paths), load_memory_pending(paths[6]), history(paths)) == (before_state, before_pending, before_history)
    assert revert_events(paths) == ["prepared", "presented", "approval"]
    assert load_reverts(paths[8])[-1]["approved"] is False
    assert "[取り消し] 取り消しませんでした。確認 c001 の変更はそのままです" in outputs
    assert llm.calls == []


def test_revert_command_while_a_confirmation_waits_cancels_the_confirmation(tmp_path, clock):
    company = {"subject": "user", "key": "employer", "value": "A社", "evidence": "A社で働いています。", "updated_at": T0}
    b_pending = pending_item("p002", "employer", "A社", "B社", "今はB社で働いています。", "A社にも今も勤めていますか？")
    paths = make_revert_paths(tmp_path, state=(KAWASAKI, company), pending=(P001, b_pending))
    confirm_replace(paths)
    llm = FakeLLM()

    outputs = run_cli(ChatSession(llm, *paths), ["/confirm p002", "/revert c001", "y"])

    assert [e["event"] for e in load_confirmations(paths[7]) if e["confirmation_id"] == "c002"] == ["prepared", "presented", "cancelled"]
    assert "[確認] 確認を取り消しました。保留はそのままです" in outputs
    assert state(paths)[0]["value"] == "川崎"
    assert pending(paths, "p002")["status"] == "unresolved"
    assert pending(paths, "p003")["reopened_from"] == "p001"
    assert llm.calls == []


def test_exit_while_waiting_for_approval_cancels(paths):
    confirm_replace(paths)

    run_cli(ChatSession(FakeLLM(), *paths), ["/revert c001", "exit"])

    assert revert_events(paths) == ["prepared", "presented", "cancelled"]
    assert state(paths)[0]["value"] == "名古屋"


def test_same_confirmation_cannot_be_reverted_twice(paths):
    confirm_replace(paths)
    run_cli(ChatSession(FakeLLM(), *paths), ["/revert c001", "y"])
    after = (state(paths), load_memory_pending(paths[6]), history(paths), revert_events(paths))

    outputs = run_cli(ChatSession(FakeLLM(), *paths), ["/revert c001", "y"])

    assert "[取り消し] 確認 c001 は既に r001 で取り消しています" in outputs
    assert (state(paths), load_memory_pending(paths[6]), history(paths), revert_events(paths)) == after


def test_complete_revert_is_idempotent(paths):
    confirm_replace(paths)
    plan_ = plan_for(paths)

    items, pending_items = complete_revert(plan_, state(paths), load_memory_pending(paths[6]), paths[4], paths[5], paths[6])
    once = (state(paths), load_memory_pending(paths[6]), history(paths))
    complete_revert(plan_, items, pending_items, paths[4], paths[5], paths[6])

    assert (state(paths), load_memory_pending(paths[6]), history(paths)) == once
    assert [e["operation"] for e in history(paths)] == ["replace", "revert"]


def test_reverted_pending_cannot_be_confirmed(paths):
    confirm_replace(paths)
    run_cli(ChatSession(FakeLLM(), *paths), ["/revert c001", "y"])

    outputs = run_cli(ChatSession(FakeLLM(), *paths), ["/confirm p001"])

    assert any("p001 は確認できません: p001 は、解決した確認を取り消した保留です" in o for o in outputs)


# --- 取り消せない場合 ---


def later_replace(paths, value, timestamp):
    """確認の後に、同じ key を別の経路（MVP9 の自動置換など）で置き換えたファイルを作る。"""
    [item] = state(paths)
    save_memory_state(paths[4], [{**item, "value": value, "updated_at": timestamp}])
    append_memory_history(
        paths[5],
        [{"operation": "replace", "subject": "user", "key": "home_location", "old_value": item["value"], "value": value, "evidence": f"{value}に引っ越しました。"}],
        timestamp,
    )


def test_later_change_of_the_same_key_blocks_the_revert(paths):
    confirm_replace(paths)
    later_replace(paths, "大阪", "2026-10-18T08:00:00+09:00")

    outputs = run_cli(ChatSession(FakeLLM(), *paths), ["/revert c001"])

    assert any("確認 c001 の後に user.home_location が変更されています" in o and "取り消せません" in o for o in outputs)
    assert not paths[8].exists()


def test_value_that_changed_and_came_back_is_not_mistaken_for_unchanged(paths):
    confirm_replace(paths)
    later_replace(paths, "大阪", "2026-10-18T08:00:00+09:00")
    later_replace(paths, "名古屋", "2026-10-18T08:30:00+09:00")

    outputs = run_cli(ChatSession(FakeLLM(), *paths), ["/revert c001"])

    assert any("確認 c001 の後に user.home_location が変更されています" in o for o in outputs)
    assert state(paths)[0]["value"] == "名古屋"


def test_changed_update_time_alone_blocks_the_revert(paths):
    """変更履歴に残らない経路で値が変わって戻った場合も、更新時刻で見つける。"""
    confirm_replace(paths)
    [item] = state(paths)
    save_memory_state(paths[4], [{**item, "updated_at": "2026-10-18T08:30:00+09:00"}])

    outputs = run_cli(ChatSession(FakeLLM(), *paths), ["/revert c001"])

    assert any("一度変わって戻った可能性があります" in o for o in outputs)


def test_added_key_that_is_gone_blocks_the_revert(paths):
    confirm_coexist(paths)
    save_memory_state(paths[4], [KAWASAKI])

    outputs = run_cli(ChatSession(FakeLLM(), *paths), ["/revert c001"])

    assert any("user.home_location_2 が現在の長期状態から無くなっています" in o for o in outputs)


def test_restoring_a_value_that_already_exists_in_the_same_attribute_is_refused(paths):
    confirm_replace(paths)
    save_memory_state(paths[4], state(paths) + [{**KAWASAKI, "key": "home_location_3"}])

    outputs = run_cli(ChatSession(FakeLLM(), *paths), ["/revert c001"])

    assert any("同じ属性の user.home_location_3 に「川崎」があるため、戻すと値が重複します" in o for o in outputs)


def test_confirmation_log_and_history_must_agree(paths):
    confirm_replace(paths)
    paths[5].write_text("", encoding="utf-8")

    outputs = run_cli(ChatSession(FakeLLM(), *paths), ["/revert c001"])

    assert any("確認ログと変更履歴が一致しません" in o for o in outputs)


def test_unreadable_history_refuses_the_revert(paths):
    confirm_replace(paths)
    with open(paths[5], "a", encoding="utf-8") as f:
        f.write("{壊れた行\n")

    outputs = run_cli(ChatSession(FakeLLM(), *paths), ["/revert c001"])

    assert any("変更履歴が読めないため、取り消せません" in o for o in outputs)
    assert not paths[8].exists()


def test_revert_in_history_without_the_log_is_refused(paths):
    confirm_replace(paths)
    append_memory_history(paths[5], [{"operation": "revert", "subject": "user", "key": "home_location", "reverted_confirmation_id": "c001"}], REVERT_TIME)

    outputs = run_cli(ChatSession(FakeLLM(), *paths), ["/revert c001"])

    assert any("取り消しログと変更履歴が一致しません" in o for o in outputs)


def test_operation_none_is_reported_as_out_of_scope(tmp_path, clock):
    nagoya = {**KAWASAKI, "key": "home_location_2", "value": "名古屋", "evidence": NAGOYA_TEXT}
    paths = make_revert_paths(tmp_path, state=(KAWASAKI, nagoya))
    run_cli(ChatSession(FakeLLM([judge("coexist", "はい")]), *paths), ["/confirm p001", "はい"])

    outputs = run_cli(ChatSession(FakeLLM(), *paths), ["/revert", "/revert c001"])

    assert any("[取り消し] c001（p001）user.home_location_2: 現在値の変更なし" in o for o in outputs)
    message = "確認 c001 は、同じ値が既にあったため現在値を変更していません。現在値を変更していない確認の取り消しは、MVP11 では扱いません"
    assert f"[取り消し]   取り消せません: {message}" in outputs
    assert f"[取り消し] {message}" in outputs
    assert pending(paths)["status"] == "resolved"


@pytest.mark.parametrize("answers", [["まだ分かりません。"], ["いいえ", "N"]])
def test_confirmation_that_changed_nothing_cannot_be_reverted(paths, answers):
    judgment = judge("unresolved", answers[0]) if len(answers) == 1 else judge("replace", answers[0])
    run_cli(ChatSession(FakeLLM([judgment]), *paths), ["/confirm p001", *answers])

    outputs = run_cli(ChatSession(FakeLLM(), *paths), ["/revert c001", "/revert"])

    assert any("確認 c001 は記憶を変更していないため、取り消せません" in o for o in outputs)
    assert "[取り消し] 記憶を変更した確認はありません" in outputs


@pytest.mark.parametrize(
    ("command", "message"),
    [("/revert c999", "確認 c999 はありません"), ("/revert p001", "確認の ID の形式が不正です: p001")],
)
def test_unknown_or_malformed_confirmation_id(paths, command, message):
    confirm_replace(paths)

    outputs = run_cli(ChatSession(FakeLLM(), *paths), [command])

    assert any(message in o for o in outputs)
    assert not paths[8].exists()


def test_revert_list_shows_candidates_and_reasons(paths):
    confirm_replace(paths)
    session = ChatSession(FakeLLM(), *paths)

    outputs = run_cli(session, ["/revert", "/revert c001", "y", "/revert"])

    assert outputs.count("[取り消し] c001（p001）user.home_location: 川崎 → 名古屋") == 2
    assert "[取り消し]   /revert c001 で取り消せます" in outputs
    assert "[取り消し]   取り消せません: 確認 c001 は既に r001 で取り消しています" in outputs


def test_revert_needs_its_log_path(tmp_path, clock):
    paths = make_paths(tmp_path)

    outputs = run_cli(ChatSession(FakeLLM(), *paths), ["/revert c001"])

    assert any("保存先が設定されていないため、取り消せません" in o for o in outputs)


# --- 確認できなくなる保留 ---


def test_pending_that_becomes_unconfirmable_is_shown_before_approval(paths):
    confirm_replace(paths)
    [item] = state(paths)
    osaka = pending_item("p002", old="名古屋", new="大阪", evidence="大阪に住んでいます。", question="名古屋の住まいも残っていますか？",
                         target_updated_at=item["updated_at"], created_at="2026-10-18T08:00:00+09:00")
    save_memory_pending(paths[6], load_memory_pending(paths[6]) + [osaka])

    outputs = run_cli(ChatSession(FakeLLM(), *paths), ["/revert c001", "y", "/pending"])

    prompt = next(o for o in outputs if "実行しますか？（y/N）" in o)
    assert "注意: 取り消すと、次の保留は確認できなくなります。p002（保留した後に user.home_location の値が「名古屋」から「川崎」に変わっています）" in prompt
    assert "新しい保留 p003 として残ります" in prompt
    assert pending(paths, "p002")["status"] == "unresolved"
    assert any("確認できません: 保留した後に user.home_location の値が「名古屋」から「川崎」に変わっています" in o for o in outputs)
    assert any(o.startswith("[長期保留] p003") and "r001 で p001 の確定を取り消して保留に戻したもの" in o for o in outputs)


# --- 番号付きの key ---


def test_numbered_key_is_not_reused_after_reverting_a_coexist(paths):
    confirm_coexist(paths)
    run_cli(ChatSession(FakeLLM(), *paths), ["/revert c001", "y"])

    run_cli(ChatSession(FakeLLM([judge("coexist", "はい", "p002")]), *paths), ["/confirm p002", "はい"])

    assert [i["key"] for i in state(paths)] == ["home_location", "home_location_3"]
    assert [(e["operation"], e["key"]) for e in history(paths)] == [
        ("add", "home_location_2"), ("revert", "home_location_2"), ("add", "home_location_3")
    ]


# --- 承認の記録 ---


class OutputFailure(Exception):
    pass


def fail_on_prompt(outputs):
    def output(text):
        if "実行しますか？（y/N）" in text:
            raise OutputFailure("出力できません")
        outputs.append(text)

    return output


def run_with_output(session, inputs, output_fn):
    it = iter(inputs)

    def read(prompt):
        try:
            return next(it)
        except StopIteration:
            raise EOFError from None

    cli_module.run(session, input_fn=read, output_fn=output_fn)


def test_prompt_that_fails_to_be_output_is_not_presented(paths):
    confirm_replace(paths)
    session = ChatSession(FakeLLM(), *paths)
    outputs = []

    with pytest.raises(OutputFailure):
        run_with_output(session, ["/revert c001"], fail_on_prompt(outputs))

    assert revert_events(paths) == ["prepared", "cancelled"]
    assert session.awaiting_revert is None
    # 次の y は承認ではなく通常の会話になる
    run_cli(session, ["y"])
    assert state(paths)[0]["value"] == "名古屋"
    assert "complete" in session.llm.calls


def test_presented_is_written_only_after_the_prompt_is_output(paths):
    confirm_replace(paths)
    session = ChatSession(FakeLLM(), *paths)
    seen = []

    def output(text):
        if "実行しますか？（y/N）" in text:
            seen.append((revert_events(paths), session.awaiting_revert))

    run_with_output(session, ["/revert c001"], output)

    assert seen == [(["prepared"], None)]
    assert revert_events(paths) == ["prepared", "presented", "cancelled"]


def test_failure_to_record_presented_does_not_wait_for_approval(paths, monkeypatch):
    confirm_replace(paths)
    session = ChatSession(FakeLLM(), *paths)
    original = revert_module.append_text

    def fail_presented(path, text):
        if '"presented"' in text and path == paths[8]:
            raise OSError("書けません")
        return original(path, text)

    monkeypatch.setattr(revert_module, "append_text", fail_presented)
    outputs = run_cli(session, ["/revert c001", "y"])

    assert any("変更内容を表示したことを記録できなかったため、取り消しを中止しました" in o for o in outputs)
    assert session.awaiting_revert is None
    assert state(paths)[0]["value"] == "名古屋"
    assert revert_events(paths) == ["prepared"]


def fail_conversation_line(monkeypatch, marker):
    original = chat_module.append_history

    def fail(path, messages):
        if any(marker in json.dumps(m, ensure_ascii=False) for m in messages):
            raise chat_module.HistoryError("会話原文に書けません")
        return original(path, messages)

    monkeypatch.setattr(chat_module, "append_history", fail)


def test_prompt_that_cannot_be_saved_to_the_conversation_does_not_wait(paths, monkeypatch):
    confirm_replace(paths)
    fail_conversation_line(monkeypatch, '"revert_event": "presented"')
    session = ChatSession(FakeLLM(), *paths)

    outputs = run_cli(session, ["/revert c001", "y"])

    assert revert_events(paths) == ["prepared", "presented", "cancelled"]
    assert any("会話原文に変更内容を保存できなかったため、取り消しを中止しました" in o for o in outputs)
    assert state(paths)[0]["value"] == "名古屋"


def test_approval_that_cannot_be_saved_to_the_conversation_does_not_revert(paths, monkeypatch):
    confirm_replace(paths)
    fail_conversation_line(monkeypatch, '"revert_event": "approval"')

    outputs = run_cli(ChatSession(FakeLLM(), *paths), ["/revert c001", "y"])

    assert revert_events(paths) == ["prepared", "presented", "cancelled"]
    assert any("承認の入力を会話原文に保存できなかったため、取り消しませんでした" in o for o in outputs)
    assert state(paths)[0]["value"] == "名古屋"
    assert pending(paths)["status"] == "resolved"
    # 再起動しても実行しない
    ChatSession(FakeLLM(), *paths)
    assert state(paths)[0]["value"] == "名古屋"


def test_conversation_keeps_the_prompt_and_the_input_with_the_revert_id(paths):
    confirm_replace(paths)

    run_cli(ChatSession(FakeLLM(), *paths), ["/revert c001", "y"])

    lines = [json.loads(line) for line in paths[0].read_text(encoding="utf-8").splitlines()]
    revert_lines = [line for line in lines if line.get("revert_id") == "r001"]
    log = load_reverts(paths[8])
    assert [(line["role"], line["revert_event"]) for line in revert_lines] == [("assistant", "presented"), ("user", "approval")]
    assert revert_lines[0]["content"] == log[1]["prompt"] == log[0]["prompt"]
    assert revert_lines[1]["content"] == log[2]["input"] == "y"
    load_history(paths[0])


def test_state_changed_after_the_prompt_is_not_reverted(paths):
    confirm_replace(paths)
    session = ChatSession(FakeLLM(), *paths)
    run_cli(session, ["/revert c001"])
    assert session.awaiting_revert
    # 表示した後に、このセッションの外で長期状態が変わった
    later_replace(paths, "大阪", "2026-10-18T08:00:00+09:00")

    result = session.approve_revert("y")

    assert result.status == "rejected"
    assert "このセッションの外で変更されています" in result.reason
    assert revert_events(paths) == ["prepared", "presented", "approval", "failed"]
    assert state(paths)[0]["value"] == "大阪"


@pytest.mark.parametrize("stop_after", [["/revert c001"], ["/revert c001", "y"]])
def test_restart_does_not_resume_an_unapproved_revert(paths, monkeypatch, stop_after):
    confirm_replace(paths)
    session = ChatSession(FakeLLM(), *paths)
    if len(stop_after) == 2:
        # y を会話原文に保存した直後（approval を書く前）にプロセスが止まった
        original = session._revert_log

        def stop(name, *args, **kwargs):
            if name == "approval":
                raise KeyboardInterrupt
            return original(name, *args, **kwargs)

        monkeypatch.setattr(session, "_revert_log", stop)
        with pytest.raises(KeyboardInterrupt):
            run_cli(session, stop_after)
    else:
        run_cli(session, stop_after)

    restarted = ChatSession(FakeLLM(), *paths)
    run_cli(restarted, ["y"])

    assert revert_events(paths) == ["prepared", "presented", "expired"]
    assert restarted.startup_messages == [
        "r001（確認 c001 の取り消し）: 前回の取り消しは承認の前に終わったため、続きは行いません（再起動を挟んだため、続きを行いません）"
    ]
    assert state(paths)[0]["value"] == "名古屋"
    assert pending(paths)["status"] == "resolved"
    # もう一度起動しても何もしない
    assert ChatSession(FakeLLM(), *paths).startup_messages == []


def test_restart_after_prepared_only_is_expired(paths):
    confirm_replace(paths)
    session = ChatSession(FakeLLM(), *paths)
    session.start_revert("c001")

    restarted = ChatSession(FakeLLM(), *paths)

    assert revert_events(paths) == ["prepared", "expired"]
    assert "変更内容を表示した記録が無いまま終わった" in restarted.startup_messages[0]


# --- R7: ファイルとセッションの一致 ---

PEAR = {"subject": "user", "key": "favorite_fruit", "value": "梨", "evidence": "好きな果物は梨です。", "updated_at": T0}
OSAKA_TEXT = "大阪に住んでいます。"


def add_unrelated_state(paths):
    save_memory_state(paths[4], state(paths) + [PEAR])


def add_unrelated_pending(paths):
    save_memory_pending(paths[6], load_memory_pending(paths[6]) + [pending_item("p009", "favorite_fruit", "梨", "りんご", "りんごが好きです。")])


def add_later_history(paths):
    # 現在値はそのままで、変更履歴にだけ同じ key の後の変更がある
    append_memory_history(
        paths[5],
        [{"operation": "replace", "subject": "user", "key": "home_location", "old_value": "名古屋", "value": "大阪", "evidence": OSAKA_TEXT}],
        "2026-10-18T08:00:00+09:00",
    )


@pytest.mark.parametrize(
    ("change", "message"),
    [
        (add_unrelated_state, "長期状態ファイルが、このセッションの外で変更されています"),
        (add_unrelated_pending, "長期保留ファイルが、このセッションの外で変更されています"),
        (add_later_history, "確認 c001 の後に user.home_location が変更されています"),
    ],
)
def test_external_change_before_the_start_is_detected(paths, change, message):
    confirm_replace(paths)
    session = ChatSession(FakeLLM(), *paths)
    # セッションを始めた後、/revert の前に、このセッションの外でファイルが変わった
    change(paths)

    outputs = run_cli(session, ["/revert c001"])

    assert any(o.startswith("[取り消し]") and message in o for o in outputs)
    assert not paths[8].exists()
    assert session.awaiting_revert is None


def waiting_revert(paths):
    """c001 を確定し、別のセッションで /revert c001 の y/N を待っている状態にする。"""
    confirm_replace(paths)
    session = ChatSession(FakeLLM(), *paths)
    run_cli(session, ["/revert c001"])
    assert session.awaiting_revert
    return session


@pytest.mark.parametrize(
    ("change", "message"),
    [
        # 対象と関係のない項目の変更（表示する文面は変わらない）も、ファイルの比較で見つける
        (add_unrelated_state, "長期状態ファイルが、このセッションの外で変更されています"),
        (add_unrelated_pending, "長期保留ファイルが、このセッションの外で変更されています"),
        # 変更履歴は毎回ファイルから読み直すので、後の変更（R4）として見つける
        (add_later_history, "確認 c001 の後に user.home_location が変更されています"),
    ],
)
def test_external_change_while_waiting_for_approval_is_not_executed(paths, change, message):
    session = waiting_revert(paths)
    change(paths)
    changed = (state(paths), load_memory_pending(paths[6]), history(paths))

    result = session.approve_revert("y")

    assert result.status == "rejected"
    assert message in result.reason
    assert revert_events(paths) == ["prepared", "presented", "approval", "failed"]
    assert (state(paths), load_memory_pending(paths[6]), history(paths)) == changed
    assert pending(paths)["status"] == "resolved"


def test_plan_that_differs_from_the_displayed_prompt_is_not_executed(paths):
    """ファイルとセッションが一致していても、作り直した計画が表示した内容と違えば実行しない。"""
    session = waiting_revert(paths)
    [item] = session.memory_state
    osaka = pending_item("p002", old="名古屋", new="大阪", evidence=OSAKA_TEXT, question="名古屋の住まいも残っていますか？",
                         target_updated_at=item["updated_at"], created_at="2026-10-18T08:00:00+09:00")
    session.memory_pending = session.memory_pending + [osaka]
    save_memory_pending(paths[6], session.memory_pending)

    result = session.approve_revert("y")

    assert result.status == "rejected"
    assert result.reason == "変更内容を表示した後に状態が変わったため、表示した内容のとおりには取り消せません"
    assert revert_events(paths) == ["prepared", "presented", "approval", "failed"]
    assert state(paths)[0]["value"] == "名古屋"


def test_executed_plan_is_the_one_rebuilt_at_approval(paths):
    session = waiting_revert(paths)

    session.approve_revert("y")

    log = load_reverts(paths[8])
    prepared_plan = log[0]["plan"]
    committed_plan = next(e for e in log if e["event"] == "commit")["plan"]
    # 承認した時点の状態で作り直した計画を実行する（表示したときの計画を使い回さない）
    assert committed_plan["reverted_at"] != prepared_plan["reverted_at"]
    assert committed_plan["reverted_at"] == state(paths)[0]["updated_at"]
    assert {k: v for k, v in committed_plan.items() if k not in ("reverted_at", "restored", "after", "state_after", "reopened_pending")} == {
        k: v for k, v in prepared_plan.items() if k not in ("reverted_at", "restored", "after", "state_after", "reopened_pending")
    }


# --- R8: 操作の排他制御 ---


@pytest.fixture
def two_pending_paths(tmp_path, clock):
    """c001（p001）を確定済みで、別の保留 p002（勤務先）が未解決。"""
    company = {"subject": "user", "key": "employer", "value": "A社", "evidence": "A社で働いています。", "updated_at": T0}
    b_pending = pending_item("p002", "employer", "A社", "B社", "今はB社で働いています。", "A社にも今も勤めていますか？")
    paths = make_revert_paths(tmp_path, state=(KAWASAKI, company), pending=(P001, b_pending))
    confirm_replace(paths)
    return paths


def confirmation_events(paths, confirmation_id):
    return [e["event"] for e in load_confirmations(paths[7]) if e["confirmation_id"] == confirmation_id]


def test_revert_cannot_start_while_a_confirmation_waits_for_an_answer(two_pending_paths):
    paths = two_pending_paths
    session = ChatSession(FakeLLM(), *paths)
    started = session.start_confirmation("p002")
    session.mark_presented(started["confirmation_id"])

    with pytest.raises(RevertError, match="ほかの確認・取り消しの途中のため、取り消せません"):
        session.start_revert("c001")

    assert not paths[8].exists()
    # 確認はそのまま続いている
    assert session.active_confirmation["confirmation_id"] == "c002"
    assert confirmation_events(paths, "c002") == ["prepared", "presented"]


def test_revert_cannot_start_while_a_replace_waits_for_approval(two_pending_paths):
    paths = two_pending_paths
    session = ChatSession(FakeLLM([judge("replace", "いいえ", "p002")]), *paths)
    run_cli(session, ["/confirm p002", "いいえ"])
    assert session.awaiting_approval

    with pytest.raises(RevertError, match="ほかの確認・取り消しの途中のため、取り消せません"):
        session.start_revert("c001")
    assert not paths[8].exists()

    # CLI では、y/N 待ちの /revert は「実行しない」への入力で、取り消しは始まらない
    outputs = run_cli(session, ["/revert c001"])
    assert "[確認] 実行しませんでした。p002 は保留のままです" in outputs
    assert not paths[8].exists()
    assert [i["value"] for i in state(paths)] == ["名古屋", "A社"]


def test_confirmation_and_another_revert_cannot_start_while_a_revert_waits(two_pending_paths):
    paths = two_pending_paths
    session = ChatSession(FakeLLM(), *paths)
    run_cli(session, ["/revert c001"])
    assert session.awaiting_revert

    with pytest.raises(ConfirmationError, match="取り消しの承認を待っているため、確認できません"):
        session.start_confirmation("p002")
    with pytest.raises(RevertError, match="ほかの確認・取り消しの途中のため、取り消せません"):
        session.start_revert("c001")

    assert confirmation_events(paths, "c002") == []
    assert revert_events(paths) == ["prepared", "presented"]
    assert session.awaiting_revert["revert_id"] == "r001"


def test_leftover_prepared_operation_is_closed_when_another_starts(two_pending_paths):
    paths = two_pending_paths
    session = ChatSession(FakeLLM(), *paths)

    # 確認を準備しただけ（出力していない）で取り消しを始める → 確認は閉じる
    session.start_confirmation("p002")
    session.start_revert("c001")
    assert confirmation_events(paths, "c002") == ["prepared", "cancelled"]
    # 取り消しを準備しただけで確認を始める → 取り消しは閉じる
    session.start_confirmation("p002")
    assert [e["event"] for e in load_reverts(paths[8]) if e["revert_id"] == "r001"] == ["prepared", "cancelled"]
    # 取り消しを準備しただけで、もう一度取り消しを始める → 前の取り消しは閉じる
    session.start_revert("c001")
    session.start_revert("c001")
    assert [e["event"] for e in load_reverts(paths[8]) if e["revert_id"] == "r002"] == ["prepared", "cancelled"]
    assert session.prepared_revert["revert_id"] == "r003"
    assert session.prepared_confirmation is None
    assert [i["value"] for i in state(paths)] == ["名古屋", "A社"]


def test_repeated_operations_in_one_session_keep_files_and_session_consistent(paths):
    confirm_replace(paths)
    session = ChatSession(FakeLLM([judge("replace", "いいえ", "p002")]), *paths)

    run_cli(session, ["/revert c001", "N", "/revert c001", "y", "/confirm p002", "いいえ", "y", "/revert c002", "y", "/revert c001", "/revert c002"])

    assert session.memory_state == state(paths) == [{**KAWASAKI, "updated_at": state(paths)[0]["updated_at"]}]
    assert session.memory_pending == load_memory_pending(paths[6])
    assert [(p["id"], p["status"]) for p in session.memory_pending] == [("p001", "reverted"), ("p002", "reverted"), ("p003", "unresolved")]
    assert [e["operation"] for e in history(paths)] == ["replace", "revert", "replace", "revert"]
    log = load_reverts(paths[8])
    assert [(r, [e["event"] for e in log if e["revert_id"] == r]) for r in ("r001", "r002", "r003")] == [
        ("r001", ["prepared", "presented", "approval"]),
        ("r002", ["prepared", "presented", "approval", "commit", "applied"]),
        ("r003", ["prepared", "presented", "approval", "commit", "applied"]),
    ]
    # 2回目の取り消しはどちらも始まらない（取り消しログに r004 が無い）
    assert not any(e["revert_id"] == "r004" for e in log)
    assert session.prepared_revert is None and session.awaiting_revert is None


# --- 実行中の保存の失敗 ---


def failing(function, times):
    count = itertools.count()

    def wrapper(*args, **kwargs):
        if next(count) < times:
            raise structured_module.StructuredMemoryError("ディスクに書き込めません")
        return function(*args, **kwargs)

    return wrapper


def test_current_save_failure_reverts_nothing(paths, monkeypatch):
    confirm_replace(paths)
    before = (state(paths), load_memory_pending(paths[6]), history(paths))
    monkeypatch.setattr(revert_module, "save_memory_state", failing(save_memory_state, 1))

    outputs = run_cli(ChatSession(FakeLLM(), *paths), ["/revert c001", "y"])

    assert (state(paths), load_memory_pending(paths[6]), history(paths)) == before
    assert revert_events(paths) == ["prepared", "presented", "approval", "commit", "failed"]
    assert "[取り消し] 取り消しませんでした: ディスクに書き込めません" in outputs


def test_history_failure_is_completed_by_the_immediate_retry(paths, monkeypatch):
    confirm_replace(paths)
    monkeypatch.setattr(revert_module, "append_memory_history", failing(structured_module.append_memory_history, 1))

    outputs = run_cli(ChatSession(FakeLLM(), *paths), ["/revert c001", "y"])

    assert state(paths)[0]["value"] == "川崎"
    assert [e["operation"] for e in history(paths)] == ["replace", "revert"]
    assert pending(paths)["status"] == "reverted"
    assert revert_events(paths) == ["prepared", "presented", "approval", "commit", "applied"]
    assert load_reverts(paths[8])[-1]["recovered"] is True
    assert any("続きを実行して取り消しました" in o for o in outputs)


def test_persistent_pending_failure_blocks_writes_and_restart_completes_it(paths, monkeypatch):
    confirm_replace(paths)
    monkeypatch.setattr(revert_module, "save_memory_pending", failing(save_memory_pending, 99))
    session = ChatSession(FakeLLM(), *paths)

    outputs = run_cli(session, ["/revert c001", "y", "/confirm p001", "/revert c001"])

    # 現在値は戻したが、保留を書けなかった。完了したことにせず、以後の書き込みを止める
    assert state(paths)[0]["value"] == "川崎"
    assert pending(paths)["status"] == "resolved"
    assert session.write_blocked
    assert any("長期状態の書き込みを停止しました。再起動してください" in o for o in outputs)
    assert any("長期状態の書き込みを停止しているため、取り消せません" in o for o in outputs)
    assert revert_events(paths) == ["prepared", "presented", "approval", "commit"]

    monkeypatch.undo()
    restarted = ChatSession(FakeLLM(), *paths)

    assert restarted.startup_messages == ["r001（確認 c001 の取り消し）: 前回途中で止まった取り消しを確認し、続きを実行して取り消しました"]
    assert pending(paths)["status"] == "reverted"
    assert pending(paths, "p002")["reopened_from"] == "p001"
    assert [e["operation"] for e in history(paths)] == ["replace", "revert"]
    assert revert_events(paths) == ["prepared", "presented", "approval", "commit", "applied"]
    assert ChatSession(FakeLLM(), *paths).startup_messages == []


def test_writes_blocked_by_a_partial_log_append_refuse_the_commit(paths):
    confirm_replace(paths)
    session = ChatSession(FakeLLM(), *paths)
    run_cli(session, ["/revert c001"])
    session.write_blocked = "取り消しログの末尾が壊れている可能性があります"

    result = session.approve_revert("y")

    assert result.status == "failed"
    assert "commit" not in revert_events(paths)
    assert state(paths)[0]["value"] == "名古屋"


# --- 再起動時の復旧（各段階でプロセスが止まった場合） ---


def plan_for(paths, confirmation_id="c001"):
    return plan_revert(
        "r001",
        confirmation_id,
        load_confirmations(paths[7]),
        load_reverts(paths[8]),
        state(paths),
        history(paths),
        load_memory_pending(paths[6]),
        REVERT_TIME,
    )


def crash_after(paths, stage):
    """取り消しの変更計画を書き、stage までの手順を実行した直後にプロセスが止まったファイルを作る。"""
    plan_ = plan_for(paths)
    for event in ("prepared", "presented", "approval", "commit"):
        fields = {"approved": True, "input": "y"} if event == "approval" else {}
        if event in ("prepared", "commit"):
            fields["plan"] = plan_
        append_revert(paths[8], {"event": event, "revert_id": "r001", "target_confirmation_id": "c001", "timestamp": REVERT_TIME, **fields})
    stages = ["commit", "current", "history", "pending"]
    if stages.index(stage) >= 1:
        save_memory_state(paths[4], apply_to_state(plan_, state(paths)))
    if stages.index(stage) >= 2:
        append_memory_history(paths[5], [revert_history_event(plan_)], REVERT_TIME)
    if stages.index(stage) >= 3:
        pending_items, _ = revert_module._reverted_pending(plan_, load_memory_pending(paths[6]))
        save_memory_pending(paths[6], pending_items)
    return plan_


def test_crash_before_current_is_written_is_marked_failed_on_restart(paths):
    confirm_replace(paths)
    before = (state(paths), load_memory_pending(paths[6]), history(paths))
    crash_after(paths, "commit")

    restarted = ChatSession(FakeLLM(), *paths)

    assert restarted.startup_messages == ["r001（確認 c001 の取り消し）: 前回途中で止まった取り消しを確認し、反映されていなかったため、取り消しませんでした"]
    assert (state(paths), load_memory_pending(paths[6]), history(paths)) == before
    assert revert_events(paths)[-1] == "failed"
    # 取り消していないので、もう一度取り消せる
    run_cli(restarted, ["/revert c001", "y"])
    assert state(paths)[0]["value"] == "川崎"


@pytest.mark.parametrize("stage", ["current", "history", "pending"])
@pytest.mark.parametrize("confirm", [confirm_replace, confirm_coexist])
def test_crash_after_current_is_written_is_rolled_forward_once(paths, stage, confirm):
    confirm(paths)
    confirmed = state(paths)
    plan_ = crash_after(paths, stage)

    restarted = ChatSession(FakeLLM(), *paths)

    assert "続きを実行して取り消しました" in restarted.startup_messages[0]
    assert state(paths) == apply_to_state(plan_, confirmed)
    revert_history = [e for e in history(paths) if e["operation"] == "revert"]
    assert len(revert_history) == 1
    assert pending(paths)["status"] == "reverted"
    assert [p["id"] for p in load_memory_pending(paths[6])] == ["p001", "p002"]
    assert revert_events(paths)[-1] == "applied"
    assert load_reverts(paths[8])[-1]["recovered"] is True
    # もう一度起動しても何もしない
    ChatSession(FakeLLM(), *paths)
    assert len([e for e in history(paths) if e["operation"] == "revert"]) == 1
    assert revert_events(paths).count("applied") == 1


def test_coexist_rollforward_restores_the_exact_state(paths):
    confirm_coexist(paths)
    crash_after(paths, "current")

    ChatSession(FakeLLM(), *paths)

    assert state(paths) == [KAWASAKI]


def test_crash_after_everything_but_applied_is_closed_on_restart(paths):
    confirm_replace(paths)
    crash_after(paths, "pending")
    done = (state(paths), load_memory_pending(paths[6]), history(paths))

    ChatSession(FakeLLM(), *paths)

    assert (state(paths), load_memory_pending(paths[6]), history(paths)) == done
    assert revert_events(paths)[-1] == "applied"


def test_unrecoverable_state_stops_the_startup(paths):
    confirm_replace(paths)
    crash_after(paths, "commit")
    save_memory_state(paths[4], [{**KAWASAKI, "value": "横浜"}])

    with pytest.raises(RevertError, match="自動では復旧できません"):
        ChatSession(FakeLLM(), *paths)
    assert revert_events(paths)[-1] == "commit"


def test_missing_added_key_with_other_damage_is_not_taken_as_reverted(paths):
    """共存の取り消しの実行後は key が無い状態。key が無いだけでなく、全体が計画どおりでなければ続けない。"""
    confirm_coexist(paths)
    crash_after(paths, "commit")
    save_memory_state(paths[4], [{**KAWASAKI, "evidence": "壊れた内容"}])

    with pytest.raises(RevertError, match="自動では復旧できません"):
        ChatSession(FakeLLM(), *paths)
    assert history(paths)[-1]["operation"] == "add"
    assert pending(paths)["status"] == "resolved"


def test_current_before_but_history_already_reverted_stops_the_startup(paths):
    confirm_replace(paths)
    plan_ = crash_after(paths, "commit")
    append_memory_history(paths[5], [revert_history_event(plan_)], REVERT_TIME)

    with pytest.raises(RevertError, match="取り消し前のままなのに"):
        ChatSession(FakeLLM(), *paths)


def test_reopened_id_taken_by_another_pending_stops_the_startup(paths):
    confirm_replace(paths)
    crash_after(paths, "current")
    save_memory_pending(paths[6], load_memory_pending(paths[6]) + [pending_item("p002", new="大阪", evidence="大阪です。")])

    with pytest.raises(RevertError, match="別の保留に使われています"):
        ChatSession(FakeLLM(), *paths)


def test_truncated_revert_log_line_stops_the_startup(paths):
    confirm_replace(paths)
    with open(paths[8], "w", encoding="utf-8") as f:
        f.write('{"event": "prepared", "revert_id": "r00')

    with pytest.raises(RevertError, match="最後の行が途中で途切れています"):
        ChatSession(FakeLLM(), *paths)


def test_main_reports_revert_recovery_and_counts(tmp_path, monkeypatch, capsys, clock):
    paths = make_revert_paths(tmp_path)
    confirm_replace(paths)
    crash_after(paths, "current")
    names = (
        "ACM_HISTORY_FILE",
        "ACM_MEMORY_FILE",
        "ACM_WORKING_MEMORY_FILE",
        "ACM_WORKING_MEMORY_HISTORY_FILE",
        "ACM_LONG_TERM_MEMORY_STATE_FILE",
        "ACM_LONG_TERM_MEMORY_HISTORY_FILE",
        "ACM_LONG_TERM_MEMORY_PENDING_FILE",
        "ACM_LONG_TERM_MEMORY_CONFIRMATIONS_FILE",
        "ACM_LONG_TERM_MEMORY_REVERTS_FILE",
    )
    for name, path in zip(names, paths):
        monkeypatch.setenv(name, str(path))
    monkeypatch.setattr(cli_module, "ClaudeCLI", lambda: FakeLLM())
    monkeypatch.setattr(cli_module.sys, "stdin", FakeStdin("exit\n"))
    monkeypatch.setattr(cli_module.sys.stdout, "reconfigure", lambda **kwargs: None, raising=False)

    assert cli_module.main() == 0
    out = capsys.readouterr().out
    assert "長期保留を読み込みました（未解決 1件、解決済み 0件、取り消し済み 1件）。" in out
    assert "[復旧] r001（確認 c001 の取り消し）: 前回途中で止まった取り消しを確認し、続きを実行して取り消しました" in out


# --- 再起動後の追跡と、回答AIへの渡し方 ---


def test_answer_ai_sees_the_revert_apart_from_the_original_answer(paths):
    confirm_replace(paths)
    run_cli(ChatSession(FakeLLM(), *paths), ["/revert c001", "y"])
    llm = FakeLLM(plans=[plan(["住ん"], history=["home_location"])])

    run_cli(ChatSession(llm, *paths), ["なぜ記憶が変わった？"])

    prompt = llm.prompts[-1]
    block = prompt.split("[long-term memory history]\n")[1].split("\n[/long-term memory history]")[0]
    confirmed, reverted = block.split("\n\n")
    # 元の確認のイベントには、その確認の質問・回答が付く
    assert "confirmation_answer: いいえ、もうありません。" in confirmed
    # 取り消しのイベントには、元の確認の質問・回答を付けない（取り消しの根拠と誤解させないため）
    assert "operation: revert" in reverted
    assert "reverted_confirmation_id: c001" in reverted
    assert "revert_approval_input: y" in reverted
    assert "reopened_pending_id: p002" in reverted
    assert "confirmation_answer" not in reverted and "confirmation_question" not in reverted
    assert "\n  confirmation_id:" not in reverted
    # 現在値は戻した値。保留に戻した発言は、未確認の保留として渡る
    assert "- user.home_location: 川崎" in prompt
    pending_block = prompt.split("[long-term memory pending]\n")[1].split("\n[/long-term memory pending]")[0]
    assert "- p002" in pending_block and "reopened_from: p001" in pending_block
    assert "- p001" not in pending_block


def test_prompts_explain_revert_events():
    assert "revert（確認によって行った変更が取り消された）" in SYSTEM_PROMPT
    assert "取り消しの根拠ではありません" in SYSTEM_PROMPT
    assert "取り消しの理由は記録されていません。理由を推測して補わず" in SYSTEM_PROMPT
    assert "取り消しは、ユーザーの発言を撤回したものでもありません" in SYSTEM_PROMPT
    assert "reopened_from がある項目は" in SYSTEM_PROMPT
    assert "operation（add / replace / revert のいずれか）" in SEARCH_PLAN_SYSTEM_PROMPT


def test_revert_is_traceable_with_pending_details_after_restart(paths):
    confirm_replace(paths)
    run_cli(ChatSession(FakeLLM(), *paths), ["/revert c001", "y"])

    outputs = run_cli(ChatSession(FakeLLM(), *paths), ["/pending p001"])

    assert "[長期保留] p001 status: reverted" in outputs
    assert "[長期保留]   reverted_by: r001" in outputs
    assert any(o.startswith("[確認記録] c001 approval") for o in outputs)
    assert any(o.startswith("[取り消し記録] r001 approval") and "承認" in o for o in outputs)
    assert any(o.startswith("[取り消し記録] r001 applied") for o in outputs)


# --- 互換性 ---


def test_mvp10_files_without_reverts_still_load(paths):
    confirm_replace(paths)
    legacy = [{k: v for k, v in p.items() if k not in ("reverted_by", "reverted_at", "reopened_from", "reopened_by")}
              for p in load_memory_pending(paths[6])]
    paths[6].write_text(json.dumps({"items": legacy}, ensure_ascii=False), encoding="utf-8")

    session = ChatSession(FakeLLM(), *paths)

    assert session.reverts == []
    assert session.memory_pending[0]["reverted_by"] is None
    run_cli(session, ["/revert c001", "y"])
    assert state(paths)[0]["value"] == "川崎"
