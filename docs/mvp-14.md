# MVP14: 属性一覧による検索改善と検索状態の明示

状態: **実装済み・コミット済み・未固定**（tag 前）。基準は MVP13（コミット `aca71b3`、タグ `article-15`）

## 1. 目的と対象範囲

MVP13 では、検索で見つかった属性を起点に、その属性の現在値（Current）・未解決の保留（Pending）・変更履歴（History）を Python が漏れなく集めるようにした。残った問題は次の3つだった（MVP14 の調査）。

| 問題 | 調査で分かったこと |
|---|---|
| 属性の起点が見つからない | 検索プランナーは保存済みの属性名を知らない。「住まい」「勤務先」のような言葉は key にも値にも発言にも現れず、見つかるかは、プランナーが元の発言と同じ言葉（「住ん」「働」）を偶然選べるかで決まる。key 名も2つの AI が別々に付ける（テストは `home_location`、実際の抽出 AI は `residence`） |
| 検索の状態が回答 AI に伝わらない | 「0件」「属性を特定できない」「プランナーの失敗」「検索不要の判断」「何も保存されていない」の5つで、回答 AI への入力が完全に同じだった。変更履歴の読み込み失敗と「履歴は不要」の判断も同じ入力 |
| 履歴が必要かはプランナー次第 | 「名古屋の件はどうなった？」で、プランナーが変更履歴を不要と判断することがあった（調査と MVP13 の評価で 6回中3回） |

MVP14 では次の3つを入れた。

1. 保存済みの属性の識別子（`subject.key`）の一覧を検索プランナーへ渡し、`attributes` で選ばせる。Python は一覧と完全一致で照らし合わせる
2. 範囲ごとの検索の状態を `[retrieval status]` として回答 AI へ渡す
3. R4: 検索語が Current には一致せず、Pending か History にだけ一致した属性は、プランナーの判断によらず変更履歴を渡す

### 今回やらないこと

数千属性向けの段階検索、ベクトル検索、属性の日本語別名の保存、`residence` と `home_location` の統合、既存データの移行、確認・取り消しの処理の変更、検索プランの JSON の回復（下記）。

検索プランの JSON の回復は見送った。調査で見つかった失敗は「JSON・説明文・JSON」の形で、既存の `_recover_json` は JSON の候補が複数あると失敗にする（どれも選ばない）設計のため、そのまま再利用してもこの失敗は直らない。直すには `_recover_json` の規則を変えることになり、小さな変更ではない。失敗したときは `search: failed` として回答 AI に伝わる。

## 2. 属性一覧と完全一致の検証

### 一覧の作り方（`structured_memory.attribute_catalog`）

- 対象は Current の項目と、未解決の Pending の `target_key`・`proposed_key`（解決済み・取り消し済みの保留は含めない）
- 番号付きの key は既存の `attribute_base` の規則で番号の無い key にまとめる（`hobby_2` → `hobby`）
- subject を区別する（`partner.hobby` と `user.hobby` は別）。重複を除き、subject・key の順に並べる（決定的な順序）
- 渡すのは識別子だけで、値・evidence は渡さない

```text
[stored attributes]
- user.employer
- user.home_location
[/stored attributes]

[question]
住まいについて教えて
```

### プランナーの出力

検索プランに `attributes`（文字列の配列、最大5個）を足した。`queries` は、これまでどおり値や発言の言葉（地名、会社名など）を探すために使う。

### 完全一致の検証（`structured_memory.resolve_attribute`）

| プランナーが返した識別子 | 扱い |
|---|---|
| `user.home_location` | 一覧にその組があれば使う |
| `home_location`（subject なし） | その key を持つ属性が一覧にちょうど1つなら使う。複数の subject にあれば `ambiguous` で使わない |
| `user.home`、`home` | 部分一致しないので使わない（`not_in_list`） |
| `user.home_location_2` | 番号付きの key を読み替えないので使わない |
| `user.address`（一覧に無い） | 使わない。新しい属性を作ったり、似た属性へ読み替えたりしない |
| 文字列でない要素、配列でない値 | 使わない（検索プラン全体は失敗にしない。検索語は使う） |

使わなかった選択は `Retrieval.rejected_attributes` / `ignored_attributes` に残し、CLI に `[属性選択] user.address は使いません（一覧にありません）` と表示する。回答 AI へは「特定できた属性」だけを伝える。

