# MVP7: Working Memory History を必要なときだけ検索する

## 今回解決したい問題

MVP6 で、Working Memory の変更（add / replace / remove）を `working_memory_history.jsonl` へ残すようにした。しかし履歴は保存するだけで、回答用の Claude へは渡していない。

実際の Claude で次の状態まで進めた。

```text
Current Working Memory（scope）: 商用環境 / 開発環境 / ステージング環境

Working Memory History:
  add      検証環境
  replace  検証環境 → ステージング環境
  remove   ステージング環境
  add      ステージング環境
```

ここで「ステージング環境を戻す直前の対象環境は何でしたか？」と聞くと、Claude は「商用環境・開発環境」と正解した。ただし「履歴そのものを見たわけではなく、Long-term Memory と現在状態からの推定です」という回答だった。

答えの根拠は履歴ファイルに存在するのに、回答用の Claude へ届いていない。かといって履歴を毎回すべて渡せば、現在状態だけで答えられる質問にも過去のイベントが混ざる。

## 目的

Working Memory History を「必要なときだけ検索して使う記憶ソース」にする。

- 現在状態についての質問なら、Working Memory だけを見る
- 過去の状態・変更の経緯・変更の時刻が必要なら、Working Memory History を検索し、ヒットしたイベントだけを回答用の Claude へ渡す

確認したい仮説は「記憶を取得する手段を複数用意し、どれを使うべきかを AI 自身に判断させられるか」である。今回は History という新しい手段を1つ足し、それを必要なときだけ使えるかだけを確認する。

## 成功条件

共通の前提（手動スモークテストの状態）:

```text
Current Working Memory（scope）: 商用環境 / 開発環境 / ステージング環境
History: add 商用・検証・開発 → replace 検証→ステージング → remove ステージング → add ステージング
```

| # | 質問 | 期待 |
|---|---|---|
| 1 | この作業の対象環境は？ | History を検索しない。商用環境 / 開発環境 / ステージング環境と答える |
| 2 | ステージング環境を戻す直前の対象環境は何でしたか？ | History を検索する。remove / add などのイベントが回答用の Claude へ渡る。商用環境 / 開発環境と、推定ではなく履歴を根拠に答える |
| 3 | 検証環境をステージング環境に変えたのはいつ？ | History を検索する。replace イベントの timestamp を根拠に答える |
| 4 | 対象環境はどう変わってきた？ | History を検索する。取得したイベントを時系列で説明する。History に無い出来事を補わない |
| 5 | 私の好きな果物は？（Long-term Memory に「梨」がある） | Long-term Memory を検索する。History は検索しない |
| 6 | 1+1は？ | Long-term Memory も History も検索しない |

加えて:

- 履歴ファイルが無い、または History 検索が0件でも、会話は止まらない。必要なら「履歴からは確認できない」と答えられる
- MVP6 までの挙動（add / replace / remove、Long-term Memory の検索と0件時の再検索）は変わらない
- History 検索の判断のために、Claude 呼び出しを毎ターン増やさない

## 3つの記憶ソースの役割

| | Current Working Memory | Long-term Memory | Working Memory History |
|---|---|---|---|
| ファイル | `working_memory.json` | `memories.jsonl` | `working_memory_history.jsonl` |
| 答える問い | 今どうなっているか | ユーザーが過去に何を述べたか・決めたか | 作業状態がいつ・どう変わったか |
| 中身 | 現在有効な mission / scope / acceptance_criteria | ユーザー発言から抽出した短い文と根拠 | add / replace / remove のイベント（時刻つき） |
| Claude へ渡すとき | 常に全項目 | AI が必要と判断し、検索でヒットしたものだけ（最大5件） | AI が必要と判断し、検索でヒットしたものだけ（最大10件） |
| 現在状態として扱ってよいか | よい（最優先） | 不可（後で変更・取り消しされた内容を含む） | 不可（過去のイベントであって現在値ではない） |
| 時刻 | 無い | 無い | ある |

矛盾したときの優先順位:

- 現在状態については、常に Working Memory を優先する
- 過去の状態・変更の理由・変更の時刻については、History を根拠にする
- Long-term Memory は過去の発言や決定を含むので、Working Memory と矛盾したら Working Memory を優先する（MVP6 と同じ）

## AI と Python の役割分担

