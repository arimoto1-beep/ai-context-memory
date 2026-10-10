# MVP8: Long-term Memory に current / history を持たせる（subject / key / value）

## 今回解決したい問題

Long-term Memory（`memories.jsonl`）は追記のみで、記憶どうしの関係を持たない。

```jsonl
{"text": "ユーザーの好きな果物は梨", "origin": "user", "evidence": "私の好きな果物は梨です。"}
{"text": "ユーザーは最近はりんごが好き", "origin": "user", "evidence": "最近は好きな果物はりんごです。"}
```

この2件は「同じことについての古い値と新しい値」だが、ファイル上はただの2行で、どちらが現在の値なのか分からない。検索すれば両方がヒットし、回答用の Claude は文面（「最近は」）から推測するしかない。

Working Memory では MVP6・MVP7 で「現在状態」と「変更履歴」を分けた。Long-term Memory にはまだそれが無い。

## 目的

Long-term Memory の一部に、`subject` / `key` / `value` という「意味上の住所」を持たせる。同じ `subject` + `key` に新しい `value` が来たら、

- current は新しい value にする
- 古い value は history に残す

と分けて保持する。

確認したい仮説は2つある。

1. AI に「この発言は誰の、何という属性についての情報か」を判断させれば、`subject` + `key` で意味上の同一性を表せる。現在ある key を AI に見せれば、同じ意味の情報を同じ key へ分類できる
2. Python は意味を判断せず、「同じ subject + key に違う value が来たら current を置き換え、古い value を history へ残す」という状態遷移だけを確実に行える

役割分担は「AI = 意味・分類・同一性の判断」「Python = 状態遷移・検証・永続化」である。

## 成功条件

| # | 状況 | 期待 |
|---|---|---|
| 1 | 「私の好きな果物は梨です。」 | current に `user` / `favorite_fruit` / `梨` が保存される。history に add イベントが残る |
| 2 | 続けて「最近は好きな果物はりんごです。」 | AI が既存の `favorite_fruit` を再利用する。current は `りんご` になり、梨は current に残らない。history は add 梨 → replace 梨→りんご |
| 3 | 「好きな飲み物はコーヒーです。」 | 別の key（`favorite_drink`）として追加される。`favorite_fruit` は変わらない |
| 4 | 再起動して「今、私の好きな果物は？」 | 検索プランナーが Long-term Memory を選ぶ。current から `favorite_fruit = りんご` を取得し、「りんご」と答える。`memories.jsonl` に「好きな果物は梨」が残っていても、current のりんごを優先する |
| 5 | 「前に好きだった果物は？」 | AI が Long-term Memory History が必要と判断する。history を検索し、梨→りんご の変更を根拠に「以前は梨、現在はりんご」と、推定ではなく履歴を根拠に答える |
| 6 | current が `りんご` の状態で「好きな果物はりんごです。」 | AI が同じ subject / key / value を返したら、Python は current を書き換えず、history にも何も追加しない |
| 7 | 「1+1は？」「今日は眠いです」 | 構造化候補は無し（`[]`）。無理に subject / key / value を作らない |

加えて:

- MVP7 までの挙動（従来の Long-term Memory の検索と再検索、Working Memory の add / replace / remove、Working Memory History の必要時検索）は変わらない
- 抽出の失敗・不正な JSON・保存の失敗で、会話は止まらない
- History が必要かどうかの判断のために、Claude 呼び出しを増やさない（既存の検索プランナー1回の中で判断する）

成功条件にしないが観察すること: current が `favorite_fruit = りんご` の状態で「最近一番好きなフルーツはりんごです。」と言ったとき、AI が `favorite_fruit` を再利用するか、別の key を作るか。結果はそのまま記録する。

## subject / key / value の意味

| 項目 | 意味 | MVP8 での扱い |
|---|---|---|
| `subject` | 誰（何）についての情報か | `user` だけ。AI に出力させるが、Python が `user` 以外を拒否する |
| `key` | その subject の、何という属性か | AI が意味を判断して決める。英語の snake_case（例: `name`、`favorite_fruit`、`favorite_drink`） |
| `value` | その属性の、現在の値 | ユーザーの言葉のままの短い値（例: `梨`） |
| `evidence` | 根拠となったユーザー発言の抜き出し | 今回の発言に一字一句含まれること |

`subject` + `key` が「意味上の住所」で、1つの住所には current の値が1つだけある。

key を決めるのは AI である。Python に「果物なら favorite_fruit」のような対応表やキーワード規則は持たない。Python が key について確認するのは形式（`^[a-z][a-z0-9_]*$`、64文字以内）だけである。

## current / history の役割