一致した属性は、MVP13 の属性単位の展開（Current・Pending・History）の起点に加える。属性を選べたターンは、検索語が0件でも再検索しない。

### 上限で落とす順（属性5つ、変更履歴3つ）

1. 選ばれ、検索語にも一致した属性（一致の順位順）
2. 検索語で Current・Pending に一致した属性
3. R4 の属性
4. 選ばれただけの属性（選ばれた順）
5. History だけに一致した属性

「検索語に一致した」は件数の上限（`MAX_RESULTS`）の前で判定する（`search.matching_records`）。上限で検索結果から外れた Current でも、一致は一致として扱う。

この順は、評価と最終確認で見つかった2つの不具合への対策（8 の「最初のプロンプトでの失敗と修正」）。プランナーは一覧の順に、関係の薄い属性まで選ぶことがあり、選ばれた属性を単に先に置くと、変更履歴の上限で最近変わった属性が落ちたり、上限いっぱいの5つが選ばれたときに、検索語が一致した属性の Current と History が落ちたりした。

**限界**: 一覧にある無関係な属性を AI が選ぶこと（過剰な選択）は、Python では防げない。Python が防ぐのは一覧に無い識別子だけで、並びは「検索語に一致した属性を上限で落とさない」ための緩和にすぎない。検索語にどれも一致しないまま無関係な属性が選ばれたときは、その現在値は回答 AI に渡る。選びすぎを減らすのはプランナーへの指示（2 の下）で、その効果は指示の文面に強く左右された。

### `needs_memory=false` と属性の選択

`needs_memory` が false で、長期記憶の変更履歴も不要という判断と一緒に `attributes` が返されたときは、選択を使わない（記憶を渡さないほうを選ぶ）。`needs_long_term_memory_history` が true なら使う（MVP13 と同じく、変更履歴で見つかった属性の Current と Pending も渡す）。

## 3. 属性一覧の上限とフォールバック

上限は `MAX_CATALOG_ATTRIBUTES = 100`。調査では、100属性の一覧で検索プランの入力が約1,000トークン増え（約3,040 → 約4,080）、1,000属性では約12,660トークンになった。100属性の一覧（約2,100文字）は、抽出 AI が毎ターン受け取っている値つきの現在値（100属性で約3,000文字）より小さい。

上限を超えたら一覧を渡さず、MVP13 までの検索語だけの方式で探す。

- プランナーには `(not listed: 120 attributes, more than the limit of 100)` とだけ書き、key を推測した英語の snake_case も検索語に入れるよう指示する
- `Retrieval.attribute_list`（`total` / `provided` / `limit` / `chars`）に、一覧を渡したか、何件か、ブロックの文字数を残す（入力サイズの測定用）
- 回答 AI へは `attribute list: not_provided (120 attributes)` と伝え、見落としの可能性があることを指示で説明する
- CLI には `[属性一覧] 120件が上限 100 を超えるため渡していません（検索語だけで探しました）` と表示する
- 一覧を渡さなかったときも、プランナーが返した識別子は実際の一覧と完全一致すれば使う

## 4. 検索の状態（`[retrieval status]`）

回答 AI への入力の、作業記憶の直後（検索で取得した記憶の前）に置く。

```text
[retrieval status]
search: done
attribute list: provided (5 attributes)
attributes identified: user.home_location
long-term memory state: found 1
long-term memory pending: found 1
long-term memory history: unavailable
retrieved memories: none_stored
working memory history: none_stored
[/retrieval status]
```

| 行 | 値 |
|---|---|
| `search` | `done`（検索した）、`not_needed`（プランナーが不要と判断）、`failed`（検索プランを得られなかった）、`retry_failed`（1回目が0件で、再検索の計画を得られなかった）、`skipped`（検索できる記憶が無く、プランナーを呼ばなかった） |
| `attribute list` | `provided (N attributes)` / `not_provided (N attributes)`。属性が1つも無ければ行ごと省く |
| `attributes identified` | 実際に Current・Pending・History を渡す属性。特定できなければ `none`。長期記憶を検索しなかったターンは行ごと省く |
| 範囲ごとの行 | `found N`、`no_match`、`not_searched`、`unavailable`、`none_stored`。上限で省いたものは `found 5 (2 omitted)`（Pending・従来の記憶・作業記憶の変更履歴）、`found 5 (1 more matching attributes omitted)`（Current）、`found 3 attributes (4 more matching attributes not included)` / `not_included (N matching attributes)`（History） |

