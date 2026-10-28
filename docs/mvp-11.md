# MVP11: 確定した記憶変更を取り消す

状態: **実装済み・未固定**（commit / tag 前。レビュー後に固定するかを決める）。基準は MVP10（コミット `98e9feb`、タグ `article-12`）

## 1. 目的と対象範囲

MVP10 では、`/confirm` で Pending を確認し、共存・置換を確定できるようにした。しかし、誤って確定した変更を元に戻す手段が無かった。特に共存は y/N なしで実行されるので、AI が置換を共存と誤ると、値が追加されたままになる。

MVP11 では、**MVP10 の確認による確定1件を、ユーザーの明示的な操作で安全に取り消す。**

- 取り消しは、ユーザーがコマンド（`/revert c001`）で始め、変更内容を表示して `y` を入力したときだけ実行する。**Claude は呼ばない**
- 会話原文・変更履歴・確認ログは削除も書き換えもしない。取り消した事実を新しい記録として残す
- 取り消すのは「確定」であって、ユーザーの発言ではない。元の発言は、もう一度確認できる Pending に戻す

### 対象

| 確認で行った変更 | 取り消し |
|---|---|
| replace（`home_location: 川崎 → 名古屋`） | 確認の前の値と根拠の発言に戻す |
| add（共存で `home_location_2 = 名古屋` を追加） | 追加した key を現在値から外す |
| 現在値を変えなかった共存（同じ値が既にあった。`operation: None`） | **対象外。** 理由を表示する |

`operation: None` を対象外にしたのは、Current に変更が無いため、Current の書き込みを確定点とする復旧方式をそのまま使えないからである。対応するなら、Pending だけを変える操作に合った復旧方法を別に設計する。

### 今回やらないこと

自然文からの自動訂正、発言の撤回の自動判断、複数の変更をまとめた取り消し、過去の任意の時点へのロールバック、記憶全体の再構築、MVP9 の自動 add / replace の取り消し、取り消しの取り消し（代わりに、戻した Pending を `/confirm` で確認し直す）。

---

## 2. 現行実装（MVP10）の確認結果

設計調査で確かめた事実（詳細は設計調査の報告）。

- 置換の前の値は、History の `old_value`（値だけ）、確認ログの `commit.plan.before`（値と updated_at）、確認ログの `prepared.current`（evidence を含む項目全体。旧形式では `presented.current`）に残る。**evidence まで戻せるのは確認ログだけ**
- MVP10 の確定は `confirmation_id` で一意に指定できる（1つの確認につき History のイベントは最大1件）。MVP9 の自動 add / replace には ID が無い
- 長期状態の Current には、key を外す遷移が無かった
- 取り消しのイベントに元の `confirmation_id` を持たせると、回答 AI へ渡すときに元の確認の質問・回答が付いてしまう（`_with_confirmation_texts`）。`complete_plan` / `reconcile` の「History に同じ `confirmation_id` があれば反映済み」という判定とも混ざる
- 確認ログは未知のイベント名・ID 形式を拒否し、`progress()` は終端イベントが1つでもあれば closed とするので、確認ログの中に取り消しを記録すると復旧の対象にならない
- Pending の status は unresolved / resolved だけだった
- `next_numbered_key` は Current しか見ないので、取り消しで外した `_2` が次の共存で再利用される
- 本番の `data/` には長期状態・保留・確認ログのファイルがまだ無い（実データの移行は発生しない）

---

## 3. 確定した設計

### コマンド

| コマンド | 動作 |
|---|---|
| `/revert` | 記憶を変更した（applied まで進んだ）確認の一覧。取り消せないものは理由を表示する |
| `/revert c001` | 確認 c001 の取り消しの内容を表示し、y/N を待つ。`y`（`Y`）以外の入力（`N`、`yes`、`はい`、コマンドを含む）は、すべて「実行しない」 |

