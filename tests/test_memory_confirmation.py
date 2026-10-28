"""MVP10: 保留を /confirm で確認し、AIが回答を判断し、Pythonが検証して状態遷移を実行し、根拠を確認ログに残す。"""

import io
import itertools
import json

import pytest

from ai_context_memory import cli as cli_module
from ai_context_memory import confirmation as confirmation_module
from ai_context_memory import llm as llm_module
from ai_context_memory import structured_memory as structured_module
from ai_context_memory.chat import ChatSession
from ai_context_memory.cli import run
from ai_context_memory.confirmation import (
    CONFIRMATION_ID_PATTERN,
    ConfirmationError,
    append_confirmation,
    complete_plan,
    judgment_problem,
    load_confirmations,
    plan_resolution,
)
from ai_context_memory.history import load_history
from ai_context_memory.llm import (
    format_confirmation_request,
    format_memory_state,
    format_prompt,
    format_search_request,
)
from ai_context_memory.structured_memory import (
    PENDING_KEYS,
    PartialWriteError,
    append_text,
    apply_candidates,
    attribute_base,
    load_memory_history,
    load_memory_pending,
    load_memory_state,
    save_memory_pending,
    save_memory_state,
)

T0 = "2026-10-01T09:00:00+09:00"
T1 = "2026-10-14T10:00:00+09:00"

KAWASAKI = {
    "subject": "user",
    "key": "home_location",
    "value": "川崎",
    "evidence": "今は川崎に住んでいます。",
    "updated_at": T0,
}
QUESTION = "川崎の住まいも残っていますか？"
NAGOYA_TEXT = "今は名古屋に住んでいます。"


def pending_item(pending_id="p001", key="home_location", old="川崎", new="名古屋", evidence=NAGOYA_TEXT,
                 question=QUESTION, **fields):
    """保留の1項目。保存形式のすべてのキーを持つ。"""
    item = {k: None for k in PENDING_KEYS}
    item.update(
        id=pending_id,
        subject="user",
        target_key=key,
        target_value=old,
        target_updated_at=T0,
        proposed_value=new,
        evidence=evidence,
        decision="replace",
        hold_reason="old_value_evidence に既存の値が含まれていません",
        reason="判断理由です。",
        confirmation_question=question,
        status="unresolved",
        created_at=T1,
    )
    item.update(fields)
    return item


P001 = pending_item()

PEAR = {"subject": "user", "key": "favorite_fruit", "value": "梨", "evidence": "好きな果物は梨です。", "updated_at": T0}
APPLE_PENDING = pending_item(
    "p002", "favorite_fruit", "梨", "りんご", "最近は好きな果物はりんごです。", "梨も今も好きですか？"
)
A_COMPANY = {"subject": "user", "key": "employer", "value": "A社", "evidence": "A社で働いています。", "updated_at": T0}
B_PENDING = pending_item("p003", "employer", "A社", "B社", "今はB社で働いています。", "A社にも今も勤めていますか？")


def judge(outcome, evidence, pending_id="p001", **fields):
    """確認の判断AIの出力。outcome に合う既定値を入れる。"""
    defaults = {
        "coexist": ("continues", "stands", True),
        "replace": ("ended", "stands", True),
        "unresolved": ("unknown", "stands", True),
        "not_an_answer": ("unknown", "stands", False),
    }.get(outcome, ("unknown", "stands", True))
    result = {
        "pending_id": pending_id,
        "answers_question": defaults[2],
        "old_value_status": defaults[0],
        "new_statement_status": defaults[1],
        "outcome": outcome,
        "answer_evidence": evidence,
        "reason": "判断理由です。",
    }
    result.update(fields)
    return json.dumps(result, ensure_ascii=False)


NOTHING_NEEDED = json.dumps(
    {
        "needs_memory": False,
        "queries": [],
        "needs_long_term_memory_history": False,
        "memory_history_queries": [],
        "needs_working_memory_history": False,
        "history_queries": [],
    }
)


def plan(queries=(), history=()):
    return json.dumps(
        {
            "needs_memory": bool(queries),
            "queries": list(queries),
            "needs_long_term_memory_history": bool(history),
            "memory_history_queries": list(history),
            "needs_working_memory_history": False,
            "history_queries": [],
        },
        ensure_ascii=False,
    )


class FakeLLM:
    """呼び出しの種類ごとに記録し、用意された応答を順に返す。"""

    def __init__(self, judgments=(), replies=(), plans=(), structured=()):
        self.judgments = list(judgments)
        self.replies = list(replies)
        self.plans = list(plans)
        self.structured = list(structured)
        self.calls = []
        self.prompts = []
        self.judge_inputs = []

    def complete(self, messages, *context):
        self.calls.append("complete")
        self.prompts.append(format_prompt(messages, *context))
        return self.replies.pop(0) if self.replies else "はい。"

    def plan_search(self, user_text, previous_queries=(), working_memory=None):
        self.calls.append("plan_search")
        format_search_request(user_text, previous_queries, working_memory)
        return self.plans.pop(0) if self.plans else NOTHING_NEEDED

    def extract_memories(self, user_text):
        self.calls.append("extract_memories")
        return "[]"

    def extract_structured_memory(self, user_text, items):
        self.calls.append("extract_structured_memory")
        return self.structured.pop(0) if self.structured else "[]"

    def extract_working_memory(self, user_text, working_memory):
        self.calls.append("extract_working_memory")
        return "[]"

    def judge_confirmation(self, pending, current, question, answer):
        self.calls.append("judge_confirmation")
        self.judge_inputs.append(format_confirmation_request(pending, current, question, answer))
        response = self.judgments.pop(0)
        if isinstance(response, Exception):
            raise response
        return response


@pytest.fixture
def clock(monkeypatch):
    minutes = itertools.count(1)
    monkeypatch.setattr(
        "ai_context_memory.chat.timestamp_now", lambda: f"2026-10-17T10:{next(minutes):02d}:00+09:00"
    )


def make_paths(tmp_path, state=(KAWASAKI,), pending=(P001,)):
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
    ]
    save_memory_state(files[4], list(state))
    save_memory_pending(files[6], list(pending))
    return files


@pytest.fixture
def paths(tmp_path, clock):
    """現在値 home_location = 川崎、保留 p001「今は名古屋に住んでいます。」。"""
    return make_paths(tmp_path)


class Stopped(Exception):
    """入力が尽きた。exit も取り消しもせずにプロセスが止まった場合として扱う。"""


def run_cli(session, inputs):
    it = iter(inputs)
    outputs = []

    def read(prompt):
        try:
            return next(it)
        except StopIteration:
            raise Stopped from None

    try:
        run(session, input_fn=read, output_fn=outputs.append)
    except Stopped:
        pass
    return outputs


def log_events(paths):
    return [e["event"] for e in load_confirmations(paths[7])]


def state(paths):
    return load_memory_state(paths[4])


def pending(paths, pending_id="p001"):
    return next(p for p in load_memory_pending(paths[6]) if p["id"] == pending_id)


def history(paths):
    return load_memory_history(paths[5]) if paths[5].exists() else []


def unchanged(paths, state_items=(KAWASAKI,)):
    assert state(paths) == list(state_items)
    assert pending(paths)["status"] == "unresolved"
    assert history(paths) == []


# --- 確認の開始 ---


def test_confirm_presents_the_saved_question_and_logs_it_first(paths):
    llm = FakeLLM()
    session = ChatSession(llm, *paths)

    outputs = run_cli(session, ["/confirm p001"])

    assert "\nAI> 川崎の住まいも残っていますか？" in outputs
    assert session.active_confirmation["pending_id"] == "p001"
    prepared, presented = load_confirmations(paths[7])
    assert (prepared["event"], presented["event"]) == ("prepared", "presented")
    assert prepared["confirmation_id"] == presented["confirmation_id"] == "c001"
    assert prepared["pending_id"] == presented["pending_id"] == "p001"
    assert prepared["current"] == KAWASAKI
    # 生成された質問と、表示する質問を分けて残す
    assert prepared["generated_question"] == QUESTION
    assert prepared["question"] == QUESTION
    assert prepared["question_source"] == "pending"
    assert prepared["generated_at"] == T1
    # presented は出力処理が終わった後に、実際に出力した質問で書く
    assert presented["question"] == QUESTION
    assert presented["question_source"] == "pending"
    # 質問は保存済みのものを使い、Claude は呼ばない
    assert llm.calls == []
    assert load_history(paths[0]) == [{"role": "assistant", "content": QUESTION}]
    assert json.loads(paths[0].read_text(encoding="utf-8"))["confirmation_id"] == "c001"


def test_question_that_does_not_mention_the_old_value_is_replaced_by_the_neutral_one(tmp_path, clock):
    paths = make_paths(tmp_path, pending=[pending_item(question="名古屋にはいつから住んでいますか？")])

    outputs = run_cli(ChatSession(FakeLLM(), *paths), ["/confirm p001"])

    assert "\n確認> 以前の「川崎」は今も有効ですか？" in outputs
    prepared, presented = load_confirmations(paths[7])
    assert prepared["generated_question"] == "名古屋にはいつから住んでいますか？"
    assert prepared["question"] == presented["question"] == "以前の「川崎」は今も有効ですか？"
    assert presented["question_source"] == "template"