範囲ごとの判定の順序:

1. その範囲を読み込めなかった → `unavailable`（検索したかどうかによらない）
2. 読み込めて、本当に空 → `none_stored`（Current・Pending・従来の記憶は起動時に読み込み済み。Pending は未解決の保留が無いこと）
3. 検索プランを得られなかった（`search: failed`） → `unavailable`
4. その範囲を検索しなかった → `not_searched`
5. 渡したものがある → `found`、無い → `no_match`

- 「0件」を「存在しない」に変換しない。`none_stored` は空だと確かめた範囲だけ
- 範囲ごとに独立させた。History だけ読めなかったとき、Current と Pending は `found` のまま伝わる
- 状態名・件数・属性の識別子だけを渡す。ファイルパス・例外のメッセージ・値は渡さない（テストで確認）
- History の行は、MVP13 の見出し（`events: N of M, complete` / `older events omitted` / `other matching attributes not included`）と同じ数え方。詳しい完全性は見出しで伝える
- 保存先が設定されていない範囲は行を省く

回答 AI への指示（`SYSTEM_PROMPT` の末尾に追加）: 状態の意味、範囲ごとに独立していること、`no_match` と `not_searched` は記録が無いことを意味しないこと、`unavailable` は「今回は確認できなかった」と伝えること、`none_stored` の範囲だけは「保存されていない」と伝えてよいこと、状態は質問に関係するときだけ触れ、ブロックに無い原因を推測しないこと。

### CLI

- 変更履歴（長期・作業記憶）を読み込めなかったときは、`[長期履歴検索結果] 0件` ではなく `[長期履歴検索結果] 読み込めませんでした（0件ではありません）` と表示する
- 状態に、検索の表示だけでは分からないこと（失敗、`unavailable`、省略、一覧を使わなかった）があるターンだけ、`[検索状態] 検索: done / 現在値: found 1 / 保留: found 1 / 長期履歴: unavailable / ...` を1行表示する（回答 AI へは毎ターン渡す）

## 5. R4: 変更履歴の自動取得

**条件**: 長期記憶の検索語（`queries`）のうち、Current には一致せず、未解決の Pending か History に一致した語がある属性は、プランナーが変更履歴を不要と判断していても、その属性の変更履歴を渡す。

- 判定は検索語ごと（`ChatSession._non_current_attributes`）。属性一覧からの選択や、別の検索語で Current にも一致したことでは無効にならない（「名古屋」が保留にだけ一致すれば、home_location を一覧から選んでいても R4 が働く）
- 判定には MVP13 の属性展開の前の、検索語との一致だけを使う。件数の上限で Current の一致を取りこぼさないよう、上限を付けずに一致を調べる（`search.matching_records`。Python の文字列照合だけで、AI 呼び出しは増えない）
- プランナーが変更履歴を必要と判断したターンは、MVP13 と同じくすべての属性が対象（R4 は何も変えない）。不要と判断したターンは、R4 の属性だけが対象
- 件数の上限と省略の表し方は MVP13 のまま（`_expand_history` に対象の属性を絞る引数を足しただけ）
- 検索語が History にだけ一致した（置き換え前の値「A社」など）ときも、その属性を見つけたものとして扱い、検索語を変えた再検索（2回目のプランナー呼び出し）をしない
- History を読み込めなかったときは、Pending の一致だけで判定し、History は `unavailable` と伝える
- CLI には `[長期履歴] 保留・変更履歴だけに一致したため、変更履歴も渡します: user.home_location` と表示する

### 誤発動

Current と Pending の発言の言い回しが違うだけで R4 が働くことがある。現在値が「川崎在住です。」、保留が「今は名古屋に住んでいます。」のとき、「今どこに住んでいる？」の検索語「住ん」は保留にだけ一致する（テスト `test_r4_fires_when_only_the_wording_of_current_and_pending_differs`）。渡るのはその属性の変更履歴だけで、件数は MVP13 の上限（属性3つ、1属性10件）の範囲にとどまる。

実際の Claude の評価では、R4 は「名古屋の件はどうなった？」以外の質問では働かなかった（8 の評価のほか、調査で記録したプランナーの出力 48件を修正後の `retrieve()` に再生して、D1・D2 とも名古屋以外の 42件で 0件）。

