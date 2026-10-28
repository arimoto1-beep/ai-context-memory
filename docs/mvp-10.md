# MVP10: Pending（保留）を確認して解決する

状態: **実装済み・固定済み**（コミット `98e9feb`、タグ `article-12`）。誤った解決の取り消しは MVP11 で `/revert` として追加した（[mvp-11.md](mvp-11.md)）

## 1. 目的と対象範囲

MVP9 では、新しい発言と既存の値の関係を確定できないとき、Current を変えずに Pending として保存した。しかし Pending は保存・検索されるだけで、確認して解決する手段が無かった。確認質問も表示するだけだった。

MVP10 では次の流れを動かす。

1. 未解決の Pending がある
2. 利用者が `/confirm p001` で確認を始め、Python が保存済みの確認質問を表示する
3. ユーザーが回答する
4. AI が、回答が質問にどう答えているかを判断する（coexist / replace / unresolved / not_an_answer）
5. Python が安全条件を検証し、許された状態遷移だけを実行する。replace は実行前に y/N で承認させる
6. 表示した質問・回答・判断・検証結果・変更計画・承認を、根拠として確認ログに残す
7. 再起動後も、解決した状態と根拠を確認できる

**AI が判断し、Python が状態遷移を制御する** 方針は変えない。

### 対象範囲

- 確認は **利用者がコマンドで明示的に始める** ものだけ。AI が自分から確認質問を始めること、質問のタイミングの判断はしない
- 解決できるのは、既存の値（`target_key` / `target_value`）と新しい値（`proposed_value`）の両方を持ち、その既存の値が今も Current にある Pending だけ
- subject は `user` だけ。属性の種類（住まい・好み・勤務先など）には依存しない

### 基本シナリオ

```text
Current:  user.home_location = 川崎
Pending:  p001「今は名古屋に住んでいます。」（proposed_value: 名古屋）
質問:     川崎の住まいも残っていますか？
```

| ケース | 回答 | 結果 |
|---|---|---|
| A 共存 | はい、残っています。 | 川崎はそのまま。`home_location_2 = 名古屋` を追加。p001 は resolved（coexist） |
| B 置換 | いいえ、もうありません。→ y | `home_location: 川崎 → 名古屋`。History に replace（old_value 川崎）。p001 は resolved（replace） |
| B' 置換を承認しない | いいえ、もうありません。→ N | 何も変えない。p001 は unresolved のまま |
| C 不明 | まだ分かりません。 | 何も変えない。p001 は unresolved のまま |
| D 回答ではない | 1+1は？ | 確認を閉じ、何も変えず、入力を通常の会話として処理する |

---

## 2. 現行実装（MVP9）の確認結果

設計の前提にした事実。

- Pending は `{"items": [...]}` の JSON。読み込み時に既知のキー以外を捨て、`status` が無ければ `unresolved` を補う。`status` は常に `unresolved` で、値の検証もしていなかった
- Pending に、保留した時点の対象項目の `updated_at` は残っていなかった
- 格下げされた replace の中には、R2b（新しい値が発言に無い）で止めたもの、つまり **AI が作った値を `proposed_value` に持つ Pending がありうる**
- 確認質問は Pending 作成時に関係判断 AI が生成する。格下げで質問が無い場合は Python の汎用の質問になる。質問が既存の値に触れているかは検証していなかった
- 関係判断 AI に渡すのは Current とユーザー発言だけで、assistant の発言は渡さない。**「もうありません。」だけが届いても、何が無くなったのかは分からない**
- `conversation.jsonl` は `{"role", "content"}` の追記だけで、読み戻さない。`load_history` は余分なキーを無視する
- Current・Pending の保存は直接の上書き（`open(path, "w")`）で、途中で失敗するとファイルが書きかけになりうる。複数ファイルをまたぐ一貫性の仕組みは無かった
- History の読み込みは任意の JSON オブジェクトを受け付ける。キーを足しても既存ファイルと互換
- Pending の検索・回答への受け渡しは `status` を見ていなかった
- CLI のコマンドは `exit` / `quit` だけ

---

## 3. 当初の設計からの変更点

| 項目 | 当初の設計 | 実装 | 理由 |
|---|---|---|---|
| replace の実行 | 判断が検証を通れば実行 | **y/N の最終確認を追加**。`y`（`Y`）のときだけ実行し、それ以外の入力（`N`、`yes`、`はい`、コマンド、別の質問）はすべて「実行しない」 | 「はい」「いいえ」の取り違えで既存の値が消えるのを、ユーザーが確かめられるようにする（実装依頼 2） |
| History の質問・回答 | History に質問と回答の原文を写す | **History には `pending_id` / `confirmation_id` だけ**を持たせ、回答 AI へ渡すときに確認ログから原文を引いて付ける | 原文を複数箇所に持つと食い違いうるため（実装依頼 5） |
| 撤回 | 扱わない | AI の出力に `new_statement_status`（stands / withdrawn / unclear）を追加し、coexist / replace には `stands` を必須にした。さらに、回答に新しい値と訂正の語（間違 / 誤り / 訂正）が両方あれば、共存・置換を実行しない | 「名古屋は言い間違いでした」を共存・置換にしないため（実装依頼 7）。撤回そのものの処理は実装していない |
| 確認ログのイベント | presented / judged / applied / failed / cancelled | approval（承認）、commit（変更計画）、expired（再起動を挟んだ）を追加 | 承認を根拠に残す、計画を状態の変更より先に書く、再起動を挟んだ未承認の操作を再利用しないことを記録するため |
| 質問の表示と記録の順序（固定前の修正） | `presented` を書いてから表示し、同時に会話原文への保存と回答待ちの有効化も行う | 表示の前に **`prepared`** を書き、出力処理が正常に終わった後にだけ `presented` を書き、会話原文へ保存し、回答待ちにする。出力に失敗したら `cancelled` を書き、回答待ちにしない | 表示の前に止まると、表示していない質問が「表示済みのAI発言」として確認ログと会話原文に残っていたため |
| 起動時の復旧 | 実行前とも実行後とも違えば警告して failed | **起動を止める**（`ConfirmationError`） | 復旧できない場合は自動で続けない（実装依頼 6） |
| JSONL の追記 | 通常の追記 | 失敗したら追記前の長さへ戻す。戻せなければ以後の長期状態の書き込みを止める。読み込み時に途中で途切れた行を見つけたら起動を止める | 追記の途中の失敗を検出するため（実装依頼 6） |
| 番号付き key | Python が作る | 加えて、関係判断 AI が `_2` のような番号付きの `proposed_key` を作ることを拒否する。番号付き key を回答 AI に渡すときは `same_attribute_as` を付ける | key の衝突を防ぎ、別の属性と誤解させないため（実装依頼 3） |

---

## 4. 確認の流れと状態遷移

### 確認の開始方式

| 案 | 長所 | 短所 |
|---|---|---|
| A. 自動（回答 AI が会話の中で質問する） | 自然な会話 | 質問したか・どの Pending についてかを AI の出力から読み取る必要がある。タイミングの判断も同時に作ることになる |
| **B. 明示コマンド（採用）** | 対象 Pending・表示した質問・回答の対応を Python が構造的に保証できる。通常の会話に影響しない | 利用者がコマンドを打つ必要がある |
| C. 選択肢（番号で答える） | 完全に決定的 | 「AI が判断する」方針から外れる。自然文の回答を扱えない |

自動化（A）は、B の確認・解決の部分をそのまま使い、「いつ `start_confirmation` を呼ぶか」だけを足す形で別 MVP にできる。

### Pending の状態

| status | 意味 |
|---|---|
| `unresolved` | 未解決（MVP9 と同じ） |
| `resolved` | 確認により解決済み。`resolution` に `coexist` か `replace` |