| | Structured Long-term Memory Current | Long-term Memory History |
|---|---|---|
| ファイル | `data/long_term_memory_state.json` | `data/long_term_memory_history.jsonl` |
| 答える問い | ユーザーについて、今有効な値は何か | その値がいつ・どう変わったか |
| 内容 | subject + key ごとに現在の value を1つ | add / replace のイベント（追記のみ） |
| 置き換えられた古い値 | 残さない | `old_value` として残る |
| 現在値として扱ってよいか | よい | 不可（過去の値） |
| Claude へ渡すとき | AI が Long-term Memory を必要と判断し、検索でヒットした項目だけ（最大5件） | AI が History を必要と判断し、検索でヒットしたイベントだけ（最大10件、古い順） |

current は Working Memory と違い、毎回すべては渡さない。ユーザーについての情報は増え続け、大半のターンには関係しないので、MVP4 以来の「必要なときだけ検索する」方針に従う。

### 今回は「更新」と「訂正」を区別しない

「好みが梨からりんごに変わった」（世界が変わった）と、「さっきの梨は言い間違いで、りんごでした」（発言の訂正）は本来意味が違う。MVP8 では区別せず、同じ subject + key に違う value が来たら、どちらも replace として current を更新し、古い value を history へ残す。引っ越し・訂正・別物・superseded・retracted の分類もしない。

## AI と Python の役割分担

| 担当 | やること |
|---|---|
| AI（構造化抽出） | 発言の中に、ユーザー自身の長く有効な属性があるかを判断する。あれば subject / key / value / evidence の候補を出す。現在ある key と同じ意味なら、その key を再利用する。別の意味なら別の key を作る |
| Python | 候補の検証、subject + key による add / replace / no-op の判定、current の書き換え、history への追記、検索、件数の上限 |
| AI（検索プランナー） | Long-term Memory が必要か、Long-term Memory History が必要か、Working Memory History が必要かを判断し、検索語を考える |
| AI（通常回答） | 取得した current・history・従来の記憶を、優先順位に従って使い分けて答える |

AI は状態遷移を指定しない。候補に `operation` は無く、「追加か置き換えか」は Python が current を見て決める。

- current に同じ subject + key が無い → **add**
- 同じ subject + key があり、value が同じ → **no-op**（current も history も変えない）
- 同じ subject + key があり、value が違う → **replace**（current を新しい value にし、古い value を history の `old_value` に残す）

value が「同じ」かどうかは、英字の大文字小文字・全角半角・空白の違いだけを無視した完全一致で判定する（Working Memory の重複判定と同じ）。「りんご」と「リンゴ」が同じ意味かどうかは Python では判断しない。

## 従来の memories.jsonl との関係

- `memories.jsonl` はそのまま残す。形式も、抽出も、検索も変えない
- MVP8 より前に保存された記憶の migration / backfill はしない。構造化 Long-term Memory に入るのは、MVP8 以降の発言だけである
- 構造化 Long-term Memory は別レイヤーとして追加する。抽出も別の Claude 呼び出しで行う
- 同じ内容が `memories.jsonl` と current の両方に入ることは許容する。重複排除はしない
- 検索プランナーが Long-term Memory を必要と判断したら、同じ検索語で `memories.jsonl` と current の両方を検索する
- 回答時に両者が矛盾したら、current を優先する。`memories.jsonl` には古い値・後で変わった値が混ざりうることを、回答用の Claude に伝える

## 実装方針

### 保存形式

current（`data/long_term_memory_state.json`）:

```json
{
  "items": [
    {
      "subject": "user",
      "key": "favorite_fruit",
      "value": "りんご",
      "evidence": "最近は好きな果物はりんごです。",
      "updated_at": "2026-10-09T10:01:00+09:00"
    }
  ]
}
```

history（`data/long_term_memory_history.jsonl`、追記のみ、1行1イベント）:

```jsonl
{"timestamp": "2026-10-09T10:00:00+09:00", "operation": "add", "subject": "user", "key": "favorite_fruit", "value": "梨", "evidence": "私の好きな果物は梨です。"}
{"timestamp": "2026-10-09T10:01:00+09:00", "operation": "replace", "subject": "user", "key": "favorite_fruit", "old_value": "梨", "value": "りんご", "evidence": "最近は好きな果物はりんごです。"}
```

- `updated_at` と `timestamp` は同じ形式（ローカル時刻、ISO 8601、秒まで）で、Working Memory History の `timestamp` と揃える。1つの変更では、current の `updated_at` と history の `timestamp` は同じ値になる
- `old_value` は replace のイベントにだけある
- 保存の順序は current → history（Working Memory と同じ）。current の保存に失敗したら、そのターンは current も history も変えない。history の追記だけ失敗したら警告を出し、current の変更は有効なままにする
- 保存場所は環境変数 `ACM_LONG_TERM_MEMORY_STATE_FILE` / `ACM_LONG_TERM_MEMORY_HISTORY_FILE` で変えられる
- current のファイルが壊れている（JSON でない、形式が違う、同じ subject + key が複数ある）場合は、起動時にエラーを表示して中止する（Working Memory と同じ）