**適用範囲を絞る案（未実装）**: R4 の一致を、Pending の `proposed_value` / `target_value` と History の `value` / `old_value`（値）だけに限り、`evidence`（発言の文面）を除く。上の誤発動は「住ん」が保留の evidence にだけ一致して起きるので、これで防げる。評価で R4 が働いたのはすべて「名古屋」（保留の `proposed_value` と変更履歴の `value`）で、この案でも同じ結果になる。判断は1つの検索対象の集合を変えるだけで、複雑にはならない。ただし、値の言い換え（「名古屋市」と「名古屋」など）では一致しにくくなる。今回は誤発動の害が上限の範囲の履歴の追加にとどまることを確かめるまでにし、変更していない。

## 6. 変更したファイル

| ファイル | 内容 |
|---|---|
| `src/ai_context_memory/structured_memory.py` | `attribute_catalog`、`resolve_attribute` を追加 |
| `src/ai_context_memory/search.py` | `MAX_CATALOG_ATTRIBUTES`、`SearchPlan.attributes` / `ignored_attributes`、`parse_plan` での `attributes` の解析、`matching_records`（上限なしの一致）、`MEMORY_SEARCH_KEYS` |
| `src/ai_context_memory/llm.py` | 検索プランナーへの指示（属性一覧と `attributes`）、`format_attribute_list`、`format_retrieval_status`、回答 AI への指示（`[retrieval status]`）、`format_prompt` / `complete` / `plan_search` の引数 |
| `src/ai_context_memory/chat.py` | `retrieve()` で属性一覧・選択の検証・R4・検索の状態。`Retrieval` に `attribute_list` / `selected_attributes` / `rejected_attributes` / `ignored_attributes` / `auto_history_attributes` / `status`。`_expand_history` に対象を絞る引数。`send()` で状態を渡す |
| `src/ai_context_memory/cli.py` | 読み込み失敗の表示、`[属性一覧]` / `[属性選択]` / `[検索状態]` / R4 の表示、`send()` へ状態を渡す |
| `tests/test_memory_attribute_selection.py`（新規） | 75件（下記） |
| 既存のテスト | 下記「既存テストの変更」 |
| `README.md`、`docs/mvp-14.md` | 説明と限界 |

### 既存テストの変更

意図した変更に合わせたものだけ。

| テスト | 変更 |
|---|---|
| テスト用の LLM（7つ。`test_memory.py`・`test_search.py`・`test_working_memory.py`・`test_memory_confirmation.py`・`test_memory_pending.py`・`test_structured_memory.py`・`test_working_memory_history.py`）の `plan_search` | 引数 `attribute_list` を受け取り、検索プランナーへの入力の記録に含める（4つ） |
| 回答 AI への入力を完全一致で比べていたテスト（`test_structured_memory.py` 6件、`test_memory_pending.py` 1件、`test_memory_history_scope.py` 1件） | `[retrieval status]` を除いて比べ、状態の行を別に確かめる（検索不要・検索の失敗・何も保存されていない・属性が見つからない） |
| 検索プランナーへの入力を比べていたテスト（`test_structured_memory.py` 3件。うち2件は上と重複） | 属性一覧のブロックを期待値に足し、値が入らないことを確かめる |
| `test_pending_hit_brings_the_current_value_of_its_attribute`（MVP13） | 「保留だけがヒットしたら変更履歴は渡さない」から、R4 で変更履歴を渡すに変えた |

変えたテストは、テスト用の LLM を除いて10件。MVP9〜MVP13 の保存・保留・確認・取り消し・回答・属性展開のテストは、上の期待値の変更以外は変えていない。

## 7. テストの結果

### pytest（Claude をモック。保存先は pytest の一時ディレクトリ）

**793 passed, 2 xfailed**（MVP13 の 718 件に、新規 75 件）。MVP9〜MVP13 のテスト（保存・保留・確認・取り消し・回答・属性展開）は、6 の「既存テストの変更」以外は変えずに通る。`git diff --check` も問題なし。

新規テスト（`tests/test_memory_attribute_selection.py`）。データは MVP13 のテストと同じく実際の処理で作るか、保存形式で直接書き、検索プランだけを差し替えた。