- 対象は確認の ID（`c001`）で指定する。取り消すのは「確定操作」だからである。`/pending p001` で、その Pending を解決した確認と、取り消しの記録を確認できる
- `/undo` は「直前の操作」を暗に対象にするので採らなかった（再起動を挟むと何が直前か曖昧になる）。`/cancel` は「進行中の確認をやめる」意味で既に使っている
- 入力の振り分けは MVP10 と同じ。取り消しの y/N 待ちの間はすべての入力を y/N への回答として扱う。確認の回答待ちの間に `/revert` が来たら、確認を取り消してから取り消しを始める

### 置換の取り消し

- `target_key` を確認ログの `prepared.current` の項目に戻す（値と evidence）。`plan.before` と一致しなければ取り消さない
- **`updated_at` は取り消した時刻にする。** 元の時刻（T0）に戻すと、確定の前に 川崎@T0 を相手にして作られた古い Pending が S4（Pending 作成後の更新の検出）を再び通ってしまう。S4 が防いでいる「変わって戻った」状態そのものだからである

### 共存の取り消し

- 確認で追加した key（`home_location_2`）を Current から外す。ほかの項目には触れない
- 外した key は再利用しない。`next_numbered_key` は History に現れる key も使用済みとする（MVP10 の共存にも適用される）

### Pending: 案 C

| 案 | 内容 | 判断 |
|---|---|---|
| A | 元の Pending は resolved のまま | 発言が Current にも未解決の Pending にも無くなり、実質的に撤回と同じになる |
| B | 元の Pending を unresolved に戻す（同じ ID） | resolved → unresolved の逆戻り、解決の記録の上書き、`target_updated_at` の書き換えが必要になる |
| **C（採用）** | 元の Pending を `reverted` にし、同じ発言の新しい Pending を作る | ID を再利用せず、過去の記録を書き換えない。S1〜S6 がそのまま効く。やり直しは `/confirm` |
| D | reverted にし、新しい Pending は作らない | 確定の取り消しと発言の撤回を混同する |

元の Pending（p001）:

- `status: reverted`、`reverted_by: r001`、`reverted_at`（取り消した時刻）
- `resolution` / `resolved_key` / `resolved_at` / `confirmation_id` は解決の記録としてそのまま残す

新しい Pending（p002。ID は全項目の最大の番号の次）:

- `reopened_from: p001`、`reopened_by: r001`、`status: unresolved`、`created_at` は取り消した時刻
- `proposed_value` / `evidence` / `decision` / `confirmation_question` などは元の Pending と同じ
- 比べる相手（`target_*`）は、置換の取り消しでは戻した項目（`target_value: 川崎`、`target_updated_at`: 取り消した時刻）。共存の取り消しでは既存の項目を変えていないので、元の Pending のものをそのまま使う
- 今の状態ですぐ確認できない場合（共存の取り消しで、既存の項目が確定の後に変わっていたなど）は、承認の前にそう表示する

Pending の保存形式に4つのキー（`reverted_by` / `reverted_at` / `reopened_from` / `reopened_by`、値は文字列か null）と status `reverted` を追加した。MVP10 までのファイルはそのまま読める。

### History: `revert` イベント

```json
{"timestamp": "2026-10-18T09:00:00+09:00", "operation": "revert", "subject": "user", "key": "home_location",
 "old_value": "名古屋", "value": "川崎",
 "revert_id": "r001", "reverted_confirmation_id": "c001", "reverted_pending_id": "p001",
 "reverted_operation": "replace", "reopened_pending_id": "p002"}
```

- **operation は専用の `revert`。** ユーザーが述べた変更（add / replace）と混同させない
- `old_value` は取り消しで外した値、`value` は取り消しで戻した値（共存の取り消しでは無い）。「後のイベントの value が新しい状態」という読み方がそのまま成り立つ
- **`confirmation_id` のキーは持たせない。** 元の確認は `reverted_confirmation_id` で指す
- evidence は持たせない。取り消しの根拠はユーザーの発言ではなく、コマンドと承認で、取り消しログにある
- 回答 AI へ渡すときは、取り消しログから承認の入力を `revert_approval_input` として付ける。元の確認の質問・回答は付かない

### 取り消しログ