### 構造化抽出（AI）

通常回答のあと、従来の記憶抽出とは別の Claude 呼び出しで行う。入力は、current の全項目（subject / key / value）と、今回のユーザー発言だけ。

```text
[long-term memory state]
- user.favorite_fruit: 梨
[/long-term memory state]

[utterance]
最近は好きな果物はりんごです。
```

出力は候補の JSON 配列。候補が無ければ `[]`。

```json
[{"subject": "user", "key": "favorite_fruit", "value": "りんご", "evidence": "最近は好きな果物はりんごです"}]
```

抽出AIへの指示に載せる key の例には、成功条件で使う key（`favorite_fruit`、`favorite_drink`）を含めない（`name`、`hometown`、`favorite_color` を使う）。例に書いてあったから同じ key になったのか、意味を判断して同じ key を選んだのかを区別できなくなるためである。

候補にしないもの: 質問、挨拶、その場限りの依頼、一時的な気分や体調、現在進行中の作業の目的・対象・完了条件（これは Working Memory の役割）、過去の値だけを述べた発言。迷う場合は候補にしない。

### Python が行う検証

- 抽出結果全体が JSON 配列であること
- `subject` が `user` であること
- `key` が空でなく、`^[a-z][a-z0-9_]*$`・64文字以内であること
- `value` が空でない文字列であること
- `evidence` が空でない文字列で、今回のユーザー入力に部分文字列としてそのまま含まれること
- 1回の抽出結果の中で、同じ subject + key の候補が複数ある場合は、最初の1つだけを使う（1つの発言で自分自身を置き換えないため）

検証を通らなかった候補は警告を表示し、その候補だけ反映しない。value の意味が正しいかどうかは Python では判断しない。

### 検索プランナーの拡張

MVP7 の検索プランナーに、Long-term Memory History の判断を足す。Claude 呼び出しは増やさない。

```json
{
  "needs_memory": true,
  "queries": ["果物"],
  "needs_long_term_memory_history": true,
  "memory_history_queries": ["favorite_fruit", "果物"],
  "needs_working_memory_history": false,
  "history_queries": []
}
```

| キー | 内容 |
|---|---|
| `needs_memory` / `queries` | Long-term Memory（従来の記憶と current の両方）が必要か、その検索語 |
| `needs_long_term_memory_history` / `memory_history_queries` | Long-term Memory History が必要か、その検索語（最大3個） |
| `needs_working_memory_history` / `history_queries` | Working Memory History が必要か、その検索語（MVP7 から変更なし） |

- Long-term Memory History の2つのキーが無い出力（MVP7 形式）は、不要として扱う
- 検索プランナーへ渡す情報は MVP7 と同じ（Current Working Memory、現在の発言、再検索時は0件だった検索語）。current や history の中身は渡さない
- 検索プランナーには、current と history が持つ項目と、key が英語の snake_case であること、value と evidence はユーザーの言葉のままであることを説明する
- 検索できるもの（従来の記憶、current、2つの history）が1つも無いときは、検索プランナーを呼ばない

### 検索（Python）

どちらも単純な文字列検索で、ヒットの判定とスコアは MVP4・MVP7 と同じ（1つの検索語の中は AND、検索語どうしは OR、英字の大文字小文字と全角・半角はそろえる）。

| | 検索対象 | 上限 | 並び |
|---|---|---|---|
| current | `subject` / `key` / `value` / `evidence` | 5件 | スコアの高い順、同点は保存順 |
| Long-term Memory History | `operation` / `subject` / `key` / `value` / `old_value` / `evidence` | 10件（超えたらスコアの高い順、同点は新しい順に残す） | 選んだあとで古い順 |

- current は、従来の記憶と同じ検索語（`queries`）で検索する。両方とも0件のときだけ、1回だけ検索語を変えて再検索する（MVP4 の再検索を2つの検索先に広げたもの）
- Long-term Memory History の検索は1ターンに1回だけで、再検索しない（Working Memory History と同じ）

### 回答用の Claude へ渡す形式と優先順位

渡す順序（無いブロックは省く）:

1. `[working memory]` — Current Working Memory（常に、全項目）
2. `[working memory history]` — 検索でヒットした Working Memory History
3. `[long-term memory state]` — 検索でヒットした current
4. `[long-term memory history]` — 検索でヒットした Long-term Memory History
5. `[retrieved memories]` — 検索でヒットした従来の記憶
6. 今回起動してからの会話と、現在のユーザー入力