| 確認したこと | テスト |
|---|---|
| 一覧は Current と未解決の Pending の識別子だけ（値・発言なし）、決定的な順序。番号付きの key をまとめ、subject を分ける。解決済み・取り消し済みの保留は含めない | `test_catalog_*`、`test_resolved_and_reverted_pending_are_not_in_the_catalog` |
| 完全一致（`user.home` / `home` は不一致、`hometown` は別、番号付きの key・大文字は読み替えない、subject の無い key は1つに決まるときだけ） | `test_resolve_attribute_accepts_exact_identifiers_only`（12通り） |
| 「住まい」から `home_location` を選べる（検索語は0件でも再検索しない） | `test_selected_attribute_is_found_without_a_matching_query` |
| `residence` で保存されていれば `residence` を使い、一覧に無い `home_location` へ読み替えない | `test_actual_stored_key_is_used_even_if_it_is_named_residence` |
| 「勤務先」「職場」「どこで働いている」 | `test_paraphrased_questions_select_the_same_attribute` |
| 番号付きの key、別 subject の同名 key、似ているが違う key、一覧に無い key（作らない） | `test_numbered_key_*`、`test_same_key_of_another_subject_*`、`test_similar_but_different_keys_*`、`test_attribute_not_in_the_list_*` |
| `needs_memory=false` との組み合わせ、MVP13 形式の検索プラン、選択の上限 | `test_selection_with_*`、`test_plan_without_attributes_key_*`、`test_selected_attributes_are_capped` |
| 選ばれた属性の並び（変更履歴の上限で、検索語に一致した属性を落とさない） | `test_selected_attributes_are_ordered_by_search_evidence`（5属性すべてが一覧の順に選ばれた場合）、`test_history_limit_prefers_matched_attributes_over_selected_ones`（変更履歴の上限3つを超える選択。状態の行と MVP13 の見出しの数が一致すること） |
| 過剰な選択で、検索語が一致した属性を落とさない | `test_over_selection_does_not_push_out_the_attribute_the_query_matched`（無関係な5属性が選ばれ、「名古屋」が保留にだけ一致）、`test_status_omitted_attributes_of_current_values`（上限で落ちるのは選ばれただけの属性） |
| 一覧の上限（100はちょうど渡す、101は渡さず検索語で探す）、入力サイズの測定 | `test_catalog_at_the_limit_is_provided`、`test_catalog_over_the_limit_falls_back_to_search_terms`、`test_attribute_list_size_is_measurable` |
| 変更履歴だけにある属性を、存在しないと扱わない | `test_history_only_attribute_is_not_in_the_list_and_not_described_as_missing` |
| 検索の状態: 取得できた／0件／属性を特定できない／検索しなかった／History だけ読めない（Current・Pending は found）／作業記憶の変更履歴が読めない／省略（Pending・従来の記憶・Current の属性）／空だと確かめた範囲だけ none_stored／再検索の失敗／ブロックに値・パス・例外が無い | `test_status_*`（12件） |
| R4: 名古屋（属性を一覧から選んでいても働く）、Current に一致する語では働かない、言い回しの違いによる誤発動（1属性の履歴だけ）、無関係な属性の履歴が混ざらない、プランナーの要求を妨げない、MVP13 の上限、履歴が読めないときの状態、変更履歴だけに一致したときに再検索しない | `test_r4_*`（8件） |
| 指示（プランナー・回答。選びすぎを抑える指示を含む）と、指示に評価用の値が無いこと | `test_search_plan_prompt_*`、`test_system_prompt_explains_the_retrieval_status`、`test_new_instructions_do_not_name_evaluation_values` |

## 8. 実際の Claude による評価

評価用データは、実際の処理（発言の抽出 → add・置換・保留、`/confirm`、`/revert`。抽出 AI と確認の判断だけモック）で作り、日時は1つの時計から `datetime` の加算で作った。Claude に渡す前に、すべての日時が ISO 8601 として読めること、変更履歴が時刻順であること、現在値の `updated_at` がその値を書いた最後のイベントの日時と一致することを機械的に確かめた（5つのデータすべて合格）。