# --- 質問の準備（prepared）と出力（presented） ---


def run_with_output(session, inputs, output_fn):
    """run_cli と同じく入力が尽きたら止まる。出力先だけを差し替える。"""
    it = iter(inputs)

    def read(prompt):
        try:
            return next(it)
        except StopIteration:
            raise Stopped from None

    try:
        run(session, input_fn=read, output_fn=output_fn)
    except Stopped:
        pass


def test_presented_and_the_conversation_are_written_only_after_the_question_is_output(paths):
    """質問を出力している最中には、まだ presented も会話原文も回答待ちも無い。"""
    session = ChatSession(FakeLLM(), *paths)
    seen = {}

    def output(line):
        if line.startswith("\nAI> "):
            seen["events"] = log_events(paths)
            seen["conversation"] = paths[0].exists()
            seen["waiting"] = session.active_confirmation
            seen["messages"] = list(session.messages)

    run_with_output(session, ["/confirm p001"], output)

    assert seen == {"events": ["prepared"], "conversation": False, "waiting": None, "messages": []}
    assert log_events(paths) == ["prepared", "presented"]
    assert session.active_confirmation["confirmation_id"] == "c001"


class OutputFailure(Exception):
    pass


def fail_on_question(outputs):
    """質問の行を出力しようとしたときに失敗する出力先。"""

    def output(line):
        if "川崎の住まいも残っていますか？" in line:
            raise OutputFailure("端末に書き込めません")
        outputs.append(line)

    return output


def test_question_that_fails_to_be_output_is_not_presented(paths):
    session = ChatSession(FakeLLM(), *paths)

    with pytest.raises(OutputFailure):
        run_with_output(session, ["/confirm p001"], fail_on_question([]))

    events = load_confirmations(paths[7])
    assert [e["event"] for e in events] == ["prepared", "cancelled"]
    assert events[1]["reason"] == "質問を出力できませんでした"
    # 表示できなかった質問は、会話原文にもAIの発言にも残さず、回答待ちにもしない
    assert not paths[0].exists()
    assert session.messages == []
    assert session.active_confirmation is None
    assert session.prepared_confirmation is None


def test_next_input_in_the_same_session_after_an_output_failure_is_not_an_answer(paths):
    llm = FakeLLM()
    session = ChatSession(llm, *paths)
    with pytest.raises(OutputFailure):
        run_with_output(session, ["/confirm p001"], fail_on_question([]))

    run_cli(session, ["いいえ、もうありません。"])

    assert "judge_confirmation" not in llm.calls
    assert "complete" in llm.calls
    unchanged(paths)
    assert log_events(paths) == ["prepared", "cancelled"]


def test_restart_after_an_output_failure_does_not_resume(paths):
    with pytest.raises(OutputFailure):
        run_with_output(ChatSession(FakeLLM(), *paths), ["/confirm p001"], fail_on_question([]))

    llm = FakeLLM()
    restarted = ChatSession(llm, *paths)
    run_cli(restarted, ["いいえ、もうありません。"])

    assert restarted.startup_messages == []
    assert "judge_confirmation" not in llm.calls
    unchanged(paths)


def test_failure_to_record_presented_does_not_wait_for_an_answer(paths, monkeypatch):
    original = confirmation_module.append_confirmation

    def fail_on_presented(path, event):
        if event["event"] == "presented":
            raise ConfirmationError("確認ログを保存できませんでした")
        original(path, event)

    monkeypatch.setattr("ai_context_memory.chat.append_confirmation", fail_on_presented)
    llm = FakeLLM()
    session = ChatSession(llm, *paths)

    outputs = run_cli(session, ["/confirm p001", "いいえ、もうありません。"])

    assert any("質問を表示したことを記録できなかったため、確認を中止しました" in o for o in outputs)
    assert session.active_confirmation is None
    conversation = [json.loads(line) for line in paths[0].read_text(encoding="utf-8").splitlines()]
    assert not any(m.get("confirmation_id") for m in conversation)
    assert "judge_confirmation" not in llm.calls
    assert log_events(paths) == ["prepared"]


def test_crash_after_prepared_is_closed_safely_on_restart(paths):
    """prepared を書いた直後にプロセスが止まった（出力も取り消しの記録もされていない）。"""
    ChatSession(FakeLLM(), *paths).start_confirmation("p001")
    assert log_events(paths) == ["prepared"]
    assert not paths[0].exists()

    llm = FakeLLM()
    restarted = ChatSession(llm, *paths)
    run_cli(restarted, ["いいえ、もうありません。"])

    assert restarted.startup_messages == [
        "c001（p001）: 前回の確認は質問を表示した記録が無いまま終わったため、続きは行いません（保留のままです）"
    ]
    assert log_events(paths) == ["prepared", "expired"]
    assert "judge_confirmation" not in llm.calls
    unchanged(paths)
    # もう一度起動しても何もしない
    assert ChatSession(FakeLLM(), *paths).startup_messages == []


def test_leftover_prepared_is_closed_when_another_confirmation_starts(paths):
    session = ChatSession(FakeLLM(), *paths)
    session.start_confirmation("p001")

    run_cli(session, ["/confirm p001"])

    assert log_events(paths) == ["prepared", "cancelled", "prepared", "presented"]
    assert session.active_confirmation["confirmation_id"] == "c002"


# --- 会話原文への質問の保存の失敗（presented の後） ---