`data/long_term_memory_reverts.jsonl`（環境変数 `ACM_LONG_TERM_MEMORY_REVERTS_FILE`）。1行1イベントの追記だけ。取り消しごとに `r001` からの連番の `revert_id` を付ける。すべてのイベントが `event` / `revert_id` / `target_confirmation_id` / `timestamp` を持つ。確認ログとは別のファイルにした（MVP10 の `load_confirmations` / `progress` / 確認回数の数え方を変えずに済むため）。

| イベント | 内容 |
|---|---|
| prepared | 表示する変更内容を決めた（表示する前に書く）。`plan`（その時点の計画）、`prompt`（表示する文面）。**表示したことは意味しない** |
| presented | 文面の出力処理が正常に終わった（出力した後に書く）。`prompt` |
| approval | `prompt`、`input`（入力の原文）、`approved` |
| commit | `plan`（これから実行する計画。承認した時点の状態で作り直したもの） |
| applied / failed | 実行の結果。`recovered: true` は途中の失敗の後・起動時に片付けたもの |
| cancelled / expired | 取り消し・再起動を挟んだ理由 |

| 結果 | イベントの並び |
|---|---|
| 取り消した | prepared → presented → approval（approved: true）→ commit → applied |
| y 以外 | prepared → presented → approval（approved: false） |
| 表示した後に状態が変わっていた | prepared → presented → approval（approved: true）→ failed |
| 文面の出力に失敗 | prepared → cancelled |
| presented を書けなかった | prepared だけ（次の起動時に expired） |
| 文面・承認の入力を会話原文に保存できなかった | prepared → presented → cancelled（y でも実行しない） |
| 承認の前に終了・プロセスが止まった | prepared → presented → cancelled / expired |
| 実行の途中で失敗 | … → commit → applied（続きを実行できた）／ failed（Current を変える前に止まった） |

会話原文には、表示した文面（assistant、`revert_event: presented`）と承認の入力（user、`revert_event: approval`）を、`revert_id` つきで保存する。

---

## 4. 取り消せる条件（Python）

`/revert c001` を受け付けるときと、`y` の後（承認を書く前）に、今の状態から計画を作り直して確かめる。**作り直した計画の文面が、表示した文面と違えば実行しない。**

| # | 条件 | 防ぐもの |
|---|---|---|
| R1 | 確認が commit と applied まで進み、現在値を変更している（add / replace） | 何も変えていない確認・未完了の確認の取り消し、`operation: None` |
| R2 | まだ取り消していない。取り消しログに applied が無く、変更履歴に `reverted_confirmation_id` が無く、Pending が reverted でない | 二重の取り消し。取り消しの記録どうしの食い違い |
| R3 | Current の対象項目が、確認の実行後（値と updated_at）のまま | 確定後の変更を古い値で上書きすること。**値が変わって戻っただけの状態**を変更なしと誤認すること |
| R4 | 変更履歴に確認のイベントがちょうど1件あり、確認ログの計画と一致する。その後に同じ subject + key のイベントが無い | 確認ログと History が食い違ったまま続けること。後続の変更（R3 の二重の確認） |
| R5 | 確認が解決した Pending が resolved で、その確認による解決になっている | Pending と確認ログの食い違い |
| R6 | 置換なら、確認の前の項目を確認ログから復元でき、戻しても同じ属性の別の key に同じ値が無い | evidence の欠落、同じ属性での値の重複 |
| R7 | ファイル上の Current・Pending が、このセッションの持っている内容と同じ | セッションの外での変更の上書き |
| R8 | 書き込み停止中でない。ほかの確認・取り消しの途中でない | 途中の状態での実行 |

取り消すと確認できなくなる未解決の Pending（確定後の値を相手にして作られたものなど）は、拒否はせず、**承認の前に ID と理由を表示する**。拒否にすると、その Pending を解決するしか道が無く、解決すれば Current が変わって取り消せなくなるためである。

---

## 5. 障害時の一貫性

書き込みの順序は **取り消しログ（commit）→ Current → History → Pending → 取り消しログ（applied）**。Current の書き込みが確定点で、Current が書けていれば残りを完了させ、書けていなければ実行しなかったことにする（MVP10 の X1〜X8 と同じ方式）。