| データ | 内容 |
|---|---|
| D1 | 調査と同じ5属性（hometown、home_location: 川崎→名古屋を確認して置換→取り消し、p003 が未解決、employer: A社→B社、hobby + hobby_2、favorite_color） |
| D2 | 言い回しと key 名を変えた（key `residence`、「川崎在住です。」「名古屋に転勤になりました。」「勤め先はA社です。」） |
| D100 / D120 | D1 に属性を足して 100 / 120 属性（`work_company`、`home_city` のような似た key を含む） |
| D1H | D1 の変更履歴ファイルの末尾を壊したもの |

修正前は `aca71b3` のコード、修正後は作業ツリーのコードで、同じデータ・同じ質問を、検索プランも回答も実際の Claude で実行した（製品と同じ経路）。

### 修正前後（D1、6問 × 3回）

| 質問 | 正しい属性 前→後 | 無関係な属性を渡した回 前→後 | 必要な変更履歴 前→後 | プランナー呼び出し 前→後 |
|---|---|---|---|---|
| 住まいについて教えて | 3/3 → 3/3 | 0 → 0 | - | 1.0 → 1.0 |
| 居住地は？ | 3/3 → 3/3 | 0 → 0 | - | 1.0 → 1.0 |
| どこで働いている？ | **1/3 → 3/3** | 0 → 0 | - | 2.0 → 1.0 |
| 名古屋の件はどうなった？ | 3/3 → 3/3 | 0 → 0 | 3/3 → 3/3 | 1.0 → 1.0 |
| なぜ記憶が変わった？ | 3/3 → 3/3 | 0 → 1 | 3/3 → 3/3 | 1.0 → 1.0 |
| 趣味は何？ | 3/3 → 3/3 | **3 → 0** | - | 1.0 → 1.0 |
| 合計 | 16/18 → 18/18 | 3/18 → 1/18 | 6/6 → 6/6 | 1.17 → 1.00 |

- 修正前の「どこで働いている？」の見逃し2回は、検索語（勤務・会社・職場 → 仕事・勤め・work）が発言の「働いて」に当たらなかった1回と、2回目の検索プランが「JSON・説明文・JSON」の形で読めなかった1回（調査と同じ失敗）
- 修正前の「趣味は何？」は、検索語「好き」で `favorite_color` も3回とも渡った。修正後は `user.hobby` を選び、検索語は「趣味」「hobby」になった
- 修正後の「なぜ記憶が変わった？」の1回は、変更履歴の検索語（add など）で `favorite_color` の履歴が渡った（MVP13 からの、属性を特定しない質問の扱い）。属性の選択によるものではない
- 「名古屋の件はどうなった？」の変更履歴は、修正前は3回ともプランナーが必要と判断した（調査では6回中3回）。修正後は、プランナーが不要と判断した2回を R4 が補い、3回とも全件（3 of 3）が渡った

#### 最初のプロンプトでの失敗と修正

最初の版（同じ6問 × 3回）では、プランナーが属性を選びすぎた。

| 質問 | 最初の版 | 原因 |
|---|---|---|
| 名古屋の件はどうなった？ | 3/3 で home_location に加えて hometown・employer を選んだ（無関係な現在値と履歴が渡った） | 値（地名）がどの属性のものか分からないので、関係しそうな属性を念のために選んだ |
| なぜ記憶が変わった？ | 3/3 で5属性すべてを選び、**home_location の変更履歴が落ちた**（3/3） | 選ばれた属性を一覧の順（アルファベット順）に先頭へ置いたため、変更履歴の上限3つが employer・favorite_color・hobby で埋まった |

次の2つを直し、上の表は修正後の結果。

1. 選ばれた属性どうしは、検索語にも一致したものを一致の順位で先にする（`retrieve()`。テスト `test_selected_attributes_are_ordered_by_search_evidence`）
2. プランナーへの指示に、「発言が尋ねている種類の情報そのものを表す属性だけを選ぶ」「念のために選ばない。迷う属性は選ばない」「値がどの属性のものか分からないときは、推測で選ばず値を queries に入れる（値に一致した属性は自動的に集められる）」「記憶全体の変化などを尋ねる発言では attributes を空にする」を足した

**最終確認で見つけた同じ系統の不具合**: 1 の修正後も、検索語に一致しない属性が上限いっぱい（5つ）選ばれると、検索語が保留にだけ一致した属性（「名古屋」→ home_location）が6番目になり、Current（川崎）も R4 の History も渡らなかった（モックで再現。評価では観測していない）。2 の「上限で落とす順」に直した（テスト `test_over_selection_does_not_push_out_the_attribute_the_query_matched`）。修正の前後で、評価で記録したプランナーの出力 56件（修正後の評価 38件と最初の版の 18件）を `retrieve()` に再生し、回答 AI へ渡る Current・Pending・History・状態がすべて同じことを確かめた（AI は呼んでいない）。下の評価の結果はこの修正の影響を受けない。

