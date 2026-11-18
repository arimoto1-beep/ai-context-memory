"""MVP15: AI呼び出しごとの記録（役割・入力文字数・実際に報告された usage・時間・成否）。

記録にプロンプトや応答の本文、エラーの詳細を入れないこと、usage が無いときに推定値を入れないこと、
記録のために呼び出しを増やさないことを確かめる。実際の claude は起動しない（test_llm の fake_run を使う）。
"""

import json
import subprocess

from ai_context_memory import llm as llm_module
from ai_context_memory.chat import ChatSession
from ai_context_memory.cli import run
from ai_context_memory.llm import (
    SEARCH_PLAN_SYSTEM_PROMPT,
    STRUCTURED_MEMORY_SYSTEM_PROMPT,
    ClaudeCLI,
    LLMError,
)
from ai_context_memory.structured_memory import save_memory_state
from test_llm import completed, fake_run  # noqa: F401  （fake_run は pytest のフィクスチャ）

import pytest

SECRET = "私の名前は秘密山太郎です"
USAGE = {
    "input_tokens": 2,
    "cache_creation_input_tokens": 6966,
    "cache_read_input_tokens": 100,
    "output_tokens": 626,
    "service_tier": "standard",
}
MODEL_USAGE = {
    "claude-opus-5-5": {"inputTokens": 2, "outputTokens": 626, "cacheReadInputTokens": 100, "cacheCreationInputTokens": 6966, "costUSD": 0.06},
    "claude-haiku-5-5": {"inputTokens": 4914, "outputTokens": 212, "cacheReadInputTokens": 0, "cacheCreationInputTokens": 0},
}


def read_log(path):
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


def test_call_is_recorded_with_reported_usage_and_without_text(tmp_path, fake_run):
    fake_run.result = completed(result=f"{SECRET}の応答", usage=USAGE, modelUsage=MODEL_USAGE, duration_ms=6669, duration_api_ms=6500)
    log = tmp_path / "ai_calls.jsonl"
    cli = ClaudeCLI(call_log_path=log)

    cli.complete([{"role": "user", "content": SECRET}])

    [record] = read_log(log)
    assert record["role"] == "answer"
    assert record["status"] == "ok" and record["error_kind"] is None
    assert record["prompt_chars"] == len(fake_run.kwargs["input"])
    assert record["input_chars"] == record["system_chars"] + record["prompt_chars"]
    assert record["usage"] == {
        "input_tokens": 2,
        "cache_creation_input_tokens": 6966,
        "cache_read_input_tokens": 100,
        "output_tokens": 626,
        "input_tokens_total": 7068,
    }
    # モデルごとの usage は、出力にあったモデル名のまま残す（役割の分類はしない）
    assert record["model_usage"] == {
        "claude-opus-5-5": {"inputTokens": 2, "outputTokens": 626, "cacheReadInputTokens": 100, "cacheCreationInputTokens": 6966},
        "claude-haiku-5-5": {"inputTokens": 4914, "outputTokens": 212, "cacheReadInputTokens": 0, "cacheCreationInputTokens": 0},
    }
    assert record["duration_ms"] == 6669 and record["duration_api_ms"] == 6500
    assert isinstance(record["elapsed_ms"], int)
    assert SECRET not in log.read_text(encoding="utf-8")
    assert cli.calls == [record]


def test_missing_usage_is_recorded_as_null_not_estimated(tmp_path, fake_run):
    fake_run.result = completed(result="[]")
    log = tmp_path / "ai_calls.jsonl"

    ClaudeCLI(call_log_path=log).extract_memories(SECRET)

    [record] = read_log(log)
    assert record["role"] == "memory_extraction"
    assert record["usage"] is None and record["model_usage"] is None and record["duration_ms"] is None


def test_partial_usage_does_not_get_a_total(tmp_path, fake_run):
    fake_run.result = completed(result="[]", usage={"input_tokens": 10, "output_tokens": 3})
    log = tmp_path / "ai_calls.jsonl"

    ClaudeCLI(call_log_path=log).extract_memories("こんにちは")

    usage = read_log(log)[0]["usage"]
    assert usage["input_tokens"] == 10 and usage["cache_read_input_tokens"] is None
    assert usage["input_tokens_total"] is None