| 担当 | やること |
|---|---|
| AI（検索プランナー） | 現在の発言と Current Working Memory を見て、Long-term Memory が必要か・History が必要かをそれぞれ判断し、必要なものの検索語を考える |
| Python | 検索プランの解析と検証、History の文字列検索、件数の上限、時系列への並べ替え、検索回数の制御 |
| AI（通常回答） | Current Working Memory と取得したイベントから、過去の状態や経緯を読み取って答える |

Python は発言の意味を解釈しない。「直前」「以前」「いつ」「どう変わった」のような語を見て History を検索する、といった分岐は書かない。Python が見るのは、AI が出した検索プランの `needs_working_memory_history` と `history_queries` だけである。

過去のある時点の状態を Python が組み立てることもしない（State Replay Engine は作らない）。現在状態と取得したイベントから過去の状態を推論するのは AI の役割とする。

## History を毎回渡さない理由

- 履歴は追記のみで増え続ける。毎回渡すと入力が際限なく増える
- 大半の質問は現在状態だけで答えられる。過去のイベントを常に見せると、取り消された古い値を現在値と取り違える余地が増える（MVP6 で「否定情報を現在状態に残さない」ようにしたことと逆行する）
- 今回確認したいのは「AI が記憶ソースを使い分けられるか」であり、全部渡してしまうと確認できない

## 実装方針

### 検索プランナーの拡張

MVP4 の検索プランナーを拡張し、1回の呼び出しで Long-term Memory と History の両方について判断させる。History 専用の Claude 呼び出しは追加しない。

出力（JSON オブジェクト）:

```json
{
  "needs_memory": false,
  "queries": [],
  "needs_working_memory_history": true,
  "history_queries": ["ステージング環境", "検証環境 ステージング環境"]
}
```

| キー | 内容 |
|---|---|
| `needs_memory` / `queries` | Long-term Memory が必要か、その検索語（MVP4 から変更なし） |
| `needs_working_memory_history` | History が必要か |
| `history_queries` | History の検索語（最大3個） |

- `needs_working_memory_history` が false なら、`history_queries` があっても使わない
- History 側の2つのキーが無い出力（MVP4 形式）は、History 不要として扱う
- 検索語の整理（空白の正規化、重複の除去、上限3個）は Long-term Memory と同じ

### 検索プランナーへ渡す情報

- Current Working Memory（項目が1つも無ければブロックごと省く）
- 現在のユーザー入力
- 再検索のときだけ、0件だった Long-term Memory の検索語（MVP4 と同じ）

History そのもの、Long-term Memory の中身、会話履歴は渡さない。Working Memory を見せるのは、「現在状態だけで答えられる質問か、過去の変更を見る必要があるか」を判断できるようにするためである。

検索プランナーには、History の1イベントが持つ項目（operation / field / text / target / evidence）と、その値の例（add / replace / remove、mission / scope / acceptance_criteria）も説明する。検索は部分一致なので、「scope に対する変更すべて」を取りたければ `scope` を検索語にする、といった戦略を AI が立てられるようにするためである。

### プランナーを呼ぶ条件

Long-term Memory が1件も無く、かつ History のイベントも1件も無いときは、検索するものが無いので検索プランナーを呼ばない（MVP4 の「記憶が無ければ呼ばない」を2ソースに広げたもの）。どちらかがあれば呼ぶ。

### History の検索

ベクトル検索は使わない。`working_memory_history.jsonl` をターンごとに読み込み、Python で単純に文字列検索する。

- 検索対象: 各イベントの `operation` / `field` / `text` / `target` / `evidence`
- 1つの検索語は空白区切りのキーワードで、キーワードをすべて含むイベントがヒットする（AND）
- 複数の検索語は、どれかにヒットすればよい（OR）
- 英字の大文字小文字と全角・半角の違いは無視する（Long-term Memory の検索と同じ正規化）
- スコアは、ヒットした検索語のキーワード数の合計（Long-term Memory と同じ）
- 上限は10件。超える場合はスコアの高い順、同点なら新しいイベントを優先して選ぶ
- 選んだイベントは、最後に古い順（履歴ファイルの順 = 時刻順）へ並べ直して返す

History の検索は1ターンに1回だけ行う。0件でも検索語を変えて再検索はしない。Long-term Memory 側の「0件なら1回だけ検索語を変える」は維持するが、2回目のプランに含まれる History の指定は使わない。

