import json
import subprocess

import pytest

from ai_context_memory import llm
from ai_context_memory.llm import ClaudeCLI, LLMError

HISTORY = [{"role": "user", "content": "やあ"}]


@pytest.fixture
def fake_run(monkeypatch):
    """subprocess.run と shutil.which を差し替え、実際の claude を起動しない。

    fake_run.result に CompletedProcess か例外を設定して使う。
    呼び出し時の引数は fake_run.command / fake_run.kwargs に記録される。
    """

    def run(command, **kwargs):
        run.command = command
        run.kwargs = kwargs
        if isinstance(run.result, Exception):
            raise run.result
        return run.result

    run.result = completed(result="こんにちは")
    monkeypatch.setattr(llm.shutil, "which", lambda name: "/fake/bin/claude")
    monkeypatch.setattr(llm.subprocess, "run", run)
    monkeypatch.delenv("ACM_MODEL", raising=False)
    return run


def completed(returncode=0, stderr="", stdout=None, **payload):
    if stdout is None:
        stdout = json.dumps({"type": "result", "is_error": False, **payload})
    return subprocess.CompletedProcess([], returncode, stdout=stdout, stderr=stderr)


def test_complete_returns_result_text(fake_run):
    assert ClaudeCLI().complete(HISTORY) == "こんにちは"


def test_runs_claude_in_non_interactive_mode_without_tools(fake_run):
    ClaudeCLI().complete(HISTORY)

    command = fake_run.command
    assert command[0] == "/fake/bin/claude"
    assert "-p" in command
    assert command[command.index("--output-format") + 1] == "json"
    assert command[command.index("--tools") + 1] == ""
    assert "--model" not in command
    assert fake_run.kwargs["timeout"] == llm.TIMEOUT_SECONDS


def test_whole_history_is_sent_on_stdin(fake_run):
    history = [
        {"role": "user", "content": "私の名前は有本です"},
        {"role": "assistant", "content": "了解"},
        {"role": "user", "content": "私の名前は？"},
    ]

    ClaudeCLI().complete(history)

    assert fake_run.kwargs["input"] == (
        "[user]\n私の名前は有本です\n\n[assistant]\n了解\n\n[user]\n私の名前は？"
    )


def test_memories_are_sent_as_a_separate_block_before_the_conversation(fake_run):
    memories = [
        {"text": "ユーザーの名前はテスト太郎", "origin": "user", "evidence": "私の名前はテスト太郎です"},
        {"text": "今回の試験対象はAとB", "origin": "user", "evidence": "今回の試験対象はAとBです"},
    ]

    ClaudeCLI().complete([{"role": "user", "content": "私の名前は？"}], memories)

    assert fake_run.kwargs["input"] == (
        "[retrieved memories]\n"
        "- ユーザーの名前はテスト太郎 (origin: user, evidence: 私の名前はテスト太郎です)\n"
        "- 今回の試験対象はAとB (origin: user, evidence: 今回の試験対象はAとBです)\n"
        "[/retrieved memories]\n\n"
        "[user]\n私の名前は？"
    )
    command = fake_run.command
    assert command[command.index("--system-prompt") + 1] == llm.SYSTEM_PROMPT
    assert "[retrieved memories]" in llm.SYSTEM_PROMPT