| # | 仕組み |
|---|---|
| V1 | 状態を変えるのは commit を書けた後だけ |
| V2 | Current・Pending は一時ファイルへの書き出しと `os.replace`。Pending の2つの変更（p001 の reverted と p002 の追加）は1回の置き換えで書く |
| V3 | 実行は冪等（`complete_revert`）。History に同じ `revert_id` があれば追記しない。Pending は、反映済みの部分（`reverted_by` / `reopened_by` が同じ r001）を飛ばす |
| V4 | **Current は対象の key だけでなく全体で比べる。** 計画に、実行前と実行後の Current 全体のハッシュ（`state_before` / `state_after`）を持たせる |
| V5 | Pending が計画と食い違う（新しい Pending の ID が別の Pending に使われているなど）場合は、何も書く前に止める |
| V6 | 新しい Pending の ID は計画の時点で決め、commit に入れる。復旧しても同じ ID になる |
| V7 | 実行の途中で失敗したら、その場でファイルを読み直して1回だけ片付ける。片付けられなければ、そのセッションでは長期状態の書き込みを止め、再起動を促す |
| V8 | 起動時、確認の片付けの後に、commit があって applied / failed の無い取り消しを片付ける。承認の前に終わった取り消しには expired を書く（**未確定の承認から処理を再開しない**） |

| 起動時の Current 全体 | 判断 | やること |
|---|---|---|
| 計画の実行後と同じ | Current までは書けた | History に無ければ追記し、Pending を反映し、applied（recovered）を書く |
| 計画の実行前と同じで、History・Pending にも取り消しが無い | Current を書く前に止まった | failed を書く。もう一度 `/revert` できる |
| 計画の実行前と同じなのに、History か Pending には取り消しがある | 食い違い | **起動を止める** |
| どちらでもない | 外部での編集・破損 | **起動を止める** |

**共存の取り消しで「key が無い」状態とデータ破損を混同しない:** 実行後の Current は「`home_location_2` が無い」状態である。key が無いことだけで判断すると、ファイルが壊れた・外で編集された場合も「取り消し済み」と誤認しうる。V4 により、key が無く、かつほかの項目も計画どおりの場合だけを実行後とみなす（テスト `test_missing_added_key_with_other_damage_is_not_taken_as_reverted`）。ただし、commit の後で、外部の編集がちょうど対象の key だけを消した場合は、取り消しの書き込みと区別できない（結果の状態は同じになる）。

### 保証できる状態と保証できない状態

| | 内容 |
|---|---|
| 保証できる | 取り消しログに commit の無い取り消しが、Current・History・Pending に入ることは無い |
| 保証できる | 会話を始める時点で、取り消しについて Current・History・Pending・取り消しログが一致している。一致させられなければ起動しない |
| 保証できる | 同じ確認を二重に取り消さない。復旧で History・Pending を二重に更新しない |
| 保証できる | 会話原文・変更履歴・確認ログの既存の内容を変えない（追記だけ） |
| 保証できる | 保存に失敗した取り消しを完了したことにしない（Pending を書けなければ applied を書かず、書き込みを止める） |
| 保証できない | プロセスが止まってから次に起動するまでの間の、ファイル同士の食い違い |
| 保証できない | `fsync` の後のディスク・OS の障害、複数のプロセスによる同じ `data/` の同時使用 |
| 保証できない | 外部での手作業の編集の整合（検出できれば起動を止めるが、すべては検出できない） |

---

## 6. 回答 AI への渡し方

- 現在値は戻した値（置換の取り消し）・外した後の値（共存の取り消し）になる
- 新しい Pending（p002）は通常の未解決の Pending として渡し、`reopened_from` を付ける。reverted の Pending は渡さない
- History の revert イベントには、取り消しの承認の入力だけを付ける。元の確認のイベントには従来どおり質問・回答が付くが、revert イベントには付かない
- システムプロンプトに次を追加した: revert の意味、old_value / value の意味、**取り消された確認の質問・回答は取り消された変更の根拠であって、取り消しの根拠ではない**、**取り消しの理由は記録されていないので推測しない**、取り消しは発言の撤回ではなく発言は Pending に戻っている、`reopened_from` の意味。検索計画のプロンプトにも revert を追加した