### 回答用の Claude へ渡す形式

```text
[working memory history]
- 2026-10-07T18:48:19+09:00
  operation: replace
  field: scope
  target: 検証環境
  text: ステージング環境
  evidence: 検証環境の代わりにステージング環境を対象にします。

- 2026-10-07T18:48:45+09:00
  operation: remove
  field: scope
  text: ステージング環境
  evidence: ステージング環境はやっぱり対象外にします。
[/working memory history]
```

`timestamp` は保存されている値をそのまま渡す。`target` は replace のイベントにだけある。

渡す順序:

1. Current Working Memory（常に、全項目）
2. 検索でヒットした Working Memory History（検索したターンだけ。0件ならブロックを省く）
3. 検索でヒットした Long-term Memory
4. 今回起動してからの会話
5. 現在のユーザー入力

システム指示に次を追加する。

- Working Memory History は、作業記憶に対する過去の変更イベントを検索で取得したもので、古い順に並んでいる
- 各項目の意味（text は add なら追加した項目、replace なら置き換え後、remove なら取り除いた項目。target は置き換え前）
- History は過去のイベントであって現在の状態ではない。History にある値を現在の対象や条件として扱わない
- 現在の状態については、常に Working Memory を優先する
- 過去の状態・変更の理由・変更の時刻については、History を根拠にする。現在の Working Memory と取得したイベントから読み取れることは、推定としてではなく履歴を根拠として答える
- ブロックにあるのは検索でヒットしたイベントだけで、全部ではない。ブロックに無い出来事を補わない
- 必要な過去の変更がブロックに見当たらなければ、推測せず、履歴からは確認できないと伝える

### CLI 表示

History を検索したターンだけ表示する。イベントの中身は表示しない。

```text
[履歴検索] ステージング環境 / 検証環境 ステージング環境
[履歴検索結果] 3件
```

### Claude 呼び出し回数

History の検索は Python の処理なので、Claude 呼び出しは増えない。

| 状況 | 1ターンあたり | MVP6 との差 |
|---|---|---|
| Long-term Memory も History も無い | 3回（通常回答・長期記憶の抽出・作業記憶の抽出） | 同じ |
| 通常 | 4回（検索プラン作成・通常回答・長期記憶の抽出・作業記憶の抽出） | 同じ |
| Long-term Memory の1回目の検索が0件 | 5回（上記 + 検索語再生成） | 同じ |
| Long-term Memory が無く History だけある | 4回 | +1（MVP6 ではプランナーを呼ばなかった） |

最後の行は、作業の条件を述べた発言から Long-term Memory が1件も抽出されなかった場合にだけ起きる。

### エラー処理

| 状況 | 動作 |
|---|---|
| 履歴ファイルが無い | イベント0件として扱う |
| History 検索が0件 | `[履歴検索結果] 0件` を表示し、History ブロックなしで回答へ進む |
| 履歴ファイルが読めない・JSON として壊れた行がある | 警告を表示し、History なしで回答へ進む |
| 検索プランが不正な JSON・呼び出し失敗 | 警告を表示し、Long-term Memory も History も検索せず回答へ進む（MVP4 と同じ） |

いずれも会話は止めない。

## 今回やらないこと

- History 全文を毎回 Claude へ渡す
- History のベクトル検索・embedding
- State Replay Engine（任意時点の状態を Python で再構築する）
- History から Current Working Memory を作り直す
- History の編集・削除、Event Sourcing 基盤
- MVP6 導入前の変更の backfill
- History 検索の再試行・Agent ループ
- Long-term Memory の superseded、current / history 化
- subject / key / value 化、relation graph
- Working Memory の Task ID、複数タスク、作業の終了・切り替え
- GUI、MCP

## 評価方法

- 自動テスト（pytest）: Claude をモックし、保存先は `tmp_path`。検索プランの解析、History 検索、回答用プロンプトに検索結果だけが入ること、質問の種類ごとに History 検索が呼ばれる・呼ばれないこと、履歴が無い・0件のときも会話が続くこと、MVP4・MVP6 の挙動が変わらないことを確認する。
- 手動スモークテスト: 実際の `claude` と一時データで、作業の条件を add → replace → remove → add と変えたあと再起動し、成功条件の6つの質問を順に確認する。各質問で生成された検索プランも記録する。