```text
          /confirm p001（prepared → 質問を出力 → presented）
unresolved ──────────────────────────▶ 回答待ち（プロセス内だけ）
    ▲                                        │ 直後の入力を AI が判断
    │ unresolved / not_an_answer /            ▼
    │ 検証失敗 / 呼び出し失敗 / /cancel ◀── 判断 ── coexist ─────────────────▶ resolved (coexist)
    │                                         └── replace ─▶ y/N 待ち（プロセス内だけ）
    │                                                           ├── y ──▶ resolved (replace)
    └──────────────────────── N・その他の入力・終了 ─────────────┘
```

「回答待ち」「y/N 待ち」はファイルに保存しない。再起動すると消え、確認ログに `expired` を記録する。**再起動を挟んだ回答・承認は受け付けない。** 回答待ちになるのは、質問の出力処理が正常に終わり、`presented` を書けた後だけである。

### 1回の確認の確認ログ

| 結果 | イベントの並び |
|---|---|
| 共存 | prepared → presented → judged → commit → applied |
| 置換（承認） | prepared → presented → judged → approval（approved: true）→ commit → applied |
| 置換（承認しない） | prepared → presented → judged → approval（approved: false） |
| 不明・回答ではない・検証失敗・呼び出し失敗 | prepared → presented → judged |
| 回答の前・承認の前に取り消し・終了 | prepared → presented →（judged →）cancelled |
| 回答の前・承認の前にプロセスが止まった | prepared → presented →（judged →）expired（次の起動時に書く） |
| 質問の出力に失敗した（例外） | prepared → cancelled（reason: 質問を出力できませんでした） |
| 質問を出力する前にプロセスが止まった | prepared → expired（次の起動時に書く。「質問を表示した記録が無いまま終わった」と表示する） |
| 出力したが presented を書けなかった | prepared だけ（次の起動時に expired）。回答待ちにせず、CLI に確認を中止したと表示する |
| presented は書けたが、会話原文に質問を保存できなかった | prepared → presented → cancelled（reason: 会話原文に質問を保存できませんでした）。回答待ちにせず、CLI に確認を中止したと表示する |
| presented を書いた直後（会話原文へ書く前）にプロセスが止まった | prepared → presented → expired（次の起動時に書く） |
| 回答を会話原文に保存できなかった | prepared → presented → cancelled（reason: 回答を会話原文に保存できませんでした）。判断を呼ばない |
| 回答を会話原文に保存した直後（判断の前）にプロセスが止まった | prepared → presented → expired。会話原文には回答が残る |
| 変更内容の確認（y/N の文面）を会話原文に保存できなかった | prepared → presented → judged → cancelled。y/N を待たない |
| 承認の入力（y など）を会話原文に保存できなかった | prepared → presented → judged → cancelled（reason: 承認の入力を会話原文に保存できませんでした）。approval を書かず、y でも実行しない |
| 承認の入力を会話原文に保存した直後（approval を書く前）にプロセスが止まった | prepared → presented → judged → expired。会話原文には y が残るが、approval が無いので実行しない |
| 反映の途中で失敗 | … → commit → applied（続きを実行できた）／ failed（Current を変える前に止まった） |

### 解決できる Pending の条件（Python）

`/confirm` を受け付けるときと、反映する直前（共存は回答の判断の後、置換は承認の後）に確かめる。

| # | 条件 | 防ぐもの |
|---|---|---|
| S1 | `status` が `unresolved` | 解決済みの二重解決 |
| S2 | `subject` が `user`、`target_key` と `target_value` と `proposed_value` がある | 比べる相手や新しい値が無い Pending の解決 |
| S3 | Current に `subject.target_key` があり、その値が `target_value` と同じ | Pending 作成後に Current が変わった場合の、古い情報での上書き |
| S4 | Current のその項目の `updated_at` が、Pending の `target_updated_at` と同じ（MVP9 の Pending で記録が無ければ、`updated_at` が Pending の `created_at` 以前） | 値が一度変わって元に戻った場合の取り違え |
| S5 | `proposed_value` が Pending の `evidence` に現れる | R2b で止めた、AI が作った値での更新 |
| S6 | `proposed_value` が `target_value` と違う | 意味の無い解決 |
| Q4 | 対象の Current 項目が、質問を準備した時点（`prepared.current`）と同じ | 確認を始めた後の変更 |
| Q5 | ファイル上の Current と Pending が、このセッションが持っている内容と同じ | 確認中にセッションの外でファイルが書き換えられた場合の上書き |

---

## 5. 確認質問と回答の紐付け

### 質問の選び方

- Pending に保存済みの質問（Pending 作成時に関係判断 AI が生成したもの）を使う。Claude を呼び直さない
- ただし、**質問の中に `target_value` がそのまま現れる** ときだけ使う（Q1）。現れなければ、Python の汎用の質問 `以前の「川崎」は今も有効ですか？` を使う。何について尋ねたかを、質問の原文で確かめられるようにするため
- AI が生成した質問は `AI>`、Python の汎用の質問は `確認>` として表示する

### 紐付けの規則（Python が保証する）

1. `/confirm p001` を受けたら S1〜S6 を確かめ、質問を決める（`ChatSession.start_confirmation`）
2. 確認ログに **`prepared`** を書く。書けなければ表示しない。この時点では回答待ちにしない
3. CLI が質問を出力する（`cli.run_command`）
4. **出力処理が正常に終わった場合だけ**、確認ログに **`presented`** を書き、会話原文へ assistant の質問を保存し、回答待ちにする（`ChatSession.mark_presented`）。`presented` を書けない、または会話原文に保存できなければ、回答待ちにしない（会話原文の失敗では `cancelled` を書く）
5. 出力処理が例外で終わったら `cancelled` を書き（`ChatSession.abandon_prepared`）、回答待ちにせず、会話原文にも残さない。同じ `ChatSession` を使い続けても、次の入力はこの質問に結び付かない
6. **`presented` の後の、空でなくコマンドでもない最初の入力** だけを、その質問への回答とする（空の入力では待ち続ける）
7. 回答を受け取った時点で「回答待ち」を消す。同じ確認に2回目の回答は結び付かない
8. 回答待ち・y/N 待ちの間にコマンドが来たら、確認を取り消す（`cancelled`）。コマンドは回答として扱わない。y/N 待ちの間は、`y` 以外のすべての入力が「実行しない」になる
9. `exit` / `quit` で終了したら確認を取り消す。プロセスが止まった場合は次の起動時に `expired` を書く
10. 準備したまま出力されていない確認が残っている状態で次の確認を始めたら、前のものを `cancelled` にする
11. 会話原文が `load_history` で読めない（途中までの行が残っているなど）ときは、`/confirm` を受け付けない。壊れた行の後ろに確認の記録を足さないため

したがって、次のことは AI の判断に関係なく起こらない。

- 「はい」「いいえ」が、出力処理が終わっていない質問や別の Pending の質問に結び付く
- 再起動前の質問・承認に、再起動後の入力が結び付く
- 回答 AI が通常の応答の中で出した質問への回答で、Pending が解決する（その質問には `presented` が無い）

### 回答の判断（AI）

新しい Claude 呼び出し `judge_confirmation` を1回行う。入力は、確認している Pending 1つ、その既存の項目、表示した質問、回答だけ。ほかの Pending や会話は渡さない。

```text
[pending]
id: p001
subject: user
target_key: home_location
target_value: 川崎
proposed_value: 名古屋
evidence: 今は名古屋に住んでいます。
[/pending]

[long-term memory state]
- user.home_location: 川崎
[/long-term memory state]

[question]
川崎の住まいも残っていますか？
[/question]

[answer]
いいえ、もうありません。
[/answer]
```