def fail_conversation(monkeypatch, partial=False):
    """会話原文への追記を失敗させる。partial なら、途中までの行を書いて取り除けなかった場合にする。"""
    from ai_context_memory import history as history_module

    def fail(path, text):
        if partial:
            path.parent.mkdir(parents=True, exist_ok=True)
            with open(path, "a", encoding="utf-8") as f:
                f.write(text[: len(text) // 2])
            raise PartialWriteError(f"{path} への追記が途中で失敗し、途中まで書かれた内容を取り除けませんでした")
        raise OSError("ディスクに書き込めません")

    monkeypatch.setattr(history_module, "append_text", fail)


@pytest.mark.parametrize("partial", [False, True])
def test_conversation_failure_after_presented_does_not_wait_for_an_answer(paths, monkeypatch, partial):
    fail_conversation(monkeypatch, partial)
    llm = FakeLLM([judge("replace", "いいえ、もうありません。")])
    session = ChatSession(llm, *paths)

    outputs = run_cli(session, ["/confirm p001", "いいえ、もうありません。", "y"])

    # 出力処理は終わっているので presented は残る。会話原文に残せなかったので、確認はそこで閉じる
    events = load_confirmations(paths[7])
    assert [e["event"] for e in events] == ["prepared", "presented", "cancelled"]
    assert events[2]["reason"].startswith("会話原文に質問を保存できませんでした")
    assert session.active_confirmation is None
    assert session.messages[:1] != [{"role": "assistant", "content": QUESTION}]
    assert any("会話原文に質問を保存できなかったため、確認を中止しました" in o for o in outputs)
    # 同じセッションの次の入力は回答として扱わない
    assert "judge_confirmation" not in llm.calls
    unchanged(paths)
    # 提示回数には数える（出力処理は成功している）。ユーザーが読んだかは記録しない
    assert session.confirmation_attempts("p001") == (1, None)


def test_partially_written_conversation_blocks_further_confirmations(paths, monkeypatch):
    fail_conversation(monkeypatch, partial=True)
    session = ChatSession(FakeLLM(), *paths)

    outputs = run_cli(session, ["/confirm p001", "/confirm p001"])

    assert session.write_blocked
    assert any("長期状態の書き込みを停止しているため、確認できません" in o for o in outputs)
    assert log_events(paths) == ["prepared", "presented", "cancelled"]


def test_confirmation_is_refused_while_the_conversation_file_is_broken(paths):
    """会話原文の末尾に途中までの行が残っていたら、確認を始めない（起動はできる）。"""
    paths[0].parent.mkdir(parents=True, exist_ok=True)
    paths[0].write_text('{"role": "user", "content": "こんにちは"}\n{"role": "assistant", "con', encoding="utf-8")
    session = ChatSession(FakeLLM(), *paths)

    outputs = run_cli(session, ["/confirm p001"])

    assert any(o.startswith("[確認] 会話原文が読めないため、確認できません") for o in outputs)
    assert not paths[7].exists()
    assert session.active_confirmation is None


def test_restart_after_a_crash_between_presented_and_the_conversation_closes_safely(paths):
    """presented を書いた直後（会話原文へ書く前）にプロセスが止まった。"""
    session = ChatSession(FakeLLM(), *paths)
    started = session.start_confirmation("p001")
    session._log("presented", started["confirmation_id"], "p001", question=QUESTION, question_source="pending")

    llm = FakeLLM()
    restarted = ChatSession(llm, *paths)
    run_cli(restarted, ["いいえ、もうありません。"])

    assert restarted.startup_messages == ["c001（p001）: 前回の確認は完了していないため、続きは行いません（保留のままです）"]
    assert log_events(paths) == ["prepared", "presented", "expired"]
    assert "judge_confirmation" not in llm.calls
    unchanged(paths)


def test_restart_after_a_conversation_failure_does_not_resume(paths, monkeypatch):
    fail_conversation(monkeypatch)
    run_cli(ChatSession(FakeLLM(), *paths), ["/confirm p001"])
    monkeypatch.undo()

    llm = FakeLLM()
    restarted = ChatSession(llm, *paths)
    run_cli(restarted, ["いいえ、もうありません。"])

    assert restarted.startup_messages == []
    assert "judge_confirmation" not in llm.calls
    assert log_events(paths) == ["prepared", "presented", "cancelled"]
    unchanged(paths)


def test_conversation_append_is_rolled_back_when_it_fails_midway(tmp_path, monkeypatch):
    from ai_context_memory.history import HistoryError, append_history

    path = tmp_path / "conversation.jsonl"
    path.write_text('{"role": "user", "content": "こんにちは"}\n', encoding="utf-8")
    before = path.read_bytes()

    def fail(fd):
        raise OSError("書き込みに失敗しました")

    monkeypatch.setattr(structured_module.os, "fsync", fail)
    with pytest.raises(HistoryError):
        append_history(path, [{"role": "assistant", "content": QUESTION}])

    assert path.read_bytes() == before
    assert load_history(path) == [{"role": "user", "content": "こんにちは"}]


# --- 回答・承認の会話原文への保存の失敗 ---


def fail_conversation_line(monkeypatch, content, partial=False):
    """会話原文への追記のうち、content を含む行だけを失敗させる。それ以外は通常どおり書く。"""
    from ai_context_memory import history as history_module

    original = history_module.append_text
    marker = json.dumps({"content": content}, ensure_ascii=False)[1:-1]

    def append(path, text):
        if marker not in text:
            return original(path, text)
        if partial:
            with open(path, "a", encoding="utf-8") as f:
                f.write(text[: len(text) // 2])
            raise PartialWriteError(f"{path} への追記が途中で失敗し、途中まで書かれた内容を取り除けませんでした")
        raise OSError("ディスクに書き込めません")

    monkeypatch.setattr(history_module, "append_text", append)


ANSWER = "いいえ、もうありません。"


@pytest.mark.parametrize("partial", [False, True])
@pytest.mark.parametrize("outcome", ["coexist", "replace"])
def test_answer_that_cannot_be_saved_is_not_used(paths, monkeypatch, partial, outcome):
    """ケースA: 確認回答を会話原文に保存できなければ、その回答で確認を進めない。"""
    fail_conversation_line(monkeypatch, ANSWER, partial)
    llm = FakeLLM([judge(outcome, ANSWER)])
    session = ChatSession(llm, *paths)

    outputs = run_cli(session, ["/confirm p001", ANSWER, "y", "はい"])

    unchanged(paths)
    events = load_confirmations(paths[7])
    assert [e["event"] for e in events] == ["prepared", "presented", "cancelled"]
    assert events[2]["reason"].startswith("回答を会話原文に保存できませんでした")
    assert any("回答を会話原文に保存できなかったため、確認を中止しました" in o for o in outputs)
    # 保存できなかった回答は判断に使わず、次の入力も回答・承認として扱わない
    assert "judge_confirmation" not in llm.calls
    assert session.active_confirmation is None and session.awaiting_approval is None
    assert not any("（y/N）" in o for o in outputs)
    assert bool(session.write_blocked) is partial
    # 再起動しても続きを行わない
    restarted = ChatSession(FakeLLM(), *paths)
    run_cli(restarted, ["y"])
    assert restarted.startup_messages == []
    unchanged(paths)


@pytest.mark.parametrize("partial", [False, True])
def test_approval_that_cannot_be_saved_does_not_replace(paths, monkeypatch, partial):
    """ケースB: y を会話原文に保存できなければ、置換しない。"""
    fail_conversation_line(monkeypatch, "y", partial)
    llm = FakeLLM([judge("replace", ANSWER)])
    session = ChatSession(llm, *paths)

    outputs = run_cli(session, ["/confirm p001", ANSWER, "y", "y"])

    unchanged(paths)
    events = load_confirmations(paths[7])
    assert [e["event"] for e in events] == ["prepared", "presented", "judged", "cancelled"]
    assert events[3]["reason"].startswith("承認の入力を会話原文に保存できませんでした")
    assert any("承認の入力を会話原文に保存できなかったため、実行しませんでした" in o for o in outputs)
    assert session.awaiting_approval is None
    assert bool(session.write_blocked) is partial
    # 再起動後の復旧でも、保存できなかった承認で置換しない
    restarted = ChatSession(FakeLLM(), *paths)
    assert restarted.startup_messages == []
    run_cli(restarted, ["y"])
    unchanged(paths)
    assert log_events(paths) == ["prepared", "presented", "judged", "cancelled"]


def test_approval_prompt_that_cannot_be_saved_does_not_wait_for_approval(paths, monkeypatch):
    fail_conversation_line(monkeypatch, APPROVAL.removeprefix("[確認] "))
    session = ChatSession(FakeLLM([judge("replace", ANSWER)]), *paths)

    outputs = run_cli(session, ["/confirm p001", ANSWER, "y"])

    unchanged(paths)
    assert session.awaiting_approval is None
    assert log_events(paths) == ["prepared", "presented", "judged", "cancelled"]
    assert any("変更内容の確認を会話原文に保存できなかったため、実行しませんでした" in o for o in outputs)


def test_answer_line_is_saved_before_the_judgment(paths):
    """回答は判断の前に会話原文へ保存する。保存の直後に止まっても、再起動後は続きを行わない。"""
    seen = {}

    class Recording(FakeLLM):
        def judge_confirmation(self, *args):
            seen["conversation"] = load_history(paths[0])
            raise KeyboardInterrupt  # 判断の途中でプロセスが止まった

    session = ChatSession(Recording(), *paths)
    with pytest.raises(KeyboardInterrupt):
        session.start_confirmation("p001")
        session.mark_presented("c001")
        session.answer_confirmation(ANSWER)

    assert seen["conversation"] == [{"role": "assistant", "content": QUESTION}, {"role": "user", "content": ANSWER}]
    restarted = ChatSession(FakeLLM(), *paths)
    run_cli(restarted, ["y"])
    unchanged(paths)


def test_crash_after_the_approval_is_saved_does_not_replace_on_restart(paths):
    """y を会話原文に保存した直後（approval を書く前）に止まった。"""
    session = ChatSession(FakeLLM([judge("replace", ANSWER)]), *paths)
    run_cli(session, ["/confirm p001", ANSWER])
    from ai_context_memory.history import append_history

    append_history(paths[0], [{"role": "user", "content": "y", "confirmation_id": "c001", "confirmation_event": "approval"}])

    restarted = ChatSession(FakeLLM(), *paths)

    assert restarted.startup_messages == ["c001（p001）: 前回の確認は完了していないため、続きは行いません（保留のままです）"]
    unchanged(paths)
    assert log_events(paths) == ["prepared", "presented", "judged", "expired"]


def test_commit_is_refused_while_writes_are_blocked(paths):
    session = ChatSession(FakeLLM([judge("replace", ANSWER)]), *paths)
    run_cli(session, ["/confirm p001", ANSWER])
    session.write_blocked = "テスト用に停止"

    outputs = run_cli(session, ["y"])

    unchanged(paths)
    assert any("長期状態の書き込みを停止しているため、実行しませんでした" in o for o in outputs)


def append_events(paths, confirmation_id, *names):
    for name in names:
        append_confirmation(
            paths[7],
            {"event": name, "confirmation_id": confirmation_id, "pending_id": "p001", "timestamp": T1,
             "question": QUESTION, "question_source": "pending", "answer": "はい", "accepted": False, "outcome": None},
        )


def test_attempts_count_only_confirmations_whose_question_was_output(paths):
    append_events(paths, "c001", "prepared", "cancelled")  # 出力できなかった
    append_events(paths, "c002", "prepared", "expired")  # 出力する前に止まった
    append_events(paths, "c003", "prepared", "presented", "judged")  # 出力した
    append_events(paths, "c004", "presented", "judged")  # 旧形式。回答があるので出力処理は終わっていた
    append_events(paths, "c005", "presented", "expired")  # 旧形式。出力したかは分からない
    session = ChatSession(FakeLLM(), *paths)

    assert session.confirmation_attempts("p001") == (2, "rejected")
    outputs = run_cli(session, ["/pending", "/pending p001", "/confirm p001"])

    assert "[長期保留]   /confirm p001 で確認できます（これまでの確認 2回、前回の判断: rejected）" in outputs
    assert "[確認] p001 user.home_location（確認 c006、3回目）" in outputs
    details = [o for o in outputs if o.startswith("[確認記録]")]
    assert any(o.startswith("[確認記録] c001 prepared") and "表示したことは意味しない" in o for o in details)
    assert any(o.startswith("[確認記録] c003 presented") and "出力処理が正常に終了" in o for o in details)
    assert any(o.startswith("[確認記録] c005 presented") and "旧形式: 出力に成功したかは記録されていない" in o for o in details)


def test_question_is_not_shown_if_it_cannot_be_logged(paths, monkeypatch):
    def fail(path, event):
        raise ConfirmationError("確認ログを保存できませんでした")

    monkeypatch.setattr("ai_context_memory.chat.append_confirmation", fail)
    session = ChatSession(FakeLLM(), *paths)

    outputs = run_cli(session, ["/confirm p001"])

    assert not any("AI>" in o for o in outputs)
    assert "[確認] 確認ログを保存できませんでした" in outputs
    assert session.active_confirmation is None


@pytest.mark.parametrize(
    "command, message",
    [
        ("/confirm p999", "[確認] 保留 p999 はありません"),
        ("/confirm 001", "[確認] 保留の ID の形式が不正です: 001（例: /confirm p001）"),
        ("/confirm", "[確認] 確認する保留の ID を指定してください（例: /confirm p001）。一覧は /pending"),
    ],
)
def test_unknown_or_malformed_pending_id_does_not_start(paths, command, message):
    session = ChatSession(FakeLLM(), *paths)

    outputs = run_cli(session, [command])

    assert message in outputs
    assert session.active_confirmation is None
    assert not paths[7].exists()


def test_other_slash_text_is_still_a_normal_message(paths):
    llm = FakeLLM()

    run_cli(ChatSession(llm, *paths), ["/confirmation について教えて"])

    assert "complete" in llm.calls
    assert not paths[7].exists()


def test_answer_after_restart_is_not_bound_to_the_old_question(paths):
    """確認を始めて再起動すると、前回の質問への回答は受け付けない。"""
    run_cli(ChatSession(FakeLLM(), *paths), ["/confirm p001"])  # exit せずに終わる（プロセスが落ちた想定）

    llm = FakeLLM()
    restarted = ChatSession(llm, *paths)
    run_cli(restarted, ["いいえ、もうありません。"])

    assert restarted.startup_messages == [
        "c001（p001）: 前回の確認は完了していないため、続きは行いません（保留のままです）"
    ]
    assert log_events(paths) == ["prepared", "presented", "expired"]
    assert "judge_confirmation" not in llm.calls
    assert "complete" in llm.calls
    unchanged(paths)


def test_exit_while_waiting_for_the_answer_cancels(paths):
    run_cli(ChatSession(FakeLLM(), *paths), ["/confirm p001", "exit"])

    assert log_events(paths) == ["prepared", "presented", "cancelled"]
    assert ChatSession(FakeLLM(), *paths).startup_messages == []


def test_command_while_waiting_cancels_and_is_not_an_answer(paths):
    llm = FakeLLM()

    outputs = run_cli(ChatSession(llm, *paths), ["/confirm p001", "/pending", "いいえ"])

    assert "[確認] 確認を取り消しました。保留はそのままです" in outputs
    assert log_events(paths) == ["prepared", "presented", "cancelled"]
    # 取り消した後の「いいえ」は通常の会話
    assert "judge_confirmation" not in llm.calls
    unchanged(paths)


def test_cancel_command(paths):
    outputs = run_cli(ChatSession(FakeLLM(), *paths), ["/cancel", "/confirm p001", "/cancel"])

    assert outputs.count("[確認] 確認中ではありません") == 2
    assert log_events(paths) == ["prepared", "presented", "cancelled"]


def test_empty_input_keeps_waiting(paths):
    llm = FakeLLM([judge("coexist", "はい、残っています。")])

    run_cli(ChatSession(llm, *paths), ["/confirm p001", "", "はい、残っています。"])

    assert pending(paths)["status"] == "resolved"


# --- 共存 ---


def test_coexist_adds_a_numbered_key_and_keeps_the_old_value(paths):
    llm = FakeLLM([judge("coexist", "はい、残っています。")])

    outputs = run_cli(ChatSession(llm, *paths), ["/confirm p001", "はい、残っています。"])

    nagoya = {
        "subject": "user",
        "key": "home_location_2",
        "value": "名古屋",
        "evidence": NAGOYA_TEXT,
        "updated_at": "2026-10-17T10:03:00+09:00",
    }
    assert state(paths) == [KAWASAKI, nagoya]
    assert history(paths) == [
        {
            "timestamp": "2026-10-17T10:03:00+09:00",
            "operation": "add",
            "subject": "user",
            "key": "home_location_2",
            "value": "名古屋",
            "evidence": NAGOYA_TEXT,
            "pending_id": "p001",
            "confirmation_id": "c001",
        }
    ]
    resolved = pending(paths)
    assert (resolved["status"], resolved["resolution"], resolved["resolved_key"], resolved["confirmation_id"]) == (
        "resolved",
        "coexist",
        "home_location_2",
        "c001",
    )
    assert log_events(paths) == ["prepared", "presented", "judged", "commit", "applied"]
    # 共存は既存の値を消さないので、y/N は聞かない
    assert not any("（y/N）" in o for o in outputs)
    assert (
        "[長期状態] user.home_location_2: 名古屋（確認 c001 で p001 を解決。既存の値はそのままです）" in outputs
    )
    # 回答のターンは判断の1回だけで、通常回答・各抽出は行わない
    assert llm.calls == ["judge_confirmation"]


def test_coexist_uses_the_next_free_number(tmp_path, clock):
    osaka = {**KAWASAKI, "key": "home_location_2", "value": "大阪", "evidence": "大阪にも家があります。"}
    paths = make_paths(tmp_path, state=[KAWASAKI, osaka])

    run_cli(ChatSession(FakeLLM([judge("coexist", "はい")]), *paths), ["/confirm p001", "はい"])

    assert [item["key"] for item in state(paths)] == ["home_location", "home_location_2", "home_location_3"]


def test_coexist_with_the_value_already_present_resolves_without_a_duplicate(tmp_path, clock):
    nagoya = {**KAWASAKI, "key": "home_location_2", "value": "名古屋", "evidence": "名古屋にも家があります。"}
    paths = make_paths(tmp_path, state=[KAWASAKI, nagoya])

    outputs = run_cli(ChatSession(FakeLLM([judge("coexist", "はい")]), *paths), ["/confirm p001", "はい"])

    assert state(paths) == [KAWASAKI, nagoya]
    assert history(paths) == []
    assert pending(paths)["resolved_key"] == "home_location_2"
    assert "[長期状態] user.home_location_2 に「名古屋」が既にあるため変更しません（確認 c001 で p001 を解決）" in outputs


# --- 置換と実行前の確認 ---

APPROVAL = (
    "[確認] 「川崎」を user.home_location の現在の値から外し、「名古屋」を現在の値として登録します"
    "（「川崎」は変更履歴に残ります）。実行しますか？（y/N）"
)


def test_replace_shows_the_change_and_waits_without_changing_anything(paths):
    llm = FakeLLM([judge("replace", "いいえ、もうありません。")])
    session = ChatSession(llm, *paths)

    outputs = run_cli(session, ["/confirm p001", "いいえ、もうありません。"])

    assert outputs[-1] == APPROVAL
    assert session.awaiting_approval is not None
    unchanged(paths)
    assert log_events(paths) == ["prepared", "presented", "judged"]


def test_replace_is_executed_after_y(paths):
    llm = FakeLLM([judge("replace", "いいえ、もうありません。")])

    outputs = run_cli(ChatSession(llm, *paths), ["/confirm p001", "いいえ、もうありません。", "y"])

    assert state(paths) == [
        # 変更計画は承認した時点の状態で作り直すので、更新時刻は承認した時刻
        {**KAWASAKI, "value": "名古屋", "evidence": NAGOYA_TEXT, "updated_at": "2026-10-17T10:06:00+09:00"}
    ]
    assert history(paths) == [
        {
            "timestamp": "2026-10-17T10:06:00+09:00",
            "operation": "replace",
            "subject": "user",
            "key": "home_location",
            "old_value": "川崎",
            "value": "名古屋",
            "evidence": NAGOYA_TEXT,
            "pending_id": "p001",
            "confirmation_id": "c001",
        }
    ]
    assert pending(paths)["resolution"] == "replace"
    assert log_events(paths) == ["prepared", "presented", "judged", "approval", "commit", "applied"]
    approval = load_confirmations(paths[7])[3]
    assert approval["approved"] is True
    assert approval["input"] == "y"
    assert approval["prompt"] == APPROVAL.removeprefix("[確認] ")
    assert "[長期状態] user.home_location: 川崎 → 名古屋（確認 c001 で p001 を解決）" in outputs
    # 承認に Claude は呼ばない
    assert llm.calls == ["judge_confirmation"]


@pytest.mark.parametrize("answer", ["N", "n", "yes", "はい", "y を押します", "/pending", "/confirm p001", "1+1は？"])
def test_replace_is_not_executed_without_y(paths, answer):
    llm = FakeLLM([judge("replace", "いいえ、もうありません。")])
    session = ChatSession(llm, *paths)

    outputs = run_cli(session, ["/confirm p001", "いいえ、もうありません。", answer])

    unchanged(paths)
    assert log_events(paths) == ["prepared", "presented", "judged", "approval"]
    assert load_confirmations(paths[7])[3]["approved"] is False
    assert "[確認] 実行しませんでした。p001 は保留のままです" in outputs
    assert session.awaiting_approval is None
    assert llm.calls == ["judge_confirmation"]


def test_exit_while_waiting_for_approval_cancels(paths):
    run_cli(ChatSession(FakeLLM([judge("replace", "いいえ")]), *paths), ["/confirm p001", "いいえ", "exit"])

    unchanged(paths)
    assert log_events(paths) == ["prepared", "presented", "judged", "cancelled"]


def test_unapproved_replace_is_not_reused_after_restart(paths):
    run_cli(ChatSession(FakeLLM([judge("replace", "いいえ")]), *paths), ["/confirm p001", "いいえ"])

    llm = FakeLLM()
    restarted = ChatSession(llm, *paths)
    run_cli(restarted, ["y"])

    assert restarted.awaiting_approval is None
    assert log_events(paths) == ["prepared", "presented", "judged", "expired"]
    assert "complete" in llm.calls
    unchanged(paths)


def test_conversation_keeps_the_actual_exchange_and_matches_the_log(paths):
    run_cli(
        ChatSession(FakeLLM([judge("replace", "いいえ、もうありません。")]), *paths),
        ["/confirm p001", "いいえ、もうありません。", "y"],
    )

    lines = [json.loads(line) for line in paths[0].read_text(encoding="utf-8").splitlines()]
    assert [(m["role"], m["content"], m["confirmation_event"]) for m in lines] == [
        ("assistant", QUESTION, "presented"),
        ("user", "いいえ、もうありません。", "answer"),
        ("assistant", APPROVAL.removeprefix("[確認] "), "approval_prompt"),
        ("user", "y", "approval"),
    ]
    assert {m["confirmation_id"] for m in lines} == {"c001"}
    events = load_confirmations(paths[7])
    assert [e["event"] for e in events[:4]] == ["prepared", "presented", "judged", "approval"]
    assert events[1]["question"] == lines[0]["content"]
    assert events[2]["answer"] == lines[1]["content"]
    assert events[3]["input"] == lines[3]["content"]


# --- 不明・回答ではない ---


def test_unresolved_keeps_the_pending(paths):
    llm = FakeLLM([judge("unresolved", "まだ分かりません。")])
    session = ChatSession(llm, *paths)

    outputs = run_cli(session, ["/confirm p001", "まだ分かりません。", "/pending"])

    unchanged(paths)
    judged = load_confirmations(paths[7])[2]
    assert (judged["accepted"], judged["outcome"], judged["plan"]) == (True, "unresolved", None)
    assert "[確認] p001 は保留のままにしました（判断: 不明）" in outputs
    assert "[長期保留]   /confirm p001 で確認できます（これまでの確認 1回、前回の判断: unresolved）" in outputs
    # 同じ質問を自分からは繰り返さない。もう一度確認するかは利用者が決める
    assert llm.calls == ["judge_confirmation"]


def test_unresolved_can_be_confirmed_again_by_the_user(paths):
    llm = FakeLLM([judge("unresolved", "まだ分かりません。"), judge("coexist", "残っています。")])

    outputs = run_cli(
        ChatSession(llm, *paths), ["/confirm p001", "まだ分かりません。", "/confirm p001", "残っています。"]
    )

    assert "[確認] p001 user.home_location（確認 c002、2回目）" in outputs
    assert pending(paths)["confirmation_id"] == "c002"


def test_withdrawal_judged_unresolved_is_kept_and_reported(paths):
    llm = FakeLLM([judge("unresolved", "名古屋は言い間違いでした。", new_statement_status="withdrawn")])

    outputs = run_cli(ChatSession(llm, *paths), ["/confirm p001", "名古屋は言い間違いでした。"])

    unchanged(paths)
    assert (
        "[確認] 回答は新しい発言の取り消しと判断されました。取り消しは MVP10 では扱えないため、保留のままです"
        in outputs
    )


@pytest.mark.parametrize("outcome", ["coexist", "replace"])
def test_withdrawal_misjudged_as_coexist_or_replace_is_not_applied(paths, outcome):
    """AIが撤回を共存・置換と誤り、自己申告もそれに合わせた場合でも、新しい値と訂正の語があれば実行しない。"""
    answer = "名古屋は言い間違いでした。"
    llm = FakeLLM([judge(outcome, answer)])

    outputs = run_cli(ChatSession(llm, *paths), ["/confirm p001", answer, "y"])

    unchanged(paths)
    judged = load_confirmations(paths[7])[2]
    assert judged["accepted"] is False
    assert "撤回は MVP10 では扱いません" in judged["rejection_reason"]
    assert not any("（y/N）" in o for o in outputs)


def test_not_an_answer_returns_to_normal_conversation(paths):
    llm = FakeLLM([judge("not_an_answer", None)], replies=["2です。"])
    session = ChatSession(llm, *paths)

    outputs = run_cli(session, ["/confirm p001", "1+1は？"])

    unchanged(paths)
    assert log_events(paths) == ["prepared", "presented", "judged"]
    assert "\nAI> 2です。" in outputs
    # 判断の1回の後、通常のターンと同じ呼び出しが続く
    assert llm.calls == [
        "judge_confirmation",
        "plan_search",
        "complete",
        "extract_memories",
        "extract_structured_memory",
        "extract_working_memory",
    ]
    # 会話原文には、質問と、通常のターンの発言と応答がそれぞれ1回ずつ残る
    assert load_history(paths[0]) == [
        {"role": "assistant", "content": QUESTION},
        {"role": "user", "content": "1+1は？"},
        {"role": "assistant", "content": "2です。"},
    ]


# --- 対象の取り違え ---


def test_only_the_confirmed_pending_is_resolved_when_there_are_several(tmp_path, clock):
    paths = make_paths(tmp_path, state=[KAWASAKI, PEAR], pending=[P001, APPLE_PENDING])
    llm = FakeLLM([judge("coexist", "はい、梨も好きです。", pending_id="p002")])

    run_cli(ChatSession(llm, *paths), ["/confirm p002", "はい、梨も好きです。"])

    assert [(i["key"], i["value"]) for i in state(paths)] == [
        ("home_location", "川崎"),
        ("favorite_fruit", "梨"),
        ("favorite_fruit_2", "りんご"),
    ]
    assert pending(paths, "p001")["status"] == "unresolved"
    assert pending(paths, "p002")["status"] == "resolved"
    # 判断AIには確認している保留だけを渡す
    [judge_input] = llm.judge_inputs
    assert "p002" in judge_input and "梨" in judge_input
    assert "p001" not in judge_input and "名古屋" not in judge_input and "川崎" not in judge_input


def test_judgment_naming_another_pending_is_rejected(tmp_path, clock):
    paths = make_paths(tmp_path, state=[KAWASAKI, PEAR], pending=[P001, APPLE_PENDING])
    llm = FakeLLM([judge("coexist", "はい", pending_id="p001")])

    outputs = run_cli(ChatSession(llm, *paths), ["/confirm p002", "はい"])

    assert state(paths) == [KAWASAKI, PEAR]
    assert pending(paths, "p001")["status"] == pending(paths, "p002")["status"] == "unresolved"
    assert any("判断の pending_id が確認中の保留（p002）と違います: p001" in o for o in outputs)


# --- 古くなった保留・途中での変更 ---


@pytest.mark.parametrize(
    "current, message",
    [
        ({**KAWASAKI, "value": "横浜"}, "保留した後に user.home_location の値が「川崎」から「横浜」に変わっています"),
        ({**KAWASAKI, "updated_at": "2026-10-15T10:00:00+09:00"}, "保留した後に user.home_location が更新されています"),
    ],
)
def test_pending_made_stale_before_the_start_is_refused(tmp_path, clock, current, message):
    paths = make_paths(tmp_path, state=[current])

    outputs = run_cli(ChatSession(FakeLLM(), *paths), ["/confirm p001"])

    assert f"[確認] p001 は確認できません: {message}" in outputs
    assert not paths[7].exists()


def test_mvp9_pending_without_target_updated_at_is_checked_against_created_at(tmp_path, clock):
    later = {**KAWASAKI, "updated_at": "2026-10-15T10:00:00+09:00"}
    paths = make_paths(tmp_path, state=[later], pending=[pending_item(target_updated_at=None)])

    outputs = run_cli(ChatSession(FakeLLM(), *paths), ["/confirm p001"])

    assert "[確認] p001 は確認できません: 保留した後に user.home_location が更新されています" in outputs


def test_current_changed_on_disk_after_the_start_is_not_overwritten(paths):
    llm = FakeLLM([judge("coexist", "はい")])
    session = ChatSession(llm, *paths)
    run_cli(session, ["/confirm p001"])
    yokohama = [{**KAWASAKI, "value": "横浜"}]
    save_memory_state(paths[4], yokohama)  # 別の手段でファイルが書き換えられた

    outputs = run_cli(session, ["はい"])

    assert state(paths) == yokohama
    assert pending(paths)["status"] == "unresolved"
    assert any("このセッションの外で変更されています" in o for o in outputs)


def test_current_changed_in_memory_between_answer_and_approval_is_not_overwritten(paths):
    session = ChatSession(FakeLLM([judge("replace", "いいえ")]), *paths)
    run_cli(session, ["/confirm p001", "いいえ"])
    session.memory_state = [{**KAWASAKI, "value": "横浜"}]

    outputs = run_cli(session, ["y"])

    assert state(paths) == [KAWASAKI]
    assert pending(paths)["status"] == "unresolved"
    assert log_events(paths) == ["prepared", "presented", "judged", "approval", "failed"]
    assert any("[確認] 反映しませんでした:" in o for o in outputs)


@pytest.mark.parametrize(
    "item, message",
    [
        (pending_item(new="名古屋市"), "新しい値「名古屋市」が保留の発言にありません"),
        (pending_item(new=None), "新しい値（proposed_value）が分からない保留は、MVP10 では確認できません"),
        (pending_item(key=None, old=None), "既存の項目（target_key）に関係しない保留は、MVP10 では確認できません"),
        (pending_item(status="resolved"), "p001 は既に解決済みです"),
    ],
)
def test_pending_that_cannot_be_resolved_safely_is_refused(tmp_path, clock, item, message):
    paths = make_paths(tmp_path, pending=[item])

    outputs = run_cli(ChatSession(FakeLLM(), *paths), ["/confirm p001"])

    assert f"[確認] p001 は確認できません: {message}" in outputs


def test_pending_list_shows_why_some_cannot_be_confirmed(tmp_path, clock):
    paths = make_paths(tmp_path, pending=[P001, pending_item("p002", new=None, evidence="引っ越しました。")])

    outputs = run_cli(ChatSession(FakeLLM(), *paths), ["/pending"])

    assert "[長期保留]   /confirm p001 で確認できます（これまでの確認 0回）" in outputs
    assert (
        "[長期保留]   確認できません: 新しい値（proposed_value）が分からない保留は、MVP10 では確認できません"
        "（これまでの確認 0回）" in outputs
    )


# --- 二重の適用 ---


def test_resolved_pending_cannot_be_confirmed_again(paths):
    llm = FakeLLM([judge("coexist", "はい")])

    outputs = run_cli(ChatSession(llm, *paths), ["/confirm p001", "はい", "/confirm p001"])

    assert "[確認] p001 は確認できません: p001 は既に解決済みです" in outputs
    assert llm.calls == ["judge_confirmation"]


def test_same_answer_twice_does_not_update_twice(paths):
    llm = FakeLLM([judge("coexist", "はい、残っています。")])

    run_cli(ChatSession(llm, *paths), ["/confirm p001", "はい、残っています。", "はい、残っています。"])

    assert [item["key"] for item in state(paths)] == ["home_location", "home_location_2"]
    assert len(history(paths)) == 1
    # 2回目の入力は通常の会話として扱われる
    assert llm.calls.count("judge_confirmation") == 1


def test_complete_plan_is_idempotent(tmp_path):
    paths = make_paths(tmp_path)
    plan_ = plan_resolution("replace", P001, [KAWASAKI], "2026-10-17T10:00:00+09:00")
    args = (paths[4], paths[5], paths[6])

    state1, pending1 = complete_plan("c001", plan_, [KAWASAKI], [P001], *args)
    state2, pending2 = complete_plan("c001", plan_, state1, pending1, *args)

    assert state1 == state2 == state(paths)
    assert pending1 == pending2
    assert len(history(paths)) == 1


# --- 不正な判断・呼び出しの失敗 ---


@pytest.mark.parametrize(
    "raw, reason",
    [
        ("住まいは残っているようです", "確認の判断結果がJSONとして読めません"),
        ("[]", "確認の判断結果がJSONオブジェクトではありません"),
        (judge("keep", "はい"), "outcome が不正です: keep"),
        (judge("coexist", "はい", answers_question="true"), "answers_question が true / false ではありません"),
        (judge("coexist", "はい", old_value_status=None), "old_value_status が不正です"),
        (judge("coexist", "はい", new_statement_status=None), "new_statement_status が不正です"),
        (judge("coexist", "残っています"), "answer_evidence が回答に含まれていません"),
        (judge("coexist", ""), "answer_evidence がありません"),
        (judge("replace", "はい", old_value_status="continues"), "判断が食い違っています"),
        (judge("coexist", "はい", old_value_status="ended"), "判断が食い違っています"),
        (judge("coexist", "はい", new_statement_status="unclear"), "判断が食い違っています"),
        (judge("not_an_answer", None, answers_question=True), "not_an_answer なのに answers_question が true"),
        (judge("coexist", "はい", answers_question=False), "answers_question が false なのに outcome が coexist"),
    ],
)
def test_invalid_judgment_changes_nothing(paths, raw, reason):
    llm = FakeLLM([raw])

    outputs = run_cli(ChatSession(llm, *paths), ["/confirm p001", "はい", "y"])

    unchanged(paths)
    judged = load_confirmations(paths[7])[2]
    assert judged["accepted"] is False
    assert reason in judged["rejection_reason"]
    assert judged["llm_output"] == raw
    assert any(o.startswith("[確認] 反映しませんでした:") for o in outputs)
    # 反映しなかった入力「y」は通常の会話（承認としては扱わない）
    assert log_events(paths) == ["prepared", "presented", "judged"]


def test_llm_failure_changes_nothing(paths):
    llm = FakeLLM([llm_module.LLMError("claude コマンドが異常終了しました")])

    outputs = run_cli(ChatSession(llm, *paths), ["/confirm p001", "はい"])

    unchanged(paths)
    judged = load_confirmations(paths[7])[2]
    assert (judged["accepted"], judged["llm_output"]) == (False, None)
    assert any("確認の判断を得られませんでした" in o for o in outputs)


def test_json_after_a_preamble_is_recovered_and_still_checked(paths):
    raw = "<reasoning>回答は肯定です</reasoning>\n" + judge("coexist", "はい")

    run_cli(ChatSession(FakeLLM([raw]), *paths), ["/confirm p001", "はい"])

    judged = load_confirmations(paths[7])[2]
    assert judged["recovered"] is True
    assert pending(paths)["status"] == "resolved"


# --- 書き込みの失敗と復旧 ---


def failing(function, times):
    """最初の times 回だけ OSError 相当の失敗をする関数。"""
    count = itertools.count()

    def wrapper(*args, **kwargs):
        if next(count) < times:
            raise structured_module.StructuredMemoryError("ディスクに書き込めません")
        return function(*args, **kwargs)

    return wrapper


def test_current_save_failure_changes_nothing(paths, monkeypatch):
    monkeypatch.setattr(confirmation_module, "save_memory_state", failing(save_memory_state, 1))

    outputs = run_cli(ChatSession(FakeLLM([judge("coexist", "はい")]), *paths), ["/confirm p001", "はい"])

    unchanged(paths)
    assert log_events(paths) == ["prepared", "presented", "judged", "commit", "failed"]
    assert any("[確認] 反映しませんでした: ディスクに書き込めません" in o for o in outputs)


def test_history_failure_is_completed_by_the_immediate_retry(paths, monkeypatch):
    original = structured_module.append_memory_history
    monkeypatch.setattr(confirmation_module, "append_memory_history", failing(original, 1))

    outputs = run_cli(ChatSession(FakeLLM([judge("coexist", "はい")]), *paths), ["/confirm p001", "はい"])

    assert [item["key"] for item in state(paths)] == ["home_location", "home_location_2"]
    assert len(history(paths)) == 1
    assert pending(paths)["status"] == "resolved"
    assert log_events(paths) == ["prepared", "presented", "judged", "commit", "applied"]
    assert load_confirmations(paths[7])[-1]["recovered"] is True
    assert any("続きを実行して反映しました" in o for o in outputs)


def test_persistent_pending_failure_blocks_writes_and_restart_completes_it(paths, monkeypatch):
    monkeypatch.setattr(confirmation_module, "save_memory_pending", failing(save_memory_pending, 99))
    session = ChatSession(FakeLLM([judge("coexist", "はい")]), *paths)

    outputs = run_cli(session, ["/confirm p001", "はい", "/confirm p001", "名古屋に住んでいます。"])

    # Current は書けたが、保留を解決済みにできなかった。自動では続けず、以後の書き込みを止める
    assert [item["key"] for item in state(paths)] == ["home_location", "home_location_2"]
    assert pending(paths)["status"] == "unresolved"
    assert session.write_blocked
    assert any("長期状態の書き込みを停止しました。再起動してください" in o for o in outputs)
    assert any("長期状態の書き込みを停止しているため、確認できません" in o for o in outputs)
    assert any("長期状態の書き込みを停止しています" in o for o in outputs)
    assert log_events(paths) == ["prepared", "presented", "judged", "commit"]

    monkeypatch.undo()
    restarted = ChatSession(FakeLLM(), *paths)

    assert restarted.startup_messages == ["c001（p001）: 前回途中で止まった変更を確認し、続きを実行して反映しました"]
    assert pending(paths)["status"] == "resolved"
    assert len(history(paths)) == 1
    assert log_events(paths) == ["prepared", "presented", "judged", "commit", "applied"]
    # もう一度起動しても何もしない
    assert ChatSession(FakeLLM(), *paths).startup_messages == []
    assert log_events(paths) == ["prepared", "presented", "judged", "commit", "applied"]


def commit_only(paths, operation="replace"):
    """変更計画を書いた直後にプロセスが落ちた確認ログを作る。"""
    plan_ = plan_resolution(operation, P001, [KAWASAKI], "2026-10-17T10:00:00+09:00")
    for event in (
        {"event": "presented", "question": QUESTION},
        {"event": "judged", "answer": "いいえ", "accepted": True, "outcome": operation},
        {"event": "approval", "approved": True, "input": "y"},
        {"event": "commit", "plan": plan_},
    ):
        append_confirmation(paths[7], {"confirmation_id": "c001", "pending_id": "p001", "timestamp": T1, **event})
    return plan_


def test_crash_before_current_is_written_is_marked_failed_on_restart(paths):
    commit_only(paths)

    restarted = ChatSession(FakeLLM(), *paths)

    assert restarted.startup_messages == ["c001（p001）: 前回途中で止まった変更を確認し、反映されていなかったため、実行しませんでした"]
    unchanged(paths)
    assert log_events(paths)[-1] == "failed"


def test_crash_after_current_is_written_is_rolled_forward_on_restart(paths):
    plan_ = commit_only(paths)
    save_memory_state(paths[4], [{**KAWASAKI, "value": "名古屋", "evidence": NAGOYA_TEXT, "updated_at": plan_["after"]["updated_at"]}])

    ChatSession(FakeLLM(), *paths)

    assert [e["operation"] for e in history(paths)] == ["replace"]
    assert history(paths)[0]["confirmation_id"] == "c001"
    assert pending(paths)["status"] == "resolved"
    assert log_events(paths)[-1] == "applied"


def test_unrecoverable_state_stops_the_startup(paths):
    commit_only(paths)
    save_memory_state(paths[4], [{**KAWASAKI, "value": "横浜"}])

    with pytest.raises(ConfirmationError, match="自動では復旧できません"):
        ChatSession(FakeLLM(), *paths)
    assert log_events(paths)[-1] == "commit"


def test_truncated_log_line_stops_the_startup(paths):
    append_confirmation(paths[7], {"event": "presented", "confirmation_id": "c001", "pending_id": "p001", "timestamp": T1})
    with open(paths[7], "a", encoding="utf-8") as f:
        f.write('{"event": "judged", "confirmation_id": "c00')

    with pytest.raises(ConfirmationError, match="最後の行が途中で途切れています"):
        ChatSession(FakeLLM(), *paths)


def test_failed_append_is_rolled_back(tmp_path, monkeypatch):
    path = tmp_path / "log.jsonl"
    path.write_text('{"a": 1}\n', encoding="utf-8")

    def fail(fd):
        raise OSError("書き込みに失敗しました")

    monkeypatch.setattr(structured_module.os, "fsync", fail)
    with pytest.raises(OSError):
        append_text(path, '{"b": 2}\n')

    assert path.read_text(encoding="utf-8") == '{"a": 1}\n'


def test_append_that_cannot_be_rolled_back_is_reported_and_blocks_writes(paths, monkeypatch):
    def fail(*args):
        raise OSError("書き込みに失敗しました")

    session = ChatSession(FakeLLM(), *paths)
    monkeypatch.setattr(structured_module.os, "fsync", fail)
    monkeypatch.setattr(structured_module.os, "truncate", fail)

    with pytest.raises(PartialWriteError):
        append_text(paths[7], '{"b": 2}\n')
    outputs = run_cli(session, ["/confirm p001"])

    assert any("取り除けませんでした" in o for o in outputs)
    assert session.write_blocked


def test_append_after_a_line_without_newline_starts_a_new_line(tmp_path):
    path = tmp_path / "log.jsonl"
    path.write_text('{"a": 1}', encoding="utf-8")

    append_text(path, '{"b": 2}\n')

    assert path.read_text(encoding="utf-8") == '{"a": 1}\n{"b": 2}\n'


def test_state_file_is_written_atomically(tmp_path, monkeypatch):
    path = tmp_path / "state.json"
    save_memory_state(path, [KAWASAKI])
    before = path.read_bytes()

    def fail(src, dst):
        raise OSError("置き換えに失敗しました")

    monkeypatch.setattr(structured_module.os, "replace", fail)
    with pytest.raises(structured_module.StructuredMemoryError):
        save_memory_state(path, [{**KAWASAKI, "value": "名古屋"}])

    assert path.read_bytes() == before
    assert not (tmp_path / "state.json.tmp").exists()


# --- 再起動後の追跡 ---


def test_resolution_and_evidence_are_traceable_after_restart(paths):
    run_cli(
        ChatSession(FakeLLM([judge("replace", "いいえ、もうありません。")]), *paths),
        ["/confirm p001", "いいえ、もうありません。", "y"],
    )
    llm = FakeLLM(plans=[plan(["住ん"], history=["home_location"])])
    restarted = ChatSession(llm, *paths)

    outputs = run_cli(restarted, ["/pending p001", "前はどこに住んでいた？"])

    assert "[長期保留] p001 status: resolved" in outputs
    assert any(o.startswith("[確認記録] c001 presented") and QUESTION in o for o in outputs)
    assert any(o.startswith("[確認記録] c001 judged") and "いいえ、もうありません。" in o for o in outputs)
    assert any(o.startswith("[確認記録] c001 approval") and "承認" in o for o in outputs)
    # 変更履歴のファイルには原文を写さず、回答AIへ渡すときに確認ログから引く
    assert "confirmation_question" not in paths[5].read_text(encoding="utf-8")
    prompt = llm.prompts[-1]
    history_block = prompt.split("[long-term memory history]\n")[1].split("\n[/long-term memory history]")[0]
    assert "old_value: 川崎" in history_block
    assert "confirmation_id: c001" in history_block
    assert f"confirmation_question: {QUESTION}" in history_block
    assert "confirmation_answer: いいえ、もうありません。" in history_block
    assert "approval_input: y" in history_block
    # 解決済みの保留は、未解決の保留として回答AIへ渡さない
    assert "[long-term memory pending]" not in prompt


def test_resolved_pending_ids_are_not_reused(paths):
    llm = FakeLLM(
        [judge("coexist", "はい")],
        structured=[json.dumps([{
            "decision": "pending", "subject": "user", "target_key": "home_location", "proposed_key": None,
            "proposed_value": "大阪", "evidence": "大阪に住んでいます。", "old_value_status": "not_stated",
            "old_value_evidence": None, "temporal_scope": "current", "reason": "r",
            "confirmation_question": "川崎には今も住んでいますか？",
        }], ensure_ascii=False)],
    )

    run_cli(ChatSession(llm, *paths), ["/confirm p001", "はい", "大阪に住んでいます。"])

    assert [(p["id"], p["status"]) for p in load_memory_pending(paths[6])] == [("p001", "resolved"), ("p002", "unresolved")]


def test_main_reports_resolved_pending_and_recovery(tmp_path, monkeypatch, capsys, clock):
    paths = make_paths(tmp_path, pending=[P001, pending_item("p002", status="resolved")])
    commit_only(paths)
    names = (
        "ACM_HISTORY_FILE",
        "ACM_MEMORY_FILE",
        "ACM_WORKING_MEMORY_FILE",
        "ACM_WORKING_MEMORY_HISTORY_FILE",
        "ACM_LONG_TERM_MEMORY_STATE_FILE",
        "ACM_LONG_TERM_MEMORY_HISTORY_FILE",
        "ACM_LONG_TERM_MEMORY_PENDING_FILE",
        "ACM_LONG_TERM_MEMORY_CONFIRMATIONS_FILE",
    )
    for name, path in zip(names, paths):
        monkeypatch.setenv(name, str(path))
    monkeypatch.setenv("ACM_LONG_TERM_MEMORY_REVERTS_FILE", str(tmp_path / "data" / "long_term_memory_reverts.jsonl"))
    monkeypatch.setattr(cli_module, "ClaudeCLI", lambda: FakeLLM())
    monkeypatch.setattr(cli_module.sys, "stdin", FakeStdin("exit\n"))
    monkeypatch.setattr(cli_module.sys.stdout, "reconfigure", lambda **kwargs: None, raising=False)

    assert cli_module.main() == 0
    out = capsys.readouterr().out
    assert "長期保留を読み込みました（未解決 1件、解決済み 1件）。" in out
    assert "[復旧] c001（p001）: 前回途中で止まった変更を確認し、反映されていなかったため、実行しませんでした" in out


class FakeStdin(io.StringIO):
    def reconfigure(self, **kwargs):
        pass


# --- 番号付きの key ---


@pytest.mark.parametrize(
    "key, base",
    [("home_location_2", "home_location"), ("home_location_10", "home_location"), ("home_location", "home_location"),
     ("top_1", "top_1"), ("a2", "a2")],
)
def test_attribute_base(key, base):
    assert attribute_base(key) == base


def test_numbered_keys_are_retrieved_and_described_together(tmp_path, clock):
    nagoya = {**KAWASAKI, "key": "home_location_2", "value": "名古屋", "evidence": NAGOYA_TEXT}
    paths = make_paths(tmp_path, state=[KAWASAKI, nagoya], pending=[])
    llm = FakeLLM(plans=[plan(["川崎"])])

    run_cli(ChatSession(llm, *paths), ["川崎の家はどうなっていますか？"])

    block = llm.prompts[0].split("[long-term memory state]\n")[1].split("\n[/long-term memory state]")[0]
    assert "- user.home_location: 川崎" in block
    assert "- user.home_location_2: 名古屋 (same_attribute_as: home_location," in block


def test_ai_cannot_create_numbered_keys():
    text = "名古屋にも家があります。"
    candidate = {
        "decision": "coexist", "subject": "user", "target_key": "home_location", "proposed_key": "home_location_2",
        "proposed_value": "名古屋", "evidence": text, "old_value_status": "continues", "old_value_evidence": None,
        "temporal_scope": "current", "reason": "r", "confirmation_question": None,
    }

    updated, changes, warnings = apply_candidates([KAWASAKI], [candidate], text, T1)

    assert updated == [KAWASAKI]
    assert changes == []
    assert "proposed_key に番号付きの key は使えません" in warnings[0]


def test_numbered_key_is_marked_in_the_state_block():
    nagoya = {**KAWASAKI, "key": "home_location_2", "value": "名古屋"}

    assert "same_attribute_as" not in format_memory_state([KAWASAKI])
    assert "same_attribute_as: home_location" in format_memory_state([nagoya])
    # 関係判断AIへの入力（with_details=False）には付けない
    assert "same_attribute_as" not in format_memory_state([nagoya], with_details=False)


# --- 通常の会話 ---


def test_normal_conversation_is_unchanged_while_pending_exists(paths):
    llm = FakeLLM(replies=["2です。"])

    outputs = run_cli(ChatSession(llm, *paths), ["1+1は？"])

    assert llm.calls == ["plan_search", "complete", "extract_memories", "extract_structured_memory", "extract_working_memory"]
    assert not any(o.startswith("[確認") for o in outputs)
    assert not paths[7].exists()


def test_pending_sent_to_the_answer_ai_comes_with_the_command_hint(paths):
    llm = FakeLLM(replies=["川崎と名古屋の記録があります。"], plans=[plan(["住ん"])])

    outputs = run_cli(ChatSession(llm, *paths), ["今の住まいは？"])

    assert "[long-term memory pending]" in llm.prompts[0]
    assert "[確認コマンド] p001 は /confirm p001 で確認できます" in outputs
    assert "judge_confirmation" not in llm.calls


def test_confirmation_needs_the_log_path(tmp_path, clock):
    paths = make_paths(tmp_path)

    outputs = run_cli(ChatSession(FakeLLM(), *paths[:7]), ["/confirm p001"])

    assert any("保存先が設定されていないため、確認できません" in o for o in outputs)


# --- 住まい以外の属性 ---


@pytest.mark.parametrize(
    "current, item, answer, key",
    [
        (PEAR, APPLE_PENDING, "梨も好きです。", "favorite_fruit_2"),
        (A_COMPANY, B_PENDING, "副業でA社にも勤めています。", "employer_2"),
    ],
)
def test_coexist_for_other_attributes(tmp_path, clock, current, item, answer, key):
    paths = make_paths(tmp_path, state=[current], pending=[item])
    llm = FakeLLM([judge("coexist", answer, pending_id=item["id"])])

    run_cli(ChatSession(llm, *paths), [f"/confirm {item['id']}", answer])

    assert [(i["key"], i["value"]) for i in state(paths)] == [
        (current["key"], current["value"]),
        (key, item["proposed_value"]),
    ]


@pytest.mark.parametrize(
    "current, item, answer",
    [(PEAR, APPLE_PENDING, "梨はもう好きじゃないです。"), (A_COMPANY, B_PENDING, "A社は辞めました。")],
)
def test_replace_for_other_attributes(tmp_path, clock, current, item, answer):
    paths = make_paths(tmp_path, state=[current], pending=[item])
    llm = FakeLLM([judge("replace", answer, pending_id=item["id"])])

    outputs = run_cli(ChatSession(llm, *paths), [f"/confirm {item['id']}", answer, "y"])

    assert any(
        f"「{current['value']}」を user.{current['key']} の現在の値から外し、「{item['proposed_value']}」" in o
        for o in outputs
    )
    assert [(i["key"], i["value"]) for i in state(paths)] == [(current["key"], item["proposed_value"])]
    assert history(paths)[0]["old_value"] == current["value"]


# --- プロンプト ---


def test_confirmation_prompt_explains_polarity_withdrawal_and_outcomes():
    prompt = llm_module.CONFIRMATION_SYSTEM_PROMPT

    for word in ("coexist", "replace", "unresolved", "not_an_answer", "withdrawn", "answer_evidence", "pending_id"):
        assert word in prompt
    assert "否定の形の質問" in prompt
    assert "言い間違い" in prompt
    assert "迷う場合は unresolved を選んでください。" in prompt
    # 住まいの例を正解として載せない
    for place in ("川崎", "名古屋"):
        assert place not in prompt


def test_confirmation_request_contains_only_the_target():
    text = format_confirmation_request(P001, KAWASAKI, QUESTION, "いいえ")

    assert text == (
        "[pending]\nid: p001\nsubject: user\ntarget_key: home_location\ntarget_value: 川崎\n"
        "proposed_value: 名古屋\nevidence: 今は名古屋に住んでいます。\n[/pending]\n\n"
        "[long-term memory state]\n- user.home_location: 川崎\n[/long-term memory state]\n\n"
        f"[question]\n{QUESTION}\n[/question]\n\n[answer]\nいいえ\n[/answer]"
    )


def test_system_prompt_explains_numbered_keys_and_confirmed_history():
    system = llm_module.SYSTEM_PROMPT

    assert "番号は優先順位や確かさを表しません。" in system
    assert "どれか1つだけを現在の値として扱わず、すべてを現在の値として伝えてください。" in system
    assert "confirmation_question はこのシステムがユーザーに表示した確認の質問" in system


def test_judgment_problem_accepts_a_consistent_judgment():
    assert judgment_problem(json.loads(judge("replace", "いいえ")), P001, "いいえ") is None
    assert judgment_problem(json.loads(judge("not_an_answer", None)), P001, "1+1は？") is None


def test_confirmation_ids_have_a_fixed_format(paths):
    run_cli(ChatSession(FakeLLM(), *paths), ["/confirm p001", "/cancel", "/confirm p001"])

    ids = [e["confirmation_id"] for e in load_confirmations(paths[7])]
    assert ids == ["c001", "c001", "c001", "c002", "c002"]
    assert all(CONFIRMATION_ID_PATTERN.fullmatch(i) for i in ids)


# --- 保存形式の互換性 ---


def test_mvp9_pending_file_without_the_new_keys_still_loads(tmp_path):
    path = tmp_path / "pending.json"
    mvp9 = {k: v for k, v in P001.items() if k not in ("target_updated_at", "resolution", "resolved_key", "resolved_at", "confirmation_id")}
    path.write_text(json.dumps({"items": [mvp9]}, ensure_ascii=False), encoding="utf-8")

    [item] = load_memory_pending(path)

    assert item == {**P001, "target_updated_at": None}


def test_pending_with_an_unknown_status_is_rejected(tmp_path):
    path = tmp_path / "pending.json"
    path.write_text(json.dumps({"items": [{**P001, "status": "done"}]}, ensure_ascii=False), encoding="utf-8")

    with pytest.raises(structured_module.StructuredMemoryError, match="長期保留ファイルの形式が不正です"):
        load_memory_pending(path)


def test_missing_confirmation_log_starts_with_no_events(paths):
    session = ChatSession(FakeLLM(), *paths)

    assert session.confirmations == []
    assert not paths[7].exists()


def test_history_mixing_mvp9_and_confirmation_events_loads(paths):
    run_cli(ChatSession(FakeLLM([judge("coexist", "はい")]), *paths), ["/confirm p001", "はい"])
    mvp9_event = {"timestamp": T1, "operation": "add", "subject": "user", "key": "name", "value": "花子", "evidence": "花子です。"}
    append_text(paths[5], json.dumps(mvp9_event, ensure_ascii=False) + "\n")

    events = history(paths)

    assert [e.get("confirmation_id") for e in events] == ["c001", None]