---

## 7. 変更したファイル

| ファイル | 変更 |
|---|---|
| `src/ai_context_memory/revert.py`（新規） | 取り消しログの読み込み・追記・ID 採番、取り消しの進み具合、取り消せるかの判定（R1〜R6）と計画（`plan_revert`）、y/N の文面、History のイベント、冪等な実行（`complete_revert`）、復旧（`reconcile_revert`）、一覧（`revert_candidates`） |
| `src/ai_context_memory/structured_memory.py` | Pending の status `reverted` と4つのキー、`next_numbered_key` が History の key も使用済みとする |
| `src/ai_context_memory/confirmation.py` | `plan_resolution` に History を渡せるようにした。reverted の Pending は確認できない（S1 の理由）。`complete_plan` は unresolved 以外の Pending を解決しない |
| `src/ai_context_memory/chat.py` | `memory_reverts_path`、取り消しの状態（準備済み・y/N 待ち）、`start_revert` / `mark_revert_presented` / `abandon_prepared_revert` / `cancel_revert` / `approve_revert` / `revert_overview` / `pending_reverts`、起動時の復旧、History への取り消しの承認の付加、会話原文への追記の共通化 |
| `src/ai_context_memory/cli.py` | `/revert`・`/revert <id>`、入力の振り分け（取り消しの y/N 待ちを最初に）、表示、`/pending` の表示、取り消しログのパスと環境変数、起動時の件数の表示 |
| `src/ai_context_memory/llm.py` | 回答 AI・検索計画のプロンプト（revert、reopened_from）、History と Pending のブロックの項目 |
| `tests/test_memory_revert.py`（新規） | MVP11 のテスト |
| `tests/test_memory_pending.py` / `test_structured_memory.py` / `test_cli.py` / `test_memory_confirmation.py` | 下記の意図的な書き換え |
| `README.md` / `docs/mvp-10.md` | MVP11 の説明。MVP10 の状態を「固定済み」に訂正 |

### 意図的に変えた既存のテスト

| テスト | 変更 |
|---|---|
| `test_memory_pending.py` の `NAGOYA_PENDING` | Pending に4つのキー（null）が加わった |
| `main()` を呼ぶテストの環境変数（4か所） | 取り消しログのパスを追加した（テストが本番の `data/` を読まないように） |

---

## 8. テストの結果

### pytest（Claude をモック。保存先は pytest の一時ディレクトリ）

**663 passed, 2 xfailed**（MVP10 の 585 passed, 2 xfailed に対し、新規 78 件。うち 13 件はコミット前の R7・R8 の確認で追加。xfail は MVP9 の既知の限界のまま）。既存のテストは、7章の意図的な書き換え以外は変えずに通っている。

`tests/test_memory_revert.py` で確かめていること:

| 観点 | 主なテスト |
|---|---|
| 置換の取り消し | 川崎と元の evidence に戻り、updated_at は取り消した時刻。History は追記だけで、revert イベントは `confirmation_id` のキーを持たない。p001 は reverted で解決の記録が残る。p002 は同じ発言で `reopened_from: p001`。取り消しログは prepared → presented → approval → commit → applied |
| 共存の取り消し | `home_location_2` だけを外す。新しい Pending の比べる相手は元のまま |
| 再確認 | 取り消した後に `/confirm p002` で確認し直せ、その確定（c002）もまた取り消せる |
| y 以外 | `N`・`n`・`yes`・`はい`・コマンド・自然文では実行しない。終了したら cancelled |
| 二重の取り消し | 同じ確認は2回目を拒否し、何も変えない。`complete_revert` を2回呼んでも1回分 |
| 後続の変更 | 同じ key の後の変更、変わって戻った場合（History あり／更新時刻だけ）、追加した key が無い場合を拒否する |
| 食い違い | History に確認のイベントが無い、取り消しログに無い revert が History にある、History が読めない場合を拒否する |
| 重複 | 戻すと同じ属性の別の key と値が重なる場合を拒否する |
| 対象外 | `operation: None` の確認は、一覧と `/revert c001` の両方で理由を表示する。何も変えなかった確認、存在しない ID、形式の違う ID は始めない |
| 確認できなくなる Pending | 承認の前に ID と理由を表示し、取り消した後も残る |
| 番号付きの key | 取り消した `_2` は再利用せず、次の共存は `_3` |
| 承認の記録 | 文面の出力中は prepared だけで y/N を待たない。出力に失敗したら cancelled で、次の y は通常の会話。presented を書けない・文面や y を会話原文に保存できない場合は実行しない。表示した後に状態が変わったら failed。承認の前・y を保存した直後に止まったら、再起動後は expired で再開しない |
| R7 ファイルとセッションの一致 | 同じセッションのまま、`/revert` の前・y/N 待ちの間に、外で Current（対象と関係のない項目を含む）・Pending・History（同じ key の後の変更だけ）が変わったら、開始しない／実行しない（approval の後に failed）。ファイルが一致していても、作り直した計画の文面が表示した内容と違えば実行しない。実行するのは承認した時点で作り直した計画 |
| R8 排他制御 | 確認の回答待ち・置換の y/N 待ちの間は取り消しを始められない（y/N 待ちの `/revert` は「実行しない」への入力になる）。取り消しの y/N 待ちの間は確認も別の取り消しも始められない。準備しただけで出力していない操作は、次の操作を始めると閉じる。同じセッションで取り消し・拒否・再確認・再取り消しを重ねても、ファイルとセッションが一致し、二重の取り消しは始まらない |
| 実行中の保存の失敗 | Current の失敗 → 何も変えず failed。History の失敗 → その場で続きを実行して applied。Pending が失敗し続ける → 完了にせず書き込みを止め、再起動時に完了させる |
| 再起動時の復旧 | commit だけ → failed（もう一度取り消せる）。Current まで／History まで／Pending まで（置換・共存の両方）→ 1回だけ完了させ、もう一度起動しても何もしない。どちらでもない・共存で key が無いうえにほかも壊れている・Current は実行前なのに History は取り消し済み・新しい Pending の ID が別の Pending に使われている・取り消しログの最後の行が途切れている → 起動を止める |
| 回答 AI | revert イベントに元の確認の質問・回答が付かず、承認の入力が付く。p002 は `reopened_from` つきで渡り、p001 は渡らない。プロンプトの説明 |
| Claude の呼び出し | 取り消しの流れ（一覧・表示・y/N・実行）で Claude を1回も呼ばない |
| 記録を消さない | 会話原文・History・確認ログは、取り消しの前の内容がそのまま先頭に残る（追記だけ） |
| 互換性 | MVP10 の Pending ファイル（新しいキーが無い）と、取り消しログが無い状態で起動し、取り消せる |

### 実際の Claude によるスモークテスト

実際の `claude`（Claude Code の既定モデル）と、一時ディレクトリのデータで実行した。本番の `data/` は使っていない。初期状態は MVP10 のスモークテストと同じ（Current `home_location = 川崎`、Pending p001「今は名古屋に住んでいます。」、質問「川崎の住まいも残っていますか？」）。プロンプトは実行前に1回だけ書き、結果を見て調整していない。

| # | 手順 | 結果 |
|---|---|---|
| R1・R2 | `/confirm p001` →「いいえ、もうありません。」→ y → `/revert` → `/revert c001` → y | 2/2 で判断は replace、川崎 → 名古屋 → 川崎。History は replace と revert の2行、p001 は reverted、p002 は unresolved（`reopened_from: p001`）、取り消しログは prepared → presented → approval → commit → applied |
| C1 | `/confirm p001` →「はい、残っています。」→ `/revert c001` → y | 判断は coexist、`home_location_2` を追加してから外した。History は add と revert |

取り消しの後、質問ごとに再起動して尋ねた結果（各1回。合計8回）:

| 質問 | R1 | R2 | C1 |
|---|---|---|---|
| 今の住まいは？ | 川崎と名古屋の関係は未確認と説明 | 同左 | 同左 |
| 前はどこに住んでいた？ | 経緯として、確認で置き換えた（質問・回答を根拠に）→ 取り消された →「取り消しの理由は記録されていません」 | 同左 | — |
| なぜ記憶が変わった？ | 1回目（確認と回答で置き換え）と2回目（コマンドで取り消し、y で承認）を分けて説明し、「取り消しの理由は記録されていません」。発言は撤回されておらず p002 に戻っていると説明 | 同左。さらに「上の確認の質問と回答は1回目の変更の根拠で、取り消しの理由ではありません」と明示 | 「取り消しの理由は記録されていないため、なぜ取り消したのかはお答えできません」 |

- **取り消しを、元の確認の質問・回答で正当化した回答は 0/5**（取り消しに触れた5回すべて）。取り消しの理由を推測した回答も0回
- 取り消しを発言の撤回と説明した回答は0回。3回で「発言は撤回されておらず保留に戻っている」と説明した
- 「今の住まいは？」の3回すべてで「川崎と名古屋のどちらか一方には断定できない」という二者択一の説明になった。未確認なのは「川崎が今も有効か」であり、MVP9 から残っている問題がそのまま出ている（取り消しの後は確認前の状態に戻るので、MVP10 のスモークテスト L と同じ状態）
- C1 の「なぜ」では、History の検索語（replace / revert）に add のイベントがヒットせず、revert のイベントだけが渡った。回答は revert のイベントから「確認で追加された値を外した」と正しく説明し、渡っていない現在値については「手元に情報がない」とした
- 取り消しと関係のない警告が出た: 長期状態の抽出の前置きの取り出し（3回）、`claude` の応答が空（1回）、長期記憶の抽出の JSON の失敗（1回）。いずれも MVP9・MVP10 からの既知の挙動で、取り消しの状態遷移には影響していない
- **これは各ケース1〜2回の結果で、回答が常にこうなることを示すものではない**

---

## 9. 残る既知の制約

| 制約 | 内容・次の手段 |
|---|---|
| `operation: None` の確認は取り消せない | Pending だけを変える操作の復旧方法を別に設計する |
| MVP9 の自動 add / replace は取り消せない | History のイベントに ID が無い。ID を付けるかは別 MVP |
| 取り消しの取り消しは無い | 戻した Pending を `/confirm` で確認し直す |
| 時刻の精度が秒 | 実際の実行では、確定と取り消しの updated_at が同じ秒になった（値が違うので計画の実行前・実行後は区別できる）。同じ秒の中で変わって戻った場合、updated_at では見分けられないが、History の後続イベント（R4）と Current 全体のハッシュで検出する。History に残らない経路で同じ秒の中に変わって戻った場合は検出できない |
| R7 で比べないもの | 確認ログ・取り消しログは、セッションが読み込んだ内容を使い、実行の直前にファイルと比べない（このプロセスだけが追記する前提）。最後の確認（y の後の計画の作り直し）から Current を書くまでの間（approval と commit の追記）は確かめ直さない。どちらも、複数のプロセスが同じ `data/` を使う場合にだけ問題になり、MVP10 と同じく保証しない |
| 外部の編集が対象の key だけを消した場合 | commit の後に外部で `home_location_2` だけが消されると、取り消しの書き込みと区別できない（結果の状態は同じ） |
| 確認できなくなる Pending | 警告するだけで、その Pending は確認できないまま残る（MVP10 の Pending と同じく、破棄の手段は無い） |
| 取り消しの理由 | 記録しない。回答 AI には「記録されていない」と答えるよう指示している |
| 回答 AI の二者択一 | 取り消しの後の「今の住まいは？」で、MVP9 からの二者択一の説明が出る。プロンプトの見直しは別に判断する |
| 取り消しログ・確認ログは追記だけ | 起動時に全件を読む。件数が問題になったら別 MVP |
| ファイル同士の一貫性 | プロセスが止まってから次に起動するまでの食い違い、`fsync` の後の障害、複数プロセスの同時使用は MVP10 と同じく保証しない |