出力は JSON オブジェクト1つ。

| キー | 内容 |
|---|---|
| `pending_id` | 入力の id をそのまま |
| `answers_question` | 回答が、この質問への答えになっているか（true / false） |
| `old_value_status` | 回答が既存の値について述べていること（continues / ended / wrong / unknown） |
| `new_statement_status` | 回答が新しい内容について述べていること（stands / withdrawn / unclear） |
| `outcome` | coexist / replace / unresolved / not_an_answer |
| `answer_evidence` | 判断の根拠を回答から一字一句そのまま抜き出したもの（not_an_answer では null） |
| `reason` | 判断理由（記録用。Python は使わない） |

プロンプトでは、否定の形の質問への「はい」「いいえ」の解釈、新しい内容の取り消し・否定・推測にとどまる回答・迷う場合を unresolved にすることを指示している。住まいの例を正解として載せていない。

### 回答・承認の保存

回答と承認の入力は、**会話原文への保存に成功してから使う**（固定前の修正）。

| 入力 | 順序 |
|---|---|
| 確認の回答 | 入力を受信 → 回答待ちを消す → **会話原文に保存**（`confirmation_event: answer`）→ AI が判断 → 確認ログに judged（回答の原文を含む）→ 共存なら commit → Current・History・Pending、置換なら変更内容の確認を **会話原文に保存** してから y/N を待つ |
| 承認の入力 | 入力を受信 → y/N 待ちを消す → **会話原文に保存**（`confirmation_event: approval`）→ 確認ログに approval（入力の原文を含む）→ y なら commit → Current・History・Pending |

- 会話原文に保存できなければ、その入力は使わない。回答なら判断を呼ばず、承認なら approval を書かず（y でも）実行しない。確認ログに `cancelled`（理由つき）を書いて確認を閉じ、次の入力は通常の会話になる
- 途中まで書いた行を取り除けなかった場合は、以後そのセッションでは長期状態の書き込みと確認を止める。書き込みを止めている間は、変更計画（commit）も書かない
- 再起動後の復旧が置換を実行するのは、approval（approved: true）と commit が確認ログにあるときだけ。会話原文に y が残っていても、approval が無ければ承認として扱わない
- not_an_answer の回答は、回答として会話原文に保存した行をそのまま使い、通常のターンでは応答だけを追記する（二重に書かない）

### 回答の後の流れ

| 判断の結果 | このターンでやること |
|---|---|
| coexist（検証を通った） | 状態遷移を実行し、結果を表示する |
| replace（検証を通った） | 変更内容を表示して y/N を待つ。この時点では何も変えない |
| unresolved | 何も変えず「保留のままにしました（判断: 不明）」と表示する。AI が withdrawn と判断していれば、撤回は扱えないことも表示する |
| not_an_answer | 確認を閉じ、**その入力を通常のターンとして処理する** |
| 検証に失敗・呼び出しに失敗 | 何も変えず「反映しませんでした: 理由」と表示する |

not_an_answer 以外の回答のターンでは、通常回答・従来の長期記憶の抽出・関係判断（構造化抽出）・作業記憶の抽出を **行わない**。同じ発言を2つの経路で反映し、二重に更新するのを防ぐためである。

**制限: 確認の回答に別の新しい情報が含まれていても（例:「いいえ。今は名古屋の栄です。」の「栄」）、MVP10 ではその情報を通常の記憶抽出に渡さない。** 長期記憶にも長期状態にも作業記憶にも反映されない。回答の原文は会話原文と確認ログには残る。必要なら、確認の後にあらためて述べてもらう。

---

## 6. Current / Pending / History の更新

### 共存の値の持ち方

| 案 | 長所 | 短所 |
|---|---|---|
| 1つの key に値のリスト | 意味がそのまま | Current の形式が変わる。既存ファイル・検索・表示・関係判断がすべて影響を受ける |
| 値を連結（「川崎、名古屋」） | 形式は同じ | 後で片方だけ終わったときに扱えない |
| AI が新しい key を付ける | 意味のある名前 | 命名が揺れる。同じ属性だと Python が分からない |
| **Python が番号付きの key を作る（採用）** | 形式を変えない。決定的。どの属性でも同じ。key 名から同じ属性だと Python が判断できる | 名前が意味を表さない。番号が主従に見える |

- key は `{target_key}_{n}`（n は2から、Current に無い最小の番号）。番号付きの key の「同じ属性」は、`_` と2以上の数字を除いた key で判断する（`attribute_base`）
- **番号は優先順位や信頼度を表さない。** 回答 AI のプロンプトでそう指示し、番号付きの key を渡すときは `same_attribute_as: home_location` を付ける
- 同じ属性の key に既に同じ値があれば、Current は変えずに Pending だけを解決する（重複を作らない）
- **キーの衝突を防ぐ**: 関係判断 AI（MVP9 の構造化抽出）が `proposed_key` に番号付きの key を出したら拒否する。番号付きの key はこの確認でだけ作る
- 検索で Current の項目がヒットしたら、同じ属性の項目をすべて一緒に回答 AI へ渡す（`related_memory_state`）
- Pending の `proposed_key`（MVP9 で AI が付けた別の key）は使わない。確認したのは `target_key` の既存の値が今も有効かなので、共存とは同じ属性に値が2つあることだから

### 置換

- `target_key` の値を `proposed_value` に置き換える。`evidence` は Pending の `evidence`（元の発言）、`updated_at` は承認した時刻
- MVP9 の replace の実行条件（R6〜R8: 旧値の終了を発言の原文で確かめる）は適用しない。原文で確かめられなかったものを、確認の対話と y/N で確かめるのが MVP10 の役目だからである。代わりに S1〜S6・Q1〜Q5・J1〜J7 を適用する

### 更新内容

| 判断 | Current | History | Pending |
|---|---|---|---|
| coexist | `{target_key}_{n}` を add（同じ値が既にあれば変えない） | add（Current を変えたときだけ） | `resolved` / `coexist` |
| replace（y） | `target_key` を置き換え | replace（old_value に旧値） | `resolved` / `replace` |
| それ以外 | 変えない | 変えない | 変えない |

### Pending に追加したキー（値はすべて文字列か null。既存ファイルと互換）

| キー | 内容 |
|---|---|
| `target_updated_at` | 保留した時点の対象項目の `updated_at`（S4 用）。MVP10 以降に作る Pending に入れる。MVP9 の Pending では null |
| `resolution` | coexist / replace。未解決は null |
| `resolved_key` | 解決で書いた（または同じ値が既にあった）Current の key |
| `resolved_at` / `confirmation_id` | 解決した時刻と、根拠の確認 ID |

- 読み込み時に `status` が `unresolved` / `resolved` 以外ならエラーにする
- MVP9 のファイルはそのまま読める。逆に、解決済みを含むファイルを MVP9 のコードで読むと、解決済みも未解決として回答に渡る（古いバージョンへ戻すことは想定しない）
- **Pending の ID は再利用しない。** 解決済みも同じファイルに残り、新しい ID は全項目の最大の番号の次になる
- 回答 AI に渡す Pending は `unresolved` だけ

---

## 7. 根拠の保存

### 確認ログ（正本）

`data/long_term_memory_confirmations.jsonl`（環境変数 `ACM_LONG_TERM_MEMORY_CONFIRMATIONS_FILE`）。1行1イベントの追記だけ。確認ごとに `c001` からの連番の `confirmation_id` を付ける。すべてのイベントが `event` / `confirmation_id` / `pending_id` / `timestamp` を持つ。