作業についてのもの（1・2）と、ユーザーについてのもの（3・4・5）をそれぞれまとめ、どちらも「現在状態 → 履歴」の順に並べる。

```text
[long-term memory state]
- user.favorite_fruit: りんご (evidence: 最近は好きな果物はりんごです。, updated_at: 2026-10-09T10:01:00+09:00)
[/long-term memory state]

[long-term memory history]
- 2026-10-09T10:00:00+09:00
  operation: add
  subject: user
  key: favorite_fruit
  value: 梨
  evidence: 私の好きな果物は梨です。

- 2026-10-09T10:01:00+09:00
  operation: replace
  subject: user
  key: favorite_fruit
  old_value: 梨
  value: りんご
  evidence: 最近は好きな果物はりんごです。
[/long-term memory history]
```

システム指示で伝える優先順位:

- 現在の会話でのユーザー発言が最優先（これまでと同じ）
- Working Memory は現在の作業状態（これまでと同じ）
- `[long-term memory state]` は、ユーザーについて現在有効な値である
- `[long-term memory history]` は過去の値の変更イベントで、現在の値ではない。`old_value` や古いイベントの value を現在の値として扱わない
- 過去の値・変更の時期については `[long-term memory history]` を根拠にし、推定としてではなく履歴を根拠として答える
- `[retrieved memories]`（従来の記憶）には、古い値や後で変わった値が混ざりうる。`[long-term memory state]` と矛盾したら、`[long-term memory state]` を優先する
- Working Memory History は作業状態の変更履歴である（これまでと同じ）

### CLI 表示

```text
[長期状態] user.favorite_fruit: 梨              ← add
[長期状態] user.favorite_fruit: 梨 → りんご     ← replace
[長期履歴検索] favorite_fruit / 果物            ← Long-term Memory History を検索したターンだけ
[長期履歴検索結果] 2件
```

no-op のときは何も表示しない。current の検索結果は、従来の記憶の検索結果と合わせて `[検索結果] N件` に含める。

### Claude 呼び出し回数

構造化抽出の分、1ターンあたり1回増える。今回は最適化しない。

| 状況 | 1ターンあたり | MVP7 との差 |
|---|---|---|
| 検索できるものが何も無い（初回など） | 4回（通常回答・長期記憶の抽出・構造化抽出・作業記憶の抽出） | +1 |
| 通常（検索なしと判断された場合も含む） | 5回（検索プラン作成 + 上記4回） | +1 |
| Long-term Memory の1回目の検索が0件 | 6回（上記 + 検索語再生成） | +1 |

### エラー処理

| 状況 | 動作 |
|---|---|
| 構造化抽出の結果が不正な JSON・呼び出し失敗 | 警告を表示。そのターンは current を更新しない |
| 候補が検証を通らない | 警告を表示。その候補だけ反映しない |
| current の保存に失敗 | 警告を表示。current も history も変えない |
| history の追記に失敗 | 警告を表示。current の変更は有効なまま |
| history ファイルが無い・0件 | イベント0件として扱い、履歴ブロックなしで回答へ進む |
| history ファイルが壊れていて読めない | 警告を表示し、履歴なしで回答へ進む |

いずれも会話は止めない。

## 今回やらないこと

- 既存 `memories.jsonl` の migration・backfill、従来の記憶との重複排除
- `subject = user` 以外（他人・会社・車・プロジェクトなど）
- 更新と訂正の区別、引っ越し・訂正・別物の分類、superseded、retracted、purge
- Long-term Memory の remove（「もう好きではない」で current から消す）
- key の階層化、key 辞書の手動管理、ontology、relation graph
- Vector DB、embedding、semantic similarity による key の統合
- key の揺れを見つけたときの自動統合
- AI が current を直接書き換えること
- Working Memory 側の大きな変更、Task lifecycle
- Claude 呼び出し回数の最適化
- GUI、MCP

## 評価方法

- 自動テスト（pytest）: Claude をモックし、保存先は `tmp_path`。current と history の読み書き、候補の検証、add / replace / no-op の判定、current と history の検索、質問の種類ごとに history 検索が呼ばれる・呼ばれないこと、回答用プロンプトと指示、失敗時に会話が続くこと、MVP7 までの挙動が変わらないことを確認する。
- 手動スモークテスト: 実際の `claude` と一時データで、梨 → りんご → コーヒーと入力したあと再起動し、現在値・過去値・別 key・検索不要の質問を順に確認する。AI が実際に出した subject / key / value と検索プランを記録する。追加で「最近一番好きなフルーツはりんごです。」の key を観察する。
