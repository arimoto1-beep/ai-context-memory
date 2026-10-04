# ai-context-memory

「忘れないAI」を段階的に作っていくPoCです。

## 現在の状態: MVP3（会話原文と長期記憶の分離）

「過去ログを全部読み直せば答えられる」から「過去ログを渡さなくても、過去から作った記憶だけで答えられる」へ1段だけ進めた段階です。評価条件は [docs/mvp-03.md](docs/mvp-03.md) にあります。

- CLIでAIと会話できる
- 会話原文は `data/conversation.jsonl` へ保存する。これは証拠として残すだけで、再起動後に Claude へ再送しない
- ユーザー発言から抽出した長期記憶を `data/memories.jsonl` へ保存する
- 再起動後は、長期記憶を会話履歴とは別枠で Claude へ渡す。会話履歴として渡すのは、今回起動してからの会話だけ
- 記憶抽出の対象はユーザー発言だけ。AIの発言からは記憶を作らない
- 現段階では全記憶を毎回渡す。記憶の更新・検索・矛盾解消はまだない
- 記憶抽出のため、1ターンあたり追加の Claude Code 呼び出しが発生する（通常回答 + 記憶抽出で2回）

これまでの段階:

| | 内容 |
|---|---|
| MVP1 | 履歴をメモリ上にだけ持つ「普通のAIチャット」。終了すると忘れる |
| MVP2 | 会話をファイルへ保存し、再起動時に全履歴を Claude へ再送する（[docs/mvp-02.md](docs/mvp-02.md)） |
| MVP3 | 会話原文と長期記憶を分離し、再起動後は長期記憶だけを渡す |

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

あなた> 私の名前はテスト太郎です

AI> テスト太郎さん、はじめまして。
[記憶] ユーザーの名前はテスト太郎

あなた> exit
終了します。
```

`[記憶]` は、その発言から長期記憶として保存した内容です。

再起動すると、長期記憶だけを読み込みます。前回の会話そのものは Claude へ渡しません。

```text
長期記憶を読み込みました（1 件）。
チャットを開始します。終了するには exit または quit と入力してください。

あなた> 私の名前は？