| イベント | 主な内容 |
|---|---|
| prepared | 質問を準備した（表示の前に書く）。`current`（確認対象の Current 項目とその時点の値・更新時刻）、`proposed_value`、`pending_evidence`、**`generated_question`（Pending 作成時に AI が生成した質問）**、`generated_at`、**`question`（表示する質問）**、`question_source`（pending / template）。**表示したことは意味しない** |
| presented | 質問の出力処理が正常に終わった（出力した後に書く）。`question`（出力した質問）、`question_source` |
| judged | `answer`（回答の原文）、`llm_model`、`llm_output`（Claude の生の出力）、`judgment`（解析した判断と理由）、`recovered`、`accepted` / `rejection_reason`（Python の検証結果）、`outcome`（Python が採用した判断。採用しなければ null）、`plan`（判断時点の変更計画。replace では承認待ちの提案） |
| approval | `prompt`（表示した変更内容と y/N）、`input`（入力の原文）、`approved` |
| commit | `plan`（これから実行する変更計画。実行前 `before` と実行後 `after` の対象項目を含む） |
| applied / failed | 実行の結果。`recovered: true` は、途中の失敗の後に続きを実行した・起動時に片付けたもの |
| cancelled / expired | 取り消し・再起動を挟んだ理由 |

### 何を事実として記録し、何を主張として記録するか

| 記録 | 種類 | 誰が保証するか |
|---|---|---|
| `generated_question` | AI が生成した文面（事実） | Python。Pending に保存されていたもの |
| `prepared.question` | 表示しようとした文面（事実） | Python。出力する文字列そのものを、出力の前に書く |
| `presented` | 質問の出力処理が正常に終わったこと（事実） | Python。出力の関数が例外なく戻った後に書く |
| `answer` / `approval.input` | ユーザーの入力（事実） | Python。表示の直後の入力そのもの |
| `llm_output` / `judgment` | AI の判断（**主張**） | 誰も正しさを保証しない。AI がそう判断したという事実だけ |
| `accepted` / `rejection_reason` | Python の検証結果（事実） | Python |
| `commit.plan` / `applied` | Python が実行した状態遷移（事実） | Python |

**AI がそう判断したことは記録するが、その判断が正しいとは記録しない。** `accepted: true` は「Python の検証条件を満たした」という意味で、意味が正しいことの証明ではない。

### 質問の段階

| 段階 | どこで分かるか | 根拠として使えるか |
|---|---|---|
| 生成された | Pending の `confirmation_question`、`prepared.generated_question` | 使えない（表示されていない） |
| 準備された | `prepared` | 使えない。表示したことを意味しない |
| 出力処理が正常に終わった | `prepared` の後の `presented` | 回答が無ければ使えない |
| 出力され、回答された | 同じ confirmation_id の `presented` と `judged` | 使える。状態を変えてよいかは、さらに検証・承認による |

**`presented` が保証するのは、出力の関数（CLI では `print`）が例外なく戻ったことまでである。** ユーザーが画面を見た・読んだ・理解したことは保証しない。出力をパイプやファイルへリダイレクトしている場合は、端末に届いたことも保証しない（`print` はバッファに書くだけのことがある）。

**旧形式の `presented`**: この修正の前の版は、`presented` を表示の前に書き、そこに `current` や `generated_question` も入れていた。`prepared` の無い確認の `presented` は旧形式で、表示の成功を必ずしも示さない。実データの移行はしない。扱いは次のとおり（`confirmation.presentation`）。

- 旧形式の `presented` の後に `judged` か `cancelled` があれば、出力処理は終わっていたとみなす（旧版でも、回答・取り消しは表示の処理が戻った後にしか書かれないため）
- 旧形式の `presented` だけ（`expired` で終わったものを含む）は、出力したかが分からないものとして扱う
- 確認回数（`/confirm` の「n回目」、`/pending` の「これまでの確認 n回」）は、出力処理が終わった確認だけを数える。`prepared` だけの確認と、出力したかが分からない旧形式の確認は数えない
- `/pending p001` の確認記録は、`prepared` を「質問を準備（表示したことは意味しない）」、新しい `presented` を「質問の出力処理が正常に終了」、旧形式の `presented` を「旧形式: 出力に成功したかは記録されていない」と表示する

### 原文の食い違いを防ぐ方法

質問と回答の原文は、確認ログと会話原文（`conversation.jsonl`）の2か所にある。

- **正本は確認ログ。** History には `pending_id` / `confirmation_id` だけを持たせ、原文を写さない。回答 AI へ History を渡すときに、確認ログから `confirmation_question` / `confirmation_answer` / `approval_input` を引いて付ける
- 会話原文は、ユーザーとシステムの実際のやり取りの記録として残す（実装依頼 4）。確認ログに書いたのと同じ文字列から書き、各行に `confirmation_id` と `confirmation_event`（presented / judged / approval_prompt / approval）を付ける。食い違ったら確認ログを正とする。両者が一致することは pytest で確かめている
- 会話原文に assistant の質問を書くのは、`presented` を書いた後だけ。出力できなかった質問・`presented` を書けなかった質問は会話原文に残さない
- 会話原文に質問を保存できなかった確認は、回答待ちにせず閉じる。この場合、確認ログには `presented`（出力処理は終わった）が残り、会話原文には質問が無い。両者の食い違いは確認ログの `cancelled` の理由で分かる
- 会話原文への追記は、確認ログと同じく、失敗したら追記前の長さに戻す（`append_text`）。戻せなかったときは、以後そのセッションでは長期状態の書き込みと確認を止める
- 会話原文の `approval_prompt` 行（role は assistant）は、AI ではなく Python が作った文面である（`confirmation_event` で区別できる）
- not_an_answer の入力は、回答として保存した行が会話原文に残り、通常のターンでは応答だけを足す（二重に書かない）
- 確認の質問・回答・変更内容の確認・承認の入力は、どれも会話原文に保存できたときだけ確認を先へ進める（5章）。保存できなかったものは `cancelled` の理由で分かる

### 再起動後に確認できること

- Current と History から、現在の値と、確認で変わったこと（`confirmation_id` つき）が分かる。回答 AI には、確認ログから引いた質問・回答・承認も渡る
- `/pending p001` で、その Pending の状態・解決結果と、確認ログにある全イベント（表示した質問・回答・AI の判断と理由・検証結果・承認・変更計画）を表示できる
- `/pending` で未解決の一覧と、それぞれを確認できるか（できなければ理由）、確認した回数、前回の判断を表示できる

### 実際の保存例

スモークテスト B（「いいえ、もうありません。」→ `y`）の実データ。確認ログ（1行1イベント。見やすいよう改行を入れた）。最初の2行は修正後の版で取り直したもの（2026-10-21）、judged 以降は修正前の版の実行（2026-10-19）で、judged 以降の形式は変わっていない:

```json
{"event": "prepared", "confirmation_id": "c001", "pending_id": "p001", "timestamp": "2026-10-21T08:02:38+09:00",
 "subject": "user", "target_key": "home_location",
 "current": {"subject": "user", "key": "home_location", "value": "川崎", "evidence": "今は川崎に住んでいます。", "updated_at": "2026-10-01T09:00:00+09:00"},
 "proposed_value": "名古屋", "pending_evidence": "今は名古屋に住んでいます。",
 "generated_question": "川崎の住まいも残っていますか？", "generated_at": "2026-10-14T10:00:00+09:00",
 "question": "川崎の住まいも残っていますか？", "question_source": "pending"}
{"event": "presented", "confirmation_id": "c001", "pending_id": "p001", "timestamp": "2026-10-21T08:02:38+09:00",
 "question": "川崎の住まいも残っていますか？", "question_source": "pending"}
{"event": "judged", "confirmation_id": "c001", "pending_id": "p001", "timestamp": "2026-10-19T23:23:00+09:00",
 "answer": "いいえ、もうありません。", "llm_model": null,
 "llm_output": "{\"pending_id\":\"p001\",\"answers_question\":true,\"old_value_status\":\"ended\", …}",
 "recovered": false,
 "judgment": {"pending_id": "p001", "answers_question": true, "old_value_status": "ended", "new_statement_status": "stands",
              "outcome": "replace", "answer_evidence": "いいえ、もうありません。",
              "reason": "「川崎の住まいも残っていますか？」に対して「いいえ、もうありません」と答えているため、川崎の住まいはもう有効ではありません。名古屋に住んでいるという新しい内容は取り消されていないため、replace と判断しました。"},
 "accepted": true, "rejection_reason": null, "outcome": "replace",
 "plan": {"pending_id": "p001", "subject": "user", "resolution": "replace", "resolved_at": "2026-10-19T23:23:00+09:00",
          "evidence": "今は名古屋に住んでいます。", "operation": "replace", "key": "home_location", "old_value": "川崎", "value": "名古屋",
          "resolved_key": "home_location", "before": {"value": "川崎", "updated_at": "2026-10-01T09:00:00+09:00"},
          "after": {"value": "名古屋", "updated_at": "2026-10-19T23:23:00+09:00"}}}
{"event": "approval", "confirmation_id": "c001", "pending_id": "p001", "timestamp": "2026-10-19T23:23:00+09:00",
 "prompt": "「川崎」を user.home_location の現在の値から外し、「名古屋」を現在の値として登録します（「川崎」は変更履歴に残ります）。実行しますか？（y/N）",
 "input": "y", "approved": true}
{"event": "commit", "confirmation_id": "c001", "pending_id": "p001", "timestamp": "2026-10-19T23:23:00+09:00",
 "plan": {"…": "（judged の plan と同じ形。承認した時点の状態で作り直したもの）"}}
{"event": "applied", "confirmation_id": "c001", "pending_id": "p001", "timestamp": "2026-10-19T23:23:00+09:00"}
```

History（原文は持たず、ID で確認ログを引く）:

```json
{"timestamp": "2026-10-19T23:23:00+09:00", "operation": "replace", "subject": "user", "key": "home_location",
 "old_value": "川崎", "value": "名古屋", "evidence": "今は名古屋に住んでいます。", "pending_id": "p001", "confirmation_id": "c001"}
```

Pending p001: `"status": "resolved", "resolution": "replace", "resolved_key": "home_location", "resolved_at": "2026-10-19T23:23:00+09:00", "confirmation_id": "c001"`

共存（スモークテスト A）の History:

```json
{"timestamp": "…", "operation": "add", "subject": "user", "key": "home_location_2", "value": "名古屋",
 "evidence": "今は名古屋に住んでいます。", "pending_id": "p001", "confirmation_id": "c001"}
```

---

## 8. Python が保証すること

### 検証可能な条件（AI が何を返しても成り立つ）

**対象と質問**: S1〜S6、Q1（質問に既存の値が現れる）、Q2（`prepared` を書けたときだけ出力し、出力処理が正常に終わって `presented` を書けたときだけ会話原文に残して回答待ちにする）、Q3（直後の入力だけを1回だけ回答とする）、Q4・Q5（反映直前に、表示時点・ファイル上の状態と同じ）

**AI の出力**

| # | 条件 |
|---|---|
| J1 | JSON オブジェクトとして読める（MVP9 と同じく、末尾にある唯一の JSON の取り出しだけ許す） |
| J2 | `pending_id` が確認中の Pending の id と一致する |
| J3 | `outcome` / `old_value_status` / `new_statement_status` が既知の値、`answers_question` が真偽値 |
| J4 | 組み合わせが下表に合う |
| J5 | `answers_question` が true なら、`answer_evidence` が空でなく、回答に一字一句含まれる |
| J6 | coexist / replace で、回答に新しい値と訂正の語（間違 / 誤り / 訂正）が両方あれば実行しない |
| J7 | 共存の key が KEY_PATTERN と64文字の制限を満たす |

| outcome | answers_question | old_value_status | new_statement_status |
|---|---|---|---|
| coexist | true | continues | stands |
| replace | true | ended / wrong | stands |
| unresolved | true | （問わない） | （問わない） |
| not_an_answer | false | （問わない） | （問わない） |

J4 は **AI の自己申告どうしの食い違いを見つけるだけ** で、独立した安全確認ではない。MVP9 の事前実験で分かったとおり、誤った判断とそれに合う自己申告は同時に出てくる。J6 は MVP9 の R8 と同じく必要条件の確認で、意味の判定ではない（「名古屋で間違いないです」のような正しい回答も止めるが、その場合は保留のままになるだけ）。

**承認**: replace は `y` / `Y` の入力があったときだけ実行する。承認の事実（表示した内容と入力）を確認ログに書いてから実行する。承認に Claude は呼ばない。

### 障害時の一貫性

Current・History・Pending・確認ログは別ファイルで、まとめて1回で書き換える手段は無い。次の仕組みで扱う。

| # | 仕組み |
|---|---|
| X1 | 状態を変えるのは、変更計画（`commit`）を確認ログに書けた後だけ |
| X2 | Current・Pending の保存は、一時ファイルへの書き出しと `os.replace`。ファイルが書きかけで残らない |
| X3 | 書き込みの順序は 確認ログ（commit）→ Current → History → Pending → 確認ログ（applied）。**Current の書き込みが確定点**で、Current が書けていれば残りを完了させ、書けていなければ実行しなかったことにする |
| X4 | 実行は冪等（`complete_plan`）。既に終わった手順は飛ばし、History に同じ `confirmation_id` のイベントがあれば追記しない |
| X5 | JSONL の追記は、失敗したら追記前の長さに戻す。戻せなければ（`PartialWriteError`）以後の長期状態の書き込みを止める。前回の追記が改行の前で止まっていたら、改行を足してから書く |
| X6 | 確認ログの読み込みで、読めない行（途中で途切れた最後の行を含む）があれば起動を止める |
| X7 | 反映の途中で失敗したら、その場でファイルを読み直し、X8 の規則で1回だけ片付ける。片付けられなければ、そのセッションでは長期状態の書き込み（確認・関係判断の両方）を止め、再起動を促す |
| X8 | 起動時、`commit` があって `applied` / `failed` の無い確認を片付けてから会話を始める（下表）。回答・承認の途中で終わった確認には `expired` を書く |

| 起動時の Current の対象項目 | 判断 | やること |
|---|---|---|
| 計画の実行後（値と updated_at）と同じ | Current までは書けた | History に無ければ追記し、Pending を解決済みにし、`applied`（recovered）を書く |
| 計画の実行前と同じ（共存なら key が無い） | Current を書く前に止まった | `failed` を書く。Pending は未解決のまま（計画は実行しない） |
| どちらでもない、または History・Pending が Current と食い違う | 外部で編集された等 | **起動を止める**（`ConfirmationError`）。自動では続けない |

### 保証できる状態と保証できない状態