### 保存時の言い回し・key 名を変えたもの（D2、4問 × 2回）

| | 正しい属性 | 無関係な属性 | 必要な変更履歴 | プランナー呼び出し |
|---|---|---|---|---|
| 修正前 | 7/8 | 0/8 | 2/2 | 1.25 |
| 修正後 | 8/8 | 0/8 | 2/2 | 1.00 |

修正前の見逃しは「どこで働いている？」（発言は「勤め先はA社です」）。修正後は `user.employer`、`user.residence` を一覧から選んだ。

### 属性数（修正後、2問 × 2回）

| | 一覧 | 正しい属性 | 無関係な属性 | プランナーの入力 | プランナー呼び出し |
|---|---|---|---|---|---|
| D100（100属性） | 渡した（約1,980文字） | 4/4（一覧から選択） | 0/4 | 約4,560トークン | 1.0 |
| D120（120属性） | 渡さない（`not_provided`） | 4/4（検索語で） | 0/4 | 約3,770トークン | 1.5（「どこで働いている？」は2回とも再検索） |

100属性では、似た key（`work_company`、`home_city`）があっても正しい属性だけを選んだ。120属性では MVP13 までと同じく検索語の当たり次第で、回答 AI には `attribute list: not_provided (120 attributes)` が渡った。

### 変更履歴の読み込み失敗（D1H、2問 × 2回）

| | Current と Pending | 変更履歴について |
|---|---|---|
| 修正前 | 4/4 で渡った | 「変更履歴が見つからなかったため確認できません」（2回）、「手元には変更履歴の詳細がない」「今回参照できる記録からは確認できません」（各1回）。安全だが、理由（読めなかった）とは違う |
| 修正後 | 4/4 で渡った | 4/4 で「今回は変更履歴を読み込めなかったため確認できませんでした」。Current と Pending による説明（保存済み・確認待ち・未確定）は続けた |

CLI は修正後、`[長期履歴検索結果] 読み込めませんでした（0件ではありません）` と `[検索状態] … 長期履歴: unavailable …` を表示した。

### 回答の内容（全 60 回のうち、修正前 30・修正後 30）

| 観点 | 修正前 | 修正後 |
|---|---|---|
| 記録に無い事情・理由の推測 | 0 | 0 |
| 記録が無いと言い切った（none_stored でないのに） | 0 | 0 |
| 検索の状態（読み込めなかった）を正しく説明した | 0/4 | 4/4 |
| 名古屋の件で、発言・確認・承認・取り消しをそれぞれ述べた（履歴が渡った回。語の有無で数え、修正後の D1 の3回は本文を読んで区別を確かめた） | 5/5 | 5/5 |

自動の目印（「転勤」「記録はありません」など）が付いた回答は本文を読んで確かめた。「転勤」は D2 の発言の引用、「記録はありません」は「この変更には確認の質問や承認の記録はありません」（add のイベントの説明）で、どれも推測や存在の否定ではなかった。

### 試行回数と不確実性

各質問・ケース2〜3回の観測で、改善を保証するものではない。特に、属性を選びすぎる傾向はプロンプトの文面に強く左右された（最初の版で 6/18 → 修正後 1/18）。

実際の Claude の呼び出しは、記録したもので 86 ターン・179 回（検索プランと回答）。このほか、動作確認 2 ターン（4 回）と、評価スクリプトの不具合（子プロセスの出力の文字コード）で結果を失った 36 ターン（修正前後の D1、約 75 回）を実行した。失った 36 ターンは、上の表にも合計にも含めていない（結果が無いので、有効・無効の判定もしていない）。

### 評価スクリプトの問題と対策

評価スクリプト（リポジトリ外の作業用。1ターンを子プロセスで実行し、親が結果の JSON を集める）で、最初の D1 の比較（36 ターン）の結果を失った。

