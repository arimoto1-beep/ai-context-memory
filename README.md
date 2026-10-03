# ai-context-memory

「忘れないAI」を段階的に作っていくPoCです。

## 現在の状態: MVP1（ベースライン）

記憶機能を持たない「普通のAIチャット」です。今後追加する記憶機能と比較するための基準点として使います。

- CLIでAIと会話できる
- 同一プロセス内では、それまでの会話履歴をすべてLLMへ渡して会話を継続する
- プロセスを終了すると会話内容はすべて失われる（永続化なし）

## セットアップ

Python 3.10 以上と、ログイン済みの [Claude Code](https://claude.com/claude-code) CLI が必要です。

```powershell
python -m venv .venv
.venv\Scripts\Activate.ps1
pip install -e ".[dev]"
```

LLMの呼び出しには `claude` コマンドを非対話モード（`claude -p`）で使います。APIキーは不要で、Claude Code にログインしているサブスクリプションの利用枠を消費します。

```powershell
claude auth status   # "loggedIn": true, "authMethod": "claude.ai" なら準備完了
claude auth login    # 未ログインの場合
```

環境変数 `ANTHROPIC_API_KEY` / `ANTHROPIC_AUTH_TOKEN` が設定されていても、このアプリは `claude` に渡しません（渡すとAPI課金になるため）。

## 起動方法

```powershell
python -m ai_context_memory
```

`exit` または `quit`（もしくは Ctrl+C / Ctrl+Z → Enter）で終了します。

```text
チャットを開始します。終了するには exit または quit と入力してください。

あなた> 私の名前は有本です

AI> 有本さん、はじめまして。

あなた> 私の名前は？

AI> 有本さんです。

あなた> exit
終了します。
```

使用モデルは Claude Code 側の既定モデルです。環境変数 `ACM_MODEL` で変更できます（`claude --model` に渡せる値）。

```powershell
$env:ACM_MODEL = "sonnet"
```

`claude` はツール無効・利用者設定（CLAUDE.md、MCP、フックなど）を読み込まないモードで起動するため、素のチャットとして動作します。1ターンごとに `claude` を起動するので、応答には数秒かかります。

## テスト

```powershell
python -m pytest
```

テストは `subprocess` をモックして実行するため、`claude` を起動せず、利用枠も消費しません。

## 構成

```text
src/ai_context_memory/
  llm.py       LLM呼び出し（claude CLIをsubprocessで実行するのはここだけ）
  chat.py      ChatSession: 会話履歴の保持と1ターンの送受信
  cli.py       対話ループ（入力・表示・終了・エラー表示）
  __main__.py  python -m ai_context_memory の入口
tests/
  test_chat.py / test_cli.py / test_llm.py
```