| | 内容 |
|---|---|
| 保証できる | 確認ログに `commit` の無い変更が Current・History・Pending に入ることは無い |
| 保証できる | 会話を始める時点（起動時の片付けの後）では、確認で行った変更について、Current・History・Pending・確認ログが一致している。一致させられなければ起動しない |
| 保証できる | 同じ確認・同じ Pending を二重に反映しない（S1、X4、回答待ちの消去） |
| 保証できる | Current・Pending のファイルが書きかけで残らない（X2） |
| 保証できる | JSONL の追記の失敗は、その場で検出して取り除くか、取り除けなければ書き込みを止める。起動時には途中で途切れた行を検出して止める |
| 保証できない | プロセスが止まってから次に起動するまでの間、ファイル同士が食い違っていること（例: Current は名古屋なのに Pending は未解決）はありうる |
| 保証できない | 書き込みを止めた後、再起動するまでのセッションでは、回答 AI に渡る状態がファイルと食い違っていることがある |
| 保証できない | `fsync` の後のディスク・OS の障害、複数のプロセスが同じ `data/` を同時に使うこと |
| 保証できない | 外部でファイルを手で編集した場合の整合（検出できれば起動を止めるが、すべてを検出できるわけではない） |
| 保証できない | MVP9 の関係判断の経路（Current → History → Pending を警告つきで書く）の一貫性は MVP9 のまま。変更履歴の追記の途中失敗だけは、MVP10 で書き込みを止めるようにした |
| 保証できない | 会話原文の破損は、`/confirm` のときと、このセッションの追記の失敗でしか見つけない。起動時には会話原文を検査しないので、通常の会話はこれまでどおり続き、その後の追記は改行してから書く（壊れた行は残る）。会話原文の保存の成功は、確認を先へ進める条件にしているが、会話原文と確認ログをまとめて1回で書く手段は無い。会話原文に保存した直後に止まると、会話原文にはあるが確認ログには続きが無い入力が残る（その入力では状態を変えない） |

---

## 9. AI の判断に依存すること・防ぎきれない誤判断

Python が確かめているのは「どの Pending について、どの質問を表示し、どの回答が返ってきたか」「AI の出力の形式と組み合わせ」「状態が古くなっていないか」だけである。**回答の意味が共存なのか置換なのかは、AI の判断に頼っている。** 置換は y/N でユーザーが確かめるが、共存は確かめない。

| 誤り | 例 | 対処 |
|---|---|---|
| 「はい」「いいえ」の取り違え | 否定の質問「川崎にはもう住んでいないのですか？」への「はい」（＝住んでいない）を coexist にする | プロンプトで質問の形に合わせた解釈を指示する。replace に誤ると y/N で止められるが、**coexist に誤ると y/N なしで追加される**（既存の値は消えない） |
| 話題の変化を回答とみなす | 「名古屋の天気は？」を replace とする（「名古屋」は回答にあるので J5 を通る） | プロンプトで not_an_answer を指示する。replace なら y/N で止められる |
| 曖昧な回答を断定する | 「たぶんもう無いと思います」を replace とする | プロンプトで unresolved を指示する。replace なら y/N で止められる |
| 撤回を共存・置換にする | 「名古屋は言い間違いでした」 | `new_statement_status` を必須にし（J4）、新しい値と訂正の語があれば止める（J6）。新しい値に触れない撤回（「さっきのは嘘です」）を AI が coexist とした場合は止められない |
| 回答の中の別の新情報 | 「いいえ。今は名古屋の栄です。」の「栄」 | MVP10 では反映しない（5章の制限） |
| 番号付きの key が主従に見える | `home_location`（川崎）と `home_location_2`（名古屋） | プロンプトで番号が優先順位でないと伝え、`same_attribute_as` を付ける。回答 AI が従うかはプロンプトに頼っている |

撤回（Pending そのものを取り消す）の処理は実装していない。安全に扱えないので、AI が withdrawn と判断したものは unresolved として Pending を維持し、CLI に「取り消しは MVP10 では扱えない」と表示する。**未実装の課題として残す。**

誤った解決の取り消し（`/undo`）も作っていない。結果は CLI に必ず表示されるので、利用者は誤りに気付ける。

---

## 10. 変更したファイル

| ファイル | 変更 |
|---|---|
| `src/ai_context_memory/confirmation.py`（新規） | 確認ログの読み込み・追記・ID 採番、確認の進み具合、質問の出力処理が終わったかの判定（`presentation`。旧形式の presented を含む）、解決できるかの判定（S1〜S6）、質問の選択（Q1）、判断の解析と検証（J1〜J7）、変更計画、冪等な実行（`complete_plan`）、復旧の判定（`reconcile`）、y/N の文面 |
| `src/ai_context_memory/structured_memory.py` | Pending のキー追加と status の検証、`target_updated_at` の記録、番号付き key の補助関数（`attribute_base` / `same_attribute_items` / `related_memory_state` / `next_numbered_key`）、`proposed_key` の番号付き key の拒否、Current・Pending の原子的な保存、JSONL 追記の巻き戻し（`append_text` / `PartialWriteError`） |
| `src/ai_context_memory/chat.py` | 確認の状態（準備済み・回答待ち・y/N 待ち・書き込み停止）、`start_confirmation`（prepared まで）/ `mark_presented`（出力成功後の presented・会話原文・回答待ち）/ `abandon_prepared`（出力失敗）/ `answer_confirmation` / `approve` / `cancel_confirmation` / `pending_overview` / `pending_details`、起動時の復旧、Pending の検索を未解決だけにする、同じ属性の key の取得、History への確認の原文の付加 |
| `src/ai_context_memory/llm.py` | `CONFIRMATION_SYSTEM_PROMPT` と `judge_confirmation`、回答 AI のプロンプト（保留の確認の説明の差し替え、番号付き key、確認による変更）、関係判断 AI のプロンプト（番号付き key）、現在値の `same_attribute_as`、History の確認の項目 |
| `src/ai_context_memory/cli.py` | `/pending`・`/pending <id>`・`/confirm <id>`・`/cancel`、入力の振り分け（y/N 待ち → コマンド → 回答 → 通常のターン）、表示、確認ログのパスと環境変数、起動時の復旧の表示 |
| `tests/test_memory_confirmation.py`（新規） | MVP10 のテスト |
| `tests/test_memory_pending.py` / `tests/test_structured_memory.py` / `tests/test_cli.py` | 意図的に変えた挙動に合わせた書き換え（下記） |
| `README.md` | MVP10 の説明、コマンド、確認ログ、呼び出し回数 |

### 意図的に変えた既存のテスト

| テスト | 変更 |
|---|---|
| `test_memory_pending.py` の `NAGOYA_PENDING`、`test_pending_keeps_the_current_value_and_holds_the_statement` | Pending に `target_updated_at` と解決のキー（null）が加わった |
| `test_system_prompt_forbids_promising_to_resolve_pending` | 「確定・更新する機能はまだありません」を差し替えたので、新しい文言（コマンドで始めたときだけ確認する、応答の中で質問しても更新されない）を確かめる形にした。約束しない・質問しない指示はそのまま確かめている |
| `test_downgraded_replace_keeps_the_new_statement`、`test_pear_then_apple_then_coffee` | 確認事項の表示を「表示のみです…」から「/confirm p001 で確認できます」に変えた |
| `main()` を呼ぶテストの環境変数（3か所） | 確認ログのパスを追加した（テストが本番の `data/` を読まないように） |

---

## 11. テストの結果

### pytest（Claude をモック。保存先は `tmp_path`）

**585 passed, 2 xfailed**（MVP9 の基準 458 passed, 2 xfailed に対し、新規 127 件。うち 8 件は質問の表示と記録の順序の修正、7 件は質問の会話原文への保存の失敗、10 件は回答・承認の会話原文への保存の失敗の確認で追加。xfail は MVP9 の既知の限界のまま）。既存のテストは、10章の意図的な書き換え以外は変えずに通っている。

`tests/test_memory_confirmation.py` で確かめていること:

| 観点 | 主なテスト |
|---|---|
| `/confirm p001` の正常開始 | 保存済みの質問を表示する。確認ログは prepared → presented。Claude を呼ばない。会話原文に `confirmation_id` つきで残る |
| 表示と記録の順序 | 質問を出力している最中は、確認ログは prepared だけで、会話原文も回答待ちも無い。出力に失敗したら prepared → cancelled で、会話原文に残さず回答待ちにしない。同じセッションの次の入力も、再起動後の入力も回答として扱わない。presented を書けなければ回答待ちにしない。prepared だけ残って再起動したら expired にして続きを行わない（2回目の起動では何もしない）。出力されないまま残った準備は、次の確認を始めたときに閉じる |
| 会話原文の保存の失敗 | presented の後に会話原文へ書けない（途中までの行を取り除けた場合・取り除けなかった場合）→ prepared → presented → cancelled、回答待ちにせず、同じセッションの次の入力も回答にしない、Current・Pending・History は変わらない、提示回数には数える。取り除けなかった場合は以後の確認と書き込みを止める。会話原文の末尾が壊れていれば /confirm を受け付けない。presented の直後に止まった・会話原文の失敗の後に再起動しても続きを行わない。会話原文の追記の途中の失敗は元の長さに戻す |
| 回答・承認の保存の失敗 | 回答を会話原文に保存できない（取り除けた・取り除けなかった、共存・置換）→ 判断を呼ばず、Current・Pending・History は変わらず、次の入力は回答・承認にならず、再起動後も続きを行わない。y を保存できない → 置換しない、Pending は解決されない、History に置換が残らない、再起動後も実行しない。変更内容の確認を保存できない → y/N を待たない。y を保存した直後に止まった → 再起動後に expired で、実行しない。書き込みを止めている間は commit を書かない |
| 提示回数と確認履歴 | 出力処理が終わった確認（新形式の presented、回答・取り消しのある旧形式の presented）だけを数える。prepared だけ・旧形式の presented だけは数えない。`/pending p001` の表示が prepared / presented / 旧形式を区別する |
| 質問の選択 | 既存の値が現れない質問は汎用の質問に替え、`question_source: template` を記録する |
| 存在しない・形式の違う Pending ID | `/confirm p999`・`/confirm 001`・`/confirm` は始めない。確認ログも作らない |
| 確認開始後の再起動 | 前回の質問に、再起動後の入力は結び付かない（expired を記録し、通常の会話として処理する） |
| 共存 | `home_location_2` を add し、川崎は残る。次の空き番号（`_3`）を使う。同じ値が既にあれば重複を作らない。y/N は聞かない |
| 置換の実行前確認 | 判断の後は何も変えず、変更内容と y/N を表示する |
| 置換の承認・拒否 | `y` のときだけ実行。`N`・`n`・`yes`・`はい`・`y を押します`・コマンド・別の質問ではいずれも実行しない。終了したら取り消し。再起動を挟んだ未承認の操作は再利用しない |
| 不明 | 何も変えない。利用者がもう一度確認できる（2回目として記録） |
| 撤回 | AI が withdrawn と判断したら保留のまま表示する。AI が撤回を coexist / replace と誤っても、新しい値と訂正の語があれば実行しない |
| not_an_answer | 確認を閉じ、入力を通常のターンとして処理する（呼び出しは判断1回 + 通常のターン）。会話原文は二重にならない |
| 取り違え防止 | Pending が2つあるとき、確認した方だけを解決する。判断 AI には対象の Pending だけを渡す。別の pending_id を返した判断は採用しない。確認中のコマンドは回答として扱わない |
| Current の途中変更 | 保留後に値・更新時刻が変わっていたら始めない（MVP9 形式の Pending も）。確認中にファイルが外で書き換えられた・承認前に状態が変わったら実行しない |
| 安全に解決できない Pending | AI が作った値、新しい値が無い、既存の key が無い、解決済み。`/pending` に理由を表示する |
| 同じ回答の重複 | 解決済みの再確認は不可。同じ回答の2回目は通常の会話になり、二重に更新しない。`complete_plan` を2回呼んでも1回分 |
| 不正な LLM 出力 | JSON でない・配列・不明な値・真偽値でない・evidence が回答に無い・組み合わせの食い違い（13通り）、呼び出しの失敗では何も変えない。前置きつきの JSON は取り出して同じ検証をする |
| 更新途中の書き込み失敗 | Current の保存失敗 → 何も変えず failed。History の失敗 → その場で続きを実行して applied。Pending の保存が失敗し続ける → 書き込みを止め、再起動時に完了させる（もう一度起動しても何もしない） |
| 起動時の復旧 | commit だけ（Current 前）→ failed。Current まで → 残りを実行して applied。どちらでもない → 起動を止める。途中で途切れた確認ログの行 → 起動を止める |
| JSONL・ファイルの書き込み | 追記の失敗は元の長さに戻す。戻せなければ PartialWriteError で書き込みを止める。改行の無い末尾の後は改行してから書く。Current の保存は原子的 |
| 解決後の再起動と追跡 | `/pending p001` で全イベントを表示。History のファイルに原文は無く、回答 AI には確認ログから引いた質問・回答・承認が渡る。解決済みの Pending は回答 AI に渡らない。会話原文と確認ログの原文が一致する |
| 番号付きキー | `attribute_base`。同じ属性の key を一緒に取得し、`same_attribute_as` を付ける。関係判断 AI の番号付き `proposed_key` を拒否する |
| ID | Pending の ID を再利用しない。確認 ID の形式 |
| 通常の会話 | Pending があっても、確認中でなければ MVP9 と同じ呼び出し順。回答 AI に Pending を渡したターンでは `/confirm` を案内する |
| 住まい以外 | 好み（梨 → りんご）、勤務先（A社 → B社）で共存・置換 |
| 互換性 | MVP9 の Pending ファイルを読める。未知の status は拒否。確認ログが無くても起動できる。MVP9 の History と混在しても読める |

### 実際の Claude によるスモークテスト

実際の `claude`（Claude Code の既定モデル、サブスクリプション経由）と一時ディレクトリのデータで、36ケースを実行した（確認の判断の呼び出し 35回。L は判断を呼ばない。合計 約10分）。本番の `data/` は使っていない。初期状態は、Current `home_location = 川崎` と Pending p001「今は名古屋に住んでいます。」（質問「川崎の住まいも残っていますか？」）。I は Current `favorite_fruit = 梨` と Pending「最近は好きな果物はりんごです。」（質問「梨も今も好きですか？」）。結果を見てプロンプトを調整することはしていない（プロンプトは実行前に1回だけ書いた）。