**原因**: 子プロセスを `python -I`（環境変数を無視する隔離モード）で起動したため、親が指定した `PYTHONIOENCODING=utf-8` が効かず、子は標準出力を Windows の既定（cp932）で書いた。親は UTF-8 として読もうとして復号に失敗し、`stdout` が `None` になり、`.strip()` の `TypeError` で集計全体が止まった。結果は子プロセスの標準出力にしか無かったので、36 ターン分（約 75 回の呼び出し）が残らなかった。

**行った対策**: 子で `sys.stdout.reconfigure(encoding="utf-8")` を明示し、親は1ターンごとに結果を書き出して flush し、解析に失敗したターンは `error` として記録して続けるようにした。この後の 86 ターンは失っていない。

**今後の実際の Claude の評価への推奨**（今回は実装していない）:

- 子プロセスの文字コードは、環境変数に頼らず子の中で明示する（`-I` では `PYTHON*` の環境変数が無視される。`-X utf8` か `reconfigure`）
- 解析・表示・集計の前に、試行ごとの生の出力（標準出力・標準エラー・終了コード）をそのままファイルへ保存する。集計は保存したファイルから行い、失敗しても生の結果は残す
- 試行ごとに、実行ID・開始時刻・コードの版・データ・質問・状態（`ok` / `error` / `timeout`）を記録し、有効な結果と失われた・無効な結果を区別して数える（報告には両方の数を書く）
- 1つの試行の例外やタイムアウトで、残りの試行を止めない

## 9. トークン数と応答時間

| | 修正前 | 修正後 |
|---|---|---|
| 検索プランナーの指示 | 2,915 文字 | 3,830 文字 |
| 検索プランナーの入力（1回、5属性） | 約3,050 トークン | 約3,790 トークン |
| 同（100属性） | - | 約4,560 トークン |
| 1ターンのプランナー呼び出し（D1 / D2） | 1.17 / 1.25 回 | 1.00 / 1.00 回 |
| 回答 AI の指示 | 7,286 文字 | 8,443 文字 |
| `[retrieval status]` | - | 平均 約320 文字 |
| 回答 AI の入力（D1 平均） | 約6,500 トークン | 約7,450 トークン（+約950、+15%） |
| 1ターンの時間（D1 / D2 平均） | 12.0 / 12.4 秒 | 11.5 / 10.3 秒 |

AI の呼び出しは増やしていない（R4 と状態の判定は Python の文字列照合だけ）。1回あたりの入力は増えたが、再検索（2回目のプランナー呼び出し）が減り、1ターンの時間は観測では増えていない。`[retrieval status]` は「1+1は？」のような検索不要のターンにも付く。

## 10. 残る問題

| 問題 | 内容 |
|---|---|
| 属性を選ぶのは AI（過剰な選択） | 選びすぎ・選び漏れはプロンプトに左右される（最初の版で無関係な属性 6/18、修正後 1/18）。Python は一覧に無いものを防ぐだけで、一覧にある無関係な属性は防げない。上限で落とす順（2）で、検索語に一致した属性を落とさないようにしただけ |
| 100属性を超えたとき | 一覧を渡さず、MVP13 までと同じ限界（言い回しの当たり次第）に戻る。段階検索などは未実装 |
| R4 の誤発動 | 現在値と保留の発言の言い回しが違うと働く（モックで確認。実際の評価では観測なし）。害はその属性の変更履歴（上限の範囲）が増えることだけ。値だけに絞る案は 5 に書いた（未実装） |
| R4 は `queries` だけ | `needs_memory` が false で検索語が無いターンでは働かない |
| 検索プランの JSON の崩れ | 「JSON・説明文・JSON」の形の出力は、今回の評価でも修正前に1回あった。`search: failed` / `retry_failed` として伝わるが、回復はしない（1 の理由） |
| 回答 AI の入力の増加 | 約15%。指示と状態ブロックの分 |
| 番号付きの key の規則 | テストデータの `attr_002` のような key は、既存の規則で1つの属性 `attr` にまとまった。AI が付ける key で起きるかは未検証（MVP13 から残る） |
| `residence` と `home_location` | 2つの AI が別々に key を付けるので、同じ意味の属性が別の key で増えうる。一覧から選べるようになったが、統合はしていない |
| 試行回数 | 実際の Claude の評価は各質問・ケース2〜3回。改善を保証するものではない |
| 評価スクリプト | 生の出力の保存と、有効・無効な結果の区別は未実装（8 の推奨） |