def test_extract_memories_sends_only_the_user_text_with_extraction_prompt(fake_run, monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test")
    fake_run.result = completed(result="[]")

    assert ClaudeCLI().extract_memories("私の名前はテスト太郎です") == "[]"

    command = fake_run.command
    assert fake_run.kwargs["input"] == "私の名前はテスト太郎です"
    assert command[command.index("--system-prompt") + 1] == llm.EXTRACTION_SYSTEM_PROMPT
    # 通常回答と同じ原則: 非対話・ツール無効・セッション非永続・APIキーを渡さない
    assert "-p" in command
    assert command[command.index("--tools") + 1] == ""
    assert "--no-session-persistence" in command
    assert "ANTHROPIC_API_KEY" not in fake_run.kwargs["env"]


def test_plan_search_sends_only_the_question_with_search_plan_prompt(fake_run, monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test")
    plan = '{"needs_memory": true, "queries": ["名前"]}'
    fake_run.result = completed(result=plan)

    assert ClaudeCLI().plan_search("私の名前は？") == plan

    command = fake_run.command
    assert fake_run.kwargs["input"] == "[question]\n私の名前は？"
    assert command[command.index("--system-prompt") + 1] == llm.SEARCH_PLAN_SYSTEM_PROMPT
    # 通常回答と同じ原則: 非対話・ツール無効・セッション非永続・APIキーを渡さない
    assert "-p" in command
    assert command[command.index("--tools") + 1] == ""
    assert "--no-session-persistence" in command
    assert "ANTHROPIC_API_KEY" not in fake_run.kwargs["env"]


def test_plan_search_retry_sends_question_and_failed_queries_only(fake_run):
    fake_run.result = completed(result='{"needs_memory": true, "queries": ["スタッドレス"]}')

    ClaudeCLI().plan_search("冬用のタイヤ、何インチ？", ["冬用タイヤ", "タイヤ サイズ"])

    assert fake_run.kwargs["input"] == (
        "[question]\n冬用のタイヤ、何インチ？\n\n"
        "[previous queries: 0 hits]\n- 冬用タイヤ\n- タイヤ サイズ"
    )


def test_extract_memories_failure_becomes_llm_error(fake_run):
    fake_run.result = completed(is_error=True, result="API Error: 529 Overloaded")

    with pytest.raises(LLMError, match="529 Overloaded"):
        ClaudeCLI().extract_memories("やあ")


def test_api_billing_env_vars_are_not_passed_to_claude(fake_run, monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test")
    monkeypatch.setenv("ANTHROPIC_AUTH_TOKEN", "token")

    ClaudeCLI().complete(HISTORY)

    env = fake_run.kwargs["env"]
    assert "ANTHROPIC_API_KEY" not in env
    assert "ANTHROPIC_AUTH_TOKEN" not in env
    assert "PATH" in env


def test_model_can_be_overridden_by_env(fake_run, monkeypatch):
    monkeypatch.setenv("ACM_MODEL", "sonnet")

    ClaudeCLI().complete(HISTORY)

    command = fake_run.command
    assert command[command.index("--model") + 1] == "sonnet"


def test_missing_cli_becomes_llm_error(fake_run, monkeypatch):
    monkeypatch.setattr(llm.shutil, "which", lambda name: None)

    with pytest.raises(LLMError, match="見つかりません"):
        ClaudeCLI().complete(HISTORY)


def test_not_logged_in_becomes_llm_error(fake_run):
    # 未ログイン時の実際の出力: 終了コード1で、stdoutにis_error付きのJSONが出る
    fake_run.result = completed(
        returncode=1, is_error=True, result="Not logged in · Please run /login"
    )

    with pytest.raises(LLMError, match="claude auth login"):
        ClaudeCLI().complete(HISTORY)


def test_timeout_becomes_llm_error(fake_run):
    fake_run.result = subprocess.TimeoutExpired(cmd="claude", timeout=llm.TIMEOUT_SECONDS)

    with pytest.raises(LLMError, match="応答しませんでした"):
        ClaudeCLI().complete(HISTORY)


def test_nonzero_exit_becomes_llm_error_with_stderr(fake_run):
    fake_run.result = completed(returncode=2, stdout="", stderr="error: something broke")

    with pytest.raises(LLMError, match=r"終了コード 2.*something broke"):
        ClaudeCLI().complete(HISTORY)


def test_is_error_with_zero_exit_becomes_llm_error(fake_run):
    fake_run.result = completed(is_error=True, result="API Error: 529 Overloaded")

    with pytest.raises(LLMError, match="529 Overloaded"):
        ClaudeCLI().complete(HISTORY)


def test_launch_failure_becomes_llm_error(fake_run):
    fake_run.result = PermissionError("access denied")

    with pytest.raises(LLMError, match="起動できませんでした"):
        ClaudeCLI().complete(HISTORY)


def test_non_json_output_becomes_llm_error(fake_run):
    fake_run.result = completed(stdout="not json")

    with pytest.raises(LLMError, match="取得できませんでした"):
        ClaudeCLI().complete(HISTORY)


def test_working_memory_is_sent_before_memories_and_conversation(fake_run):
    working_memory = {
        "mission": [],
        "scope": [{"text": "商用環境", "evidence": "商用環境"}, {"text": "検証環境", "evidence": "検証環境"}],
        "acceptance_criteria": [{"text": "全環境で動けば完了", "evidence": "全環境で動けば完了"}],
    }
    memories = [{"text": "ユーザーの名前はテスト太郎", "origin": "user", "evidence": "私の名前はテスト太郎です"}]

    ClaudeCLI().complete([{"role": "user", "content": "完成ですか？"}], memories, working_memory)

    # 項目の無いフィールド（mission）は見出しごと省く
    assert fake_run.kwargs["input"].split("\n") == [
        "[working memory]",
        "Scope:",
        "- 商用環境",
        "- 検証環境",
        "",
        "Acceptance Criteria:",
        "- 全環境で動けば完了",
        "[/working memory]",
        "",
        "[retrieved memories]",
        "- ユーザーの名前はテスト太郎 (origin: user, evidence: 私の名前はテスト太郎です)",
        "[/retrieved memories]",
        "",
        "[user]",
        "完成ですか？",
    ]


def test_extract_working_memory_sends_current_state_and_utterance_only(fake_run, monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test")
    fake_run.result = completed(result="[]")
    working_memory = {
        "mission": [],
        "scope": [{"text": "商用環境", "evidence": "商用環境"}],
        "acceptance_criteria": [],
    }

    assert ClaudeCLI().extract_working_memory("検証環境も対象にします", working_memory) == "[]"

    command = fake_run.command
    assert fake_run.kwargs["input"].split("\n") == [
        "[working memory]",
        "Scope:",
        "- 商用環境",
        "[/working memory]",
        "",
        "[utterance]",
        "検証環境も対象にします",
    ]
    assert command[command.index("--system-prompt") + 1] == llm.WORKING_MEMORY_SYSTEM_PROMPT
    # 通常回答と同じ原則: 非対話・ツール無効・セッション非永続・APIキーを渡さない
    assert "-p" in command
    assert command[command.index("--tools") + 1] == ""
    assert "--no-session-persistence" in command
    assert "ANTHROPIC_API_KEY" not in fake_run.kwargs["env"]


def test_plan_search_sends_current_working_memory_before_the_question(fake_run):
    fake_run.result = completed(result='{"needs_memory": false, "queries": []}')
    working_memory = {
        "mission": [],
        "scope": [{"text": "商用環境", "evidence": "商用環境"}],
        "acceptance_criteria": [],
    }

    ClaudeCLI().plan_search("この作業の対象環境は？", working_memory=working_memory)

    assert fake_run.kwargs["input"].split("\n") == [
        "[working memory]",
        "Scope:",
        "- 商用環境",
        "[/working memory]",
        "",
        "[question]",
        "この作業の対象環境は？",
    ]


def test_working_memory_history_is_sent_between_working_memory_and_memories(fake_run):
    working_memory = {
        "mission": [],
        "scope": [{"text": "商用環境", "evidence": "商用環境"}],
        "acceptance_criteria": [],
    }
    memories = [{"text": "ユーザーの名前はテスト太郎", "origin": "user", "evidence": "私の名前はテスト太郎です"}]
    events = [
        {
            "timestamp": "2026-10-07T18:48:45+09:00",
            "operation": "remove",
            "field": "scope",
            "text": "ステージング環境",
            "evidence": "ステージング環境はやっぱり対象外にします。",
        }
    ]

    ClaudeCLI().complete(
        [{"role": "user", "content": "いつ外した？"}], memories, working_memory, events
    )

    assert fake_run.kwargs["input"].split("\n") == [
        "[working memory]",
        "Scope:",
        "- 商用環境",
        "[/working memory]",
        "",
        "[working memory history]",
        "- 2026-10-07T18:48:45+09:00",
        "  operation: remove",
        "  field: scope",
        "  text: ステージング環境",
        "  evidence: ステージング環境はやっぱり対象外にします。",
        "[/working memory history]",
        "",
        "[retrieved memories]",
        "- ユーザーの名前はテスト太郎 (origin: user, evidence: 私の名前はテスト太郎です)",
        "[/retrieved memories]",
        "",
        "[user]",
        "いつ外した？",
    ]


def test_extract_structured_memory_sends_current_keys_and_utterance_only(fake_run, monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test")
    fake_run.result = completed(result="[]")
    items = [
        {
            "subject": "user",
            "key": "favorite_fruit",
            "value": "梨",
            "evidence": "私の好きな果物は梨です。",
            "updated_at": "2026-10-09T10:00:00+09:00",
        }
    ]

    assert ClaudeCLI().extract_structured_memory("最近は好きな果物はりんごです。", items) == "[]"

    command = fake_run.command
    assert fake_run.kwargs["input"].split("\n") == [
        "[long-term memory state]",
        "- user.favorite_fruit: 梨",
        "[/long-term memory state]",
        "",
        "[utterance]",
        "最近は好きな果物はりんごです。",
    ]
    assert command[command.index("--system-prompt") + 1] == llm.STRUCTURED_MEMORY_SYSTEM_PROMPT
    # 通常回答と同じ原則: 非対話・ツール無効・セッション非永続・APIキーを渡さない
    assert "-p" in command
    assert command[command.index("--tools") + 1] == ""
    assert "--no-session-persistence" in command
    assert "ANTHROPIC_API_KEY" not in fake_run.kwargs["env"]


def test_long_term_state_and_history_are_sent_before_retrieved_memories(fake_run):
    memories = [{"text": "ユーザーの好きな果物は梨", "origin": "user", "evidence": "私の好きな果物は梨です。"}]
    state = [
        {
            "subject": "user",
            "key": "favorite_fruit",
            "value": "りんご",
            "evidence": "最近は好きな果物はりんごです。",
            "updated_at": "2026-10-09T10:01:00+09:00",
        }
    ]
    events = [
        {
            "timestamp": "2026-10-09T10:01:00+09:00",
            "operation": "replace",
            "subject": "user",
            "key": "favorite_fruit",
            "old_value": "梨",
            "value": "りんご",
            "evidence": "最近は好きな果物はりんごです。",
        }
    ]

    ClaudeCLI().complete(
        [{"role": "user", "content": "前に好きだった果物は？"}],
        memories,
        memory_state=state,
        memory_history_events=events,
    )

    assert fake_run.kwargs["input"].split("\n") == [
        "[long-term memory state]",
        "- user.favorite_fruit: りんご (evidence: 最近は好きな果物はりんごです。, updated_at: 2026-10-09T10:01:00+09:00)",
        "[/long-term memory state]",
        "",
        "[long-term memory history]",
        "- 2026-10-09T10:01:00+09:00",
        "  operation: replace",
        "  subject: user",
        "  key: favorite_fruit",
        "  old_value: 梨",
        "  value: りんご",
        "  evidence: 最近は好きな果物はりんごです。",
        "[/long-term memory history]",
        "",
        "[retrieved memories]",
        "- ユーザーの好きな果物は梨 (origin: user, evidence: 私の好きな果物は梨です。)",
        "[/retrieved memories]",
        "",
        "[user]",
        "前に好きだった果物は？",
    ]