AI> テスト太郎さんですね。
```

使用モデルは Claude Code 側の既定モデルです。環境変数 `ACM_MODEL` で変更できます（`claude --model` に渡せる値）。通常回答と記憶抽出の両方に同じモデルを使います。

```powershell
$env:ACM_MODEL = "sonnet"
```

`claude` はツール無効・利用者設定（CLAUDE.md、MCP、フックなど）を読み込まないモードで起動するため、素のチャットとして動作します。1ターンごとに `claude` を2回（通常回答と記憶抽出）起動するので、応答が表示されたあと次の入力ができるまでにも数秒かかります。

## 保存するデータ

| ファイル | 役割 | Claude へ渡すか |
|---|---|---|
| `data/conversation.jsonl` | 会話原文。証拠 | 渡さない（追記するだけで、起動時に読み戻さない） |
| `data/memories.jsonl` | 抽出された長期記憶 | 起動時に読み込み、全件を別枠で渡す |

- 保存場所: 起動時のカレントディレクトリから見た `data/`（`chat.bat` から起動した場合はリポジトリ直下の `data/`）
- `data/` は `.gitignore` の対象で、Git管理対象外です。会話内容も記憶もGitHubへ公開されることはありません
- 会話もユーザーも1つだけです。最初からやり直すには `data/` の2つのファイルを削除してください
- 保存先は環境変数 `ACM_HISTORY_FILE` / `ACM_MEMORY_FILE` で変更できます

```powershell
$env:ACM_HISTORY_FILE = "D:\work\trial\conversation.jsonl"
$env:ACM_MEMORY_FILE = "D:\work\trial\memories.jsonl"
```

### 会話原文（conversation.jsonl）

- 形式: JSONL。1行が1メッセージ（`{"role": "user", "content": "..."}`）
- 保存タイミング: AIの応答が正常に返るたびに、user / assistant の1ターンを追記する。`exit` / `quit` やエラーになったターンは保存しない
- 保存に失敗した場合は、応答を表示したうえでエラーを表示し、会話を続行する（そのターンは原文が残らないので、記憶抽出も行わない）

MVP2 で保存した `conversation.jsonl` はそのまま残りますが、MVP3 はそこから記憶を作り直しません。記憶になるのは MVP3 で会話した発言だけです。

### 長期記憶（memories.jsonl）

- 形式: JSONL。1行が1つの記憶

  ```jsonl
  {"text": "ユーザーの名前はテスト太郎", "origin": "user", "evidence": "私の名前はテスト太郎です"}
  ```

  | フィールド | 内容 |
  |---|---|
  | `text` | 記憶内容 |
  | `origin` | 誰の発言が元か。現段階では常に `user` |
  | `evidence` | 根拠となった原文。元のユーザー発言からの抜き出し |

- 抽出タイミング: 通常の応答が返り、会話原文を保存できたあとに、そのユーザー発言1つだけを入力として、別の `claude -p` 呼び出しで抽出する。AIの発言や過去の会話は抽出の入力に含めない
- 「それに決めます」のように、その発言だけでは意味が確定しないものは記憶されない
- ファイルが壊れている場合（手で編集して不正なJSONLになったなど）は、該当の行番号を表示して起動を中止する。ファイルを修正するか削除する

### AIとPythonの役割分担

- AI: その発言に、後で役に立ちそうな記憶があるかという意味判断。記憶候補（`text` と `evidence`）をJSON配列で返す
- Python: JSONの検証、`evidence` の検証、`origin` の付与、保存と読み込み

AIが返した `evidence` が元のユーザー発言にそのまま含まれていない場合、その候補は保存しません（AIが根拠を作り出していないことの確認）。

記憶抽出はチャット本体より優先度が低いので、抽出の呼び出しが失敗した、不正なJSONが返った、`evidence` の検証に失敗した、保存に失敗した、のいずれの場合も `[警告]` を表示して会話を続行します。

### Claudeへ渡すもの

通常回答のたびに、長期記憶の全件と、今回起動してからの会話を渡します。

```text
[long-term memory]
- ユーザーの名前はテスト太郎
- 今回の試験対象はAとBである
[/long-term memory]

[user]
私の名前は？
```

長期記憶は「過去の別の会話でのユーザー発言から抽出されたもので、現在の会話の履歴ではない」「必要な場合にだけ使う」「現在の発言と矛盾したら現在の発言を優先する」とシステム指示で伝えています。

## 現段階の限界

- 全記憶を毎回渡すため、記憶が増えるほど1ターンあたりの入力が増える（関連する記憶だけを選ぶ仕組みはない）
- 記憶は追記のみ。更新・取り消し・矛盾解消はなく、名前を言い直すと新旧両方の記憶が残る
- 重複排除は `text` の完全一致だけ
- 記憶に日時がないため、「今回の」のような相対的な表現がいつのことか分からなくなる
- 1ターンあたり Claude Code を2回呼び出す（最適化していない）

## テスト

```powershell
python -m pytest
```

テストは `subprocess` / Claude をモックして実行するため、`claude` を起動せず、利用枠も消費しません。保存先には pytest の一時ディレクトリを使うため、`data/` にも触れません。

## 構成

```text
src/ai_context_memory/
  llm.py       LLM呼び出し（claude CLIをsubprocessで実行するのはここだけ）。通常回答と記憶抽出
  chat.py      ChatSession: 現在の会話の保持、1ターンの送受信、記憶の抽出結果の検証と保存
  history.py   会話原文ファイル（JSONL）の読み込みと追記
  memory.py    長期記憶ファイル（JSONL）の読み込みと追記、記憶候補の解析と検証
  cli.py       対話ループ（入力・表示・終了・エラー表示）
  __main__.py  python -m ai_context_memory の入口
tests/
  test_chat.py / test_cli.py / test_history.py / test_llm.py / test_memory.py
```