@pytest.mark.parametrize(
    "result, kind",
    [
        (completed(returncode=1, result=f"{SECRET} で失敗", is_error=True, usage=USAGE), "exit_error"),
        (completed(returncode=1, stderr="Please run claude login"), "not_logged_in"),
        (completed(result=""), "no_text"),
        (subprocess.TimeoutExpired(cmd="claude", timeout=1), "timeout"),
        (OSError("boom"), "launch_failed"),
    ],
)
def test_failed_call_is_recorded_without_the_error_detail(tmp_path, fake_run, result, kind):
    fake_run.result = result
    log = tmp_path / "ai_calls.jsonl"

    with pytest.raises(LLMError):
        ClaudeCLI(call_log_path=log).plan_search(SECRET)

    [record] = read_log(log)
    assert record["role"] == "search_plan"
    assert record["status"] == "error" and record["error_kind"] == kind
    assert SECRET not in log.read_text(encoding="utf-8")
    if kind == "exit_error":
        # 失敗した呼び出しでも、報告された usage は残す
        assert record["usage"]["output_tokens"] == 626


def test_log_that_cannot_be_written_does_not_stop_the_call(tmp_path, fake_run):
    fake_run.result = completed(result="こんにちは")
    cli = ClaudeCLI(call_log_path=tmp_path)  # ディレクトリなので開けない

    assert cli.complete([{"role": "user", "content": "やあ"}]) == "こんにちは"
    assert cli.call_log_error and str(tmp_path) in cli.call_log_error
    assert len(cli.calls) == 1


def test_unexpected_log_failure_does_not_change_the_call_result(tmp_path, fake_run):
    """ファイル以外の理由（記録先の型の誤りなど）で記録できなくても、成功は成功、失敗は元の LLMError のまま。"""
    fake_run.result = completed(result="こんにちは")
    cli = ClaudeCLI(call_log_path=str(tmp_path / "ai_calls.jsonl"))  # Path ではない

    assert cli.complete([{"role": "user", "content": "やあ"}]) == "こんにちは"
    assert cli.call_log_error

    fake_run.result = completed(result="")
    with pytest.raises(LLMError, match="応答テキストを取得できませんでした"):
        cli.plan_search("こんにちは")
    assert [c["status"] for c in cli.calls] == ["ok", "error"]


def test_no_file_is_written_without_a_log_path(tmp_path, fake_run, monkeypatch):
    monkeypatch.chdir(tmp_path)
    ClaudeCLI().complete([{"role": "user", "content": "やあ"}])
    assert list(tmp_path.iterdir()) == []


def respond_by_role(fake_run):
    """指示ごとに妥当な応答を返す subprocess.run の差し替え。"""
    plan = json.dumps({"needs_memory": False, "queries": [], "attributes": []})

    def run_(command, **kwargs):
        fake_run.commands.append(command)
        system = command[command.index("--system-prompt") + 1]
        if system == SEARCH_PLAN_SYSTEM_PROMPT:
            return completed(result=plan, usage=USAGE)
        if system.startswith("あなたはチャットアシスタント"):
            return completed(result="はい。", usage=USAGE)
        return completed(result="[]", usage=USAGE)

    fake_run.commands = []
    return run_


def test_turns_number_the_calls_and_logging_adds_no_calls(tmp_path, fake_run, monkeypatch):
    monkeypatch.setattr(llm_module.subprocess, "run", respond_by_role(fake_run))
    data = tmp_path / "data"
    files = [data / name for name in (
        "conversation.jsonl", "memories.jsonl", "working_memory.json", "working_memory_history.jsonl",
        "long_term_memory_state.json", "long_term_memory_history.jsonl", "long_term_memory_pending.json",
    )]
    save_memory_state(files[4], [{"subject": "user", "key": "hobby", "value": "釣り", "evidence": "趣味は釣りです。"}])
    log = tmp_path / "ai_calls.jsonl"
    cli = ClaudeCLI(call_log_path=log)
    inputs = iter(["こんにちは", "趣味は登山です。", "exit"])

    run(ChatSession(cli, *files), input_fn=lambda prompt: next(inputs), output_fn=lambda line: None)

    records = read_log(log)
    assert len(records) == len(fake_run.commands) == 10  # 1ターン5回（記録で呼び出しは増えない）
    assert [r["turn"] for r in records] == [1] * 5 + [2] * 5
    assert [r["role"] for r in records[:5]] == [
        "search_plan",
        "answer",
        "memory_extraction",
        "structured_memory_extraction",
        "working_memory_extraction",
    ]
    structured = records[3]
    assert structured["details"] == {
        "view": "full",
        "key_coverage": "complete",
        "attributes": 1,
        "listed_attributes": 0,
        "value_attributes": 1,
    }
    assert structured["system_chars"] == len(STRUCTURED_MEMORY_SYSTEM_PROMPT)
    assert "趣味" not in log.read_text(encoding="utf-8")
