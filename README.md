# ai-context-memory

「忘れないAI」を段階的に作っていくPoCです。

## 現在の状態: MVP2（会話履歴のファイル永続化）

「終了すると忘れる」から「終了しても会話そのものは残る」へ1段だけ進めた段階です。評価条件は [docs/mvp-02.md](docs/mvp-02.md) にあります。

- CLIでAIと会話できる
- 会話はローカルファイルへ保存され、アプリを終了・再起動しても前回の続きから会話できる
- 現段階では、保存済みのものを含む全履歴を毎ターンそのままLLMへ渡している（要約・検索・取捨選択はしない）

MVP1（ベースライン）は、履歴をメモリ上にだけ持ち、プロセスを終了すると会話内容がすべて失われる「普通のAIチャット」でした。

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

再起動すると、前回までの会話を読み込んで続きから始まります。

```text
前回までの会話を読み込みました（4 件）。
チャットを開始します。終了するには exit または quit と入力してください。

あなた> 前回私が名乗った名前は？

AI> 有本さんです。
```

使用モデルは Claude Code 側の既定モデルです。環境変数 `ACM_MODEL` で変更できます（`claude --model` に渡せる値）。

```powershell
$env:ACM_MODEL = "sonnet"
```

`claude` はツール無効・利用者設定（CLAUDE.md、MCP、フックなど）を読み込まないモードで起動するため、素のチャットとして動作します。1ターンごとに `claude` を起動するので、応答には数秒かかります。

## 会話の保存

- 保存場所: 起動時のカレントディレクトリから見た `data/conversation.jsonl`（`chat.bat` から起動した場合はリポジトリ直下の `data/`）
- 形式: JSONL。1行が1メッセージ（`{"role": "user", "content": "..."}`）
- 保存タイミング: AIの応答が正常に返るたびに、user / assistant の1ターンを追記する。`exit` / `quit` やエラーになったターンは保存しない
- `data/` は `.gitignore` の対象で、Git管理対象外です。会話内容がGitHubへ公開されることはありません
- 会話は1本だけです。最初からやり直すには `data/conversation.jsonl` を削除してください
- 保存先は環境変数 `ACM_HISTORY_FILE` で変更できます

```powershell
$env:ACM_HISTORY_FILE = "D:\work\trial\conversation.jsonl"
```

保存ファイルが壊れている場合（手で編集して不正なJSONLになったなど）は、該当の行番号を表示して起動を中止します。ファイルを修正するか削除してください。保存に失敗した場合は、応答を表示したうえでエラーを表示し、会話を続行します（そのターンはファイルに残りません）。

全履歴を毎回送るため、会話が長くなるほど1ターンあたりの入力が増え、いずれモデルのコンテキスト上限に達します。これは今後のMVPで扱う課題です。

## テスト

```powershell
python -m pytest
```

テストは `subprocess` をモックして実行するため、`claude` を起動せず、利用枠も消費しません。会話の保存先には pytest の一時ディレクトリを使うため、`data/` にも触れません。

## 構成

```text
src/ai_context_memory/
  llm.py       LLM呼び出し（claude CLIをsubprocessで実行するのはここだけ）
  chat.py      ChatSession: 会話履歴の保持・復元と1ターンの送受信
  history.py   会話履歴ファイル（JSONL）の読み込みと追記
  cli.py       対話ループ（入力・表示・終了・エラー表示）
  __main__.py  python -m ai_context_memory の入口
tests/
  test_chat.py / test_cli.py / test_history.py / test_llm.py
```