| # | 質問 | 回答 | 回数 | AI の判断 | Python の結果 |
|---|---|---|---|---|---|
| A | 川崎の住まいも残っていますか？ | はい、残っています。 | 3 | coexist 3/3 | 3/3 で `home_location_2 = 名古屋` を add、川崎は残る |
| B | 同上 | いいえ、もうありません。→ y | 3 | replace 3/3 | 3/3 で y/N を表示し、y の後に 川崎 → 名古屋、History に replace |
| C | 同上 | まだ分かりません。 | 3 | unresolved 3/3 | 3/3 で何も変えない |
| D | 同上 | 1+1は？ | 3 | not_an_answer 3/3 | 3/3 で確認を閉じ、通常の会話で「1+1は2です。」 |
| E1 | **川崎にはもう住んでいないのですか？**（否定の質問） | はい → N | 3 | replace 3/3（正しい） | 3/3 で y/N を表示。N なので何も変えない |
| E2 | 同上 | いいえ | 3 | coexist 3/3（正しい） | 3/3 で `home_location_2` を add |
| F1 | 川崎の住まいも残っていますか？ | はい | 2 | coexist 2/2 | add |
| F2 | 同上 | いいえ → N | 2 | replace 2/2 | y/N を表示。N なので何も変えない |
| G | 同上 | 名古屋は言い間違いでした。 | 3 | unresolved 3/3（new_statement_status: withdrawn） | 3/3 で何も変えず、撤回は扱えないと表示 |
| H | 同上 | たぶんもう無いと思います。 | 2 | unresolved 2/2 | 何も変えない |
| I1 | 梨も今も好きですか？ | 梨も好きです。 | 2 | coexist 2/2 | `favorite_fruit_2 = りんご` を add |
| I2 | 同上 | 梨はもう好きじゃないです。→ y | 2 | replace 2/2 | 梨 → りんご |
| J | A の後に再起動して「今の住まいは？」 | | 2 | | 2/2 で「川崎と名古屋の2か所が記録されています。どちらも有効」と答え、どちらか一方に絞らなかった |
| K | B の後に再起動して「前はどこに住んでいた？なぜ変わったの？」 | | 2 | | 2/2 で川崎と答え、確認の質問「川崎の住まいも残っていますか？」と回答「いいえ、もうありません。」、承認を根拠に経緯を説明した。引っ越しの理由は「記録にないため分からない」とし、推測しなかった |
| L | 確認せずに「今の住まいは？」 | | 1 | | 確認の質問をせず、記録の更新も約束しなかった。CLI が `/confirm p001` を案内した |

- 判断の誤りは **0/35**。否定の質問への「はい」「いいえ」、短い「はい」「いいえ」、撤回、推測にとどまる回答、無関係な発言を、すべて期待どおりに判断した
- 不正な JSON・前置きの漏れ（取り出し）は0回。Python の検証で止めたものも0回
- **ただし、これは各ケース2〜3回の結果で、判断が常に正しいことを示すものではない。** 撤回の言い方・否定の質問の言い回しはそれぞれ1種類しか試していない
- L では、回答 AI が「どちらか一方を今の住まいとは断定できません」と答えた。未確認なのは「川崎が今も残っているか」なのに、「名古屋か川崎か」の二者択一として説明しており、MVP9 で残っていた問題がそのまま残っている（MVP10 では回答 AI のこの部分のプロンプトは変えていない）
- 質問の表示と記録の順序を直した後、A（共存）・B（置換 → y）・C（不明）を1回ずつ実際の Claude で再実行した。3件とも判断は前回と同じ（coexist / replace / unresolved）で、確認ログは prepared → presented → judged …の順になり、状態遷移も前回と同じだった
- K の回答には「保留として記録しました」「記録の更新を検討しました」のような、システムの内部処理の言い換えが含まれていた。事実と食い違うものではない

---

## 12. 成功条件

### Python が保証すること

| # | 条件 | 確認したテスト |
|---|---|---|
| M1 | Current を変えるのは、`/confirm` で表示した質問への直後の回答について、AI の判断が J1〜J7 を満たし、反映の直前に S1〜S6・Q4・Q5 を満たし、replace では y が入力されたときだけ | 共存・置換・承認・不正な判断・古い Pending のテスト |
| M2 | 存在しない・形式の違う・別の・解決済みの・古くなった Pending は解決できない | ID・取り違え・途中変更・二重解決のテスト |
| M3 | 共存では既存の値を変えない。置換では旧値が History に残る | 共存・置換のテスト |
| M4 | 不明・回答ではない・不正な出力・呼び出しの失敗・承認しない、では Current・History・Pending を変えない | 該当テスト |
| M5 | 状態を変えた解決には必ず、準備した質問・出力処理が終わったこと・回答・AI の判断・検証結果・変更計画（置換では承認）が同じ confirmation_id で確認ログに残り、History がその ID を持つ。出力処理が終わっていない質問は、表示済みとしても会話原文の AI 発言としても記録せず、回答を結び付けない | 根拠・追跡・表示と記録の順序のテスト |
| M6 | 同じ確認・同じ Pending を二重に反映しない | 重複のテスト |
| M7 | 途中で失敗しても、会話を始める前に一致させる。一致させられなければ起動しない | 書き込み失敗・復旧のテスト |
| M8 | 解決の結果と根拠は再起動後も読める。解決済みの Pending は回答 AI に渡らない。ID は再利用しない | 再起動のテスト |
| M9 | 確認中でないターンは、MVP9 と同じ流れ・同じ Claude 呼び出し回数で動く | 通常の会話のテスト |
| M10 | MVP9 までのファイルはそのまま読める。migration はしない。確認ログが無くても起動できる | 互換性のテスト |

### 実際の Claude で確認すること

スモークテストの結果を記録し、期待と違ったものは違ったまま報告する。

---

## 13. Claude 呼び出し回数

| 状況 | 回数 |
|---|---|
| 通常のターン | MVP9 と同じ 4〜6回（増えない） |
| `/pending`・`/confirm`・`/cancel` | 0回（質問は保存済みのものを使う） |
| 回答のターン（not_an_answer 以外） | 1回（確認の判断だけ。通常回答・各抽出は行わない） |
| 置換の y/N | 0回（Python が判定する） |
| 回答のターン（not_an_answer） | 1回 ＋ 通常のターン（4〜6回） |
| 起動時の復旧 | 0回 |

---

## 14. 今回やらないこと・残った問題

### やらないこと

- AI が自分から確認質問を始めること、質問のタイミングの判断
- 通常の発言（確認中でないとき）から既存の Pending を自動で解決すること
- 確認の回答の中の別の新しい情報の反映
- Pending の撤回（新しい発言の取り消し）、破棄・期限切れ
- 誤った解決の取り消し（`/undo`）
- 新しい値が分からない Pending、既存の key に関係しない Pending の解決
- 確認質問の作り直し、言い回しの改善
- 1つの key に値のリストを持たせること、key の改名・階層化
- 未来の予定の期限管理、長期記憶全体の整理・再分類、意味検索の再設計、データベースへの移行、複数プロセスでの同時利用
- Claude 呼び出し回数の最適化

### 残った問題

| 問題 | 次の手段 |
|---|---|
| 共存は y/N なしで実行するので、AI が置換を共存と誤ると、y/N なしで値が追加される（既存の値は消えない） | 共存にも確認を付けるか、誤った共存を取り消す手段（`/undo`）を別 MVP で検討する |
| 撤回（新しい発言の取り消し）は保留のままにするだけで、解決できない。新しい値に触れない撤回（「さっきのは嘘です」）を AI が誤ると止められない | 5番目の分類（withdraw）と、その実行条件を別 MVP で設計する |
| 確認の回答に含まれる別の新しい情報は記憶に反映されない | 回答のターンでも、確認の対象と別の属性だけを抽出に回す方法を別 MVP で検討する |
| 番号付きの key（`home_location_2`）は意味を表さず、主従に見える。AI が以前に `_2` で終わる key を作っていた場合、同じ属性として扱われる（MVP10 からは作らせない） | 複数値のデータモデルは別 MVP |
| Pending がある状態の回答で、「名古屋か川崎か」の二者択一として説明する（MVP9 から） | 回答 AI のプロンプトの見直しは、プロンプトを際限なく調整しない方針と合わせて別に判断する |
| プロセスが止まってから次に起動するまでの間は、ファイル同士が食い違いうる。書き込みを止めたセッションでは、再起動まで回答 AI に渡る状態がファイルと食い違いうる | 必要ならデータベース（トランザクション）への移行を別 MVP で検討する |
| 判断が正しいかの確認は、各ケース2〜3回・言い回し1種類だけ | 自動で確認を始める MVP の前に、言い回しを増やした判断の実験を行う |
| 確認ログは追記だけで大きくなる。起動時に全件を読む | 件数が問題になったら別 MVP |
