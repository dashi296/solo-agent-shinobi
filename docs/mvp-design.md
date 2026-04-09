# MVP Design

## この文書の目的

この文書は、`solo-agent-shinobi` を最初に実装可能な形へ落とし込むための MVP 設計を定義します。

- `README.md`: プロダクト概要
- `docs/product-spec.md`: 外部仕様
- `docs/architecture.md`: モジュール責務
- `docs/mvp-design.md`: 実装開始に必要な具体設計

対象は最初の 1 本の実装ラインです。将来の拡張余地は残しますが、MVP に不要な複雑さは入れません。

## MVP の到達点

MVP が満たすべきこと:

1. `shinobi run` または `shinobi run --issue <id>` で 1 Issue を処理できる
2. GitHub の label / comment / PR を主要な状態管理として扱える
3. ローカルに最小 state を保存し、途中停止から再開できる
4. 停止条件と危険判定により unsafe な自動継続を避けられる
5. docs 修正、テスト追加、小規模バグ修正のような低リスク任務を完了できる

MVP でやらないこと:

- 複数 Issue の並列処理
- 複数 agent の協調
- 高リスク変更の自動マージ
- 長期の高度な計画最適化
- 巨大 Issue の自動分割

## 主要ユースケース

### 1. 自動選択で 1 任務を実行する

```bash
shinobi run
```

- `shinobi:ready` の Issue を 1 件選ぶ
- 作業開始コメントを投稿する
- branch を作る
- 実装と検証を行う
- PR を作成または更新する
- self-review を行う
- マージ可能ならマージ、危険なら停止する

### 2. 特定 Issue を指定して実行する

```bash
shinobi run --issue 123
```

- 指定 Issue が実行可能かを確認する
- 指定 Issue 自身の stale mission があり、ownership と phase を復元できる場合だけその mission を resume する
- 別 Issue の active mission や local-only mission が残っている場合は横取りせず停止する
- 状態不整合があれば停止して報告する

### 3. 現在の任務状態を確認する

```bash
shinobi status
```

- 現在の issue / PR / branch / phase を表示する
- 最終実行時刻と停止理由を表示する

## CLI 設計

MVP では次の 4 コマンドをサポートします。

```bash
shinobi init
shinobi status
shinobi run
shinobi run --issue 123
```

`plan` `execute` `review` `merge` `watch` は将来コマンド候補ですが、MVP では公開コマンドに含めません。

### `shinobi init`

- `.shinobi/` ディレクトリを作る
- `.shinobi/config.json` の雛形を作る
- workspace / installation ごとに一意な `agent_identity` を生成して `.shinobi/config.json` へ書き込む
- `.shinobi/summary.md` と `.shinobi/decisions.md` の空テンプレートを作る
- `.shinobi/run.lock` を初期化可能な状態にする
- 初回動作に必要な前提を表示する

### `shinobi status`

- ローカル state を読む
- GitHub 上の Issue / PR 状態を照合する
- GitHub 照合に失敗した場合でも、ローカル state だけで直近の mission 状態を表示する
- active mission がなければ直近の mission summary を表示する
- 不整合があれば warning を出す

### `shinobi run`

内部的には次の phase を順に進めます。

1. `select`
2. `start`
3. `context`
4. `execute`
5. `publish`
6. `review`
7. `merge`
8. `finalize`

## 実行フロー

```text
run
  -> select issue
  -> acquire mission state
  -> mark working
  -> build context
  -> edit and verify
  -> push branch / create or update PR
  -> inspect CI and diff
  -> retry within limits
  -> merge or stop
  -> close issue / report result
```

### Phase 1: select

入力:

- CLI 引数
- GitHub Issue 一覧
- ローカル state

処理:

- run 開始時は GitHub の recovery 判定や state 修復より前に `.shinobi/run.lock` を確認する
- `.shinobi/run.lock` に他 owner の live run が見つかった場合は、同一 workspace で別 run が進行中とみなして停止する
- `.shinobi/run.lock` は `heartbeat_at + mission_lease_minutes` を超えたら stale lock とみなし、次の run は reconciliation や recovery に入る前に current `run_id` でその lock を原子的に takeover してから解放または上書きできる
- select phase で `.shinobi/run.lock` を取得または stale lock を takeover していない run は GitHub の label cleanup や `needs-human` 付与を行わない
- lock を確認した後で、GitHub の Issue / PR / label 状態と `.shinobi/state.json` を reconciliation する
- `--issue` がある場合は、その Issue を最優先の mission candidate に固定する
- `--issue` の対象に GitHub 上の active mission または local-only mission が残っていれば、その mission だけ resume 可否を判定する
- `--issue` の対象以外に active mission または retryable な local-only mission が見つかった場合は、それが stale であっても別 mission への横滑りや cleanup を避けるため停止する
- `--issue` がない場合だけ、stale な active mission が state にだけ残っているかを確認する
- local-only mission で、対応する branch が存在し、state または local log に `retryable_local_only` 相当の retry 記録が残り、`agent_identity`, `run_id`, `issue_number`, `branch`, `phase` を現在設定の一意な `agent_identity` と照合でき、かつ branch 作成後の retryable な `start` 失敗記録がある場合だけ、その branch と復元可能な最小 state を使って同じ mission を再開する
- local-only mission でなく GitHub 上にも対応する active 状態が無い場合は、GitHub の状態を優先して state を修復する
- GitHub 側に `shinobi:working` / `shinobi:reviewing` が残っている場合は lease と PR / branch の生存確認で stale 判定する
- stale でない active mission は、同一 `agent_identity` であっても lease 有効中なら自動 resume しない。live mission は `.shinobi/run.lock` の owner だけが継続できるものとし、owner でない run は停止する
- lease が切れた stale mission は、他 owner の live lock が存在しないことを確認できた run が recovery / cleanup を行える。stale lock が残っている場合は、その run がまず takeover してから続行する
- stale mission を扱う run は、machine-readable な Shinobi コメントと local / PR metadata から `agent_identity`, `run_id`, `issue_number`, `branch`, `phase`, `pr_number` を整合付きで復元でき、`agent_identity` が現在設定の一意な `agent_identity` と一致する場合に限って resume する
- stale mission で復元情報が不足するか整合しない場合は、branch や PR が残っていても自動 resume せず、active label を外して `shinobi:needs-human` に遷移し、回復不能理由を Issue に記録する
- `agent_identity` が欠損または不一致の active mission は ownership 不明として自動 resume は行わない。lease が有効な live mission なら GitHub 上の label や comment を変更せず停止する。lease が切れた stale mission なら、他 owner の live lock が存在しないことを確認できた run に限って `shinobi:needs-human` への cleanup と ownership 不一致コメントの投稿だけを許可する
- `--issue` がなければ `shinobi:ready` を優先度順で 1 件選ぶ
- reconciliation 後も lease が生きている別の active mission が GitHub 上に確認できる場合だけ停止する

出力:

- `MissionCandidate`

### Phase 2: start

処理:

- 実行可能 label を確認する
- `.shinobi/run.lock` を取得または select phase から引き継ぎ、`agent_identity`, `run_id`, `pid`, `started_at`, `heartbeat_at` を記録する
- lock owner であることを確認してから `feature/issue-<id>-<slug>` branch を作る
- provisional な `.shinobi/state.json` を更新する
- `shinobi:working` を付与する
- `shinobi:ready` を除去する
- lease 情報と recovery 用メタデータを含む machine-readable な mission-state コメントを投稿または更新する
- lease を `now + mission_lease_minutes` で初期化する

fatal 時の補償動作:

- lock 取得後に branch 作成までは成功し、その後の active mission 用 state 更新が失敗した場合に限って、その mission を `start` 未完了の local-only mission として retryable に残す。このとき tool は `retryable_local_only: true` と失敗理由を state または local log の少なくとも一方へ耐久的に記録する
- branch 作成そのものに失敗した場合は local-only mission を resume 対象にせず、その run 内で停止理由を記録して終了する
- local-only mission は次回 run の reconciliation で branch / issue 番号 / phase を照合し、Shinobi 自身が state または local log に残した retryable 記録がある場合だけ resume または cleanup 判定する
- GitHub の active label 更新後に fatal が起きた場合は、active label を外して `shinobi:needs-human` を付け、fatal 理由を Issue に投稿する
- fatal / 完了 / 明示停止のいずれでも、現在 run が owner なら `.shinobi/run.lock` を解放する

停止条件:

- Issue が closed
- 既存 PR や branch が不整合
- 実行中の別 mission と衝突する

### Phase 3: context

処理:

- Issue 本文とチェックリストを抽出する
- 変更対象ファイル候補を最小限推定する
- `.shinobi/summary.md` と `.shinobi/decisions.md` を読む
- エージェントへ渡す実行コンテキストを構築する

設計原則:

- repo 全体を読まない
- Issue に無い要件は原則追加しない
- 不明点は conservative に扱う

### Phase 4: execute

処理:

- 対象ファイルだけ編集する
- high-risk path 候補に実際に触れる必要があるかを、publish 前に最終判定する
- publish 前に high-risk path が確定した場合でも、human handoff に必要な差分があるなら branch を push し、原則 draft PR を作成または更新してから `needs-human` または `blocked` に遷移する
- この pre-publish stop で PR を作成または更新した場合も、machine-readable な mission-state コメントを同じ mission の comment 上で upsert し、最新の `pr`, `phase`, `lease_expires_at` を反映して recovery と整合させる
- publish 前に差分が無いか共有価値が無い場合だけ、PR を作らず停止する
- execute 中は `mission_heartbeat_interval_minutes` ごとに定期 heartbeat を更新する
- 長時間処理に入る前と各 retry 後にも即時 heartbeat を更新する
- lint / typecheck / test を実行する
- 必要なら修正を繰り返す
- 変更要約を生成する

結果:

- `ExecutionResult`

### Phase 5: publish

処理:

- PR を作成または更新する
- `shinobi:reviewing` へ遷移する
- `shinobi:working` と `shinobi:ready` を除去する
- machine-readable な mission-state コメントを `phase: publish` と最新 `pr` で更新する
- publish 完了時に lease heartbeat を更新する
- diff と CI を review phase から参照できる状態にする

### Phase 6: review

処理:

- diff 規模を確認する
- publish 後の diff を再確認し、execute 時点で見逃した high-risk path や scope 逸脱があれば `needs-human` に遷移する
- CI 待機の前後と polling 中に lease heartbeat を更新する
- machine-readable な mission-state コメントを `phase: review` と最新 `lease_expires_at` で更新し続ける
- CI 結果を確認する
- review loop 上限内なら再試行する

主な判定軸:

- changed files 数
- changed lines 数
- review loop 回数
- 失敗テストの種類
- high-risk path の有無
- `shinobi:risky` label の有無

### Phase 7: merge

処理:

- required checks の成功を待つ
- auto-merge 対象か判定する
- 条件を満たせば squash merge する

マージ前提:

- CI green
- risky でない
- review 上限未超過
- issue scope を逸脱していない

`shinobi:risky` が付いた Issue は実行対象からは外しません。MVP では「実装と PR 更新までは行うが、自動マージはせず `needs-human` に寄せる」扱いにします。これは issue-level の merge policy であり、high-risk path のような実装停止条件とは別に扱います。

### Phase 8: finalize

処理:

- Issue に完了または停止コメントを投稿する
- label を `shinobi:merged` / `shinobi:blocked` / `shinobi:needs-human` のいずれか 1 つに正規化して更新する
- `shinobi:risky` を除く他の phase / 停止 / 完了 label は必ず除去する
- issue を close するか、継続可能な状態に残す
- `.shinobi/state.json` を完了状態へ更新する
- 現在 run が owner なら `.shinobi/run.lock` を解放する

## 状態モデル

### GitHub 側の状態

```text
ready -> working -> reviewing -> merged
       └-> blocked
       └-> needs-human
blocked -> ready
needs-human -> ready
```

補助ルール:

- `working` と `reviewing` は同時に付けない
- `merged` だけを終端扱いにする
- `risky` は補助属性であり phase ではない
- `risky` は start を止める label ではなく、auto-merge を止める label として扱う
- `risky` は issue-level の manual merge 指示であり、high-risk path は publish 前でも停止しうる execution risk として扱う
- `blocked` と `needs-human` は open issue 上の停止状態であり、人手で blocker 解消や要件補完を行った後に `ready` へ戻せる
- `risky` を除く状態 label は常に 1 つだけ残るよう正規化する

### ローカル state

`.shinobi/state.json` の想定例:

```json
{
  "issue_number": 123,
  "pr_number": 456,
  "branch": "feature/issue-123-login-form",
  "agent_identity": "owner/repo#default@mbp14-7f3a2c",
  "run_id": "20260409T100000-issue-123",
  "phase": "review",
  "review_loop_count": 1,
  "retryable_local_only": false,
  "lease_expires_at": "2026-04-09T10:30:00+09:00",
  "last_result": "ci_failed",
  "last_error": null,
  "last_completed_mission": {
    "issue_number": 122,
    "pr_number": 455,
    "branch": "feature/issue-122-doc-cleanup",
    "result": "needs-human",
    "stop_reason": "max_review_loops_exceeded",
    "finished_at": "2026-04-09T09:30:00+09:00"
  },
  "updated_at": "2026-04-09T10:00:00+09:00"
}
```

設計方針:

- active mission は 0 か 1 のみ
- 直近の完了または停止結果を `last_completed_mission` に保持する
- active mission には workspace / installation ごとに一意な `agent_identity` を保持し、別の Shinobi instance や operator が残した mission を誤回収しないようにする
- `agent_identity` だけでは同一 workspace 内の live run を区別できないため、実行中 owner の排他は `.shinobi/run.lock` で補完する
- `.shinobi/run.lock` も lease ベースで扱い、`heartbeat_at + mission_lease_minutes` を超えた lock は stale として次の run が回収可能にする
- active mission には `run_id` を保持し、local-only mission と stale recovery の同一性確認に使う
- state は再開補助であり truth ではないが、`start` 未完了の local-only mission を回復するための一次手掛かりとして扱う
- local-only mission の resume は state 単独では許可せず、state または local log に残した `retryable_local_only` 相当の retry 記録で裏付ける
- GitHub 側と矛盾したら GitHub を優先する
- run の先頭で reconciliation してから active mission 判定を行う
- run 開始時は GitHub recovery 判定の前に `.shinobi/run.lock` を確認し、他 owner の live run があれば停止する
- active mission には lease を持たせ、期限切れなら stale recovery 対象にする
- lease は execute 中の定期更新に加え、start / publish / review / retry / CI polling ごとに heartbeat 更新する
- resume は stale recovery に限定し、lease 有効中の live mission には別プロセスを attach させない

### ラベル遷移ルール

各 phase で更新する label は次の通りです。

- start: `shinobi:working` を付与し、`shinobi:risky` を除く他の状態 label を除去する
- publish: `shinobi:reviewing` を付与し、`shinobi:risky` を除く他の状態 label を除去する
- pre-publish stop: `shinobi:blocked` または `shinobi:needs-human` を付与し、`shinobi:risky` を除く他の状態 label を除去する
- finalize merged: `shinobi:merged` を付与し、`shinobi:risky` を除く他の状態 label を除去する
- finalize blocked: `shinobi:blocked` を付与し、`shinobi:risky` を除く他の状態 label を除去する
- finalize needs-human: `shinobi:needs-human` を付与し、`shinobi:risky` を除く他の状態 label を除去する

`shinobi:risky` は補助ラベルなので自動では除去しません。

## ドメインモデル

MVP で必要な主要モデル:

### `Mission`

- issue number
- title
- labels
- branch name
- pr number
- risk level
- phase

### `RunPolicy`

- mission lease minutes
- mission heartbeat interval minutes
- max review loops
- max commits per issue
- max changed files
- max lines changed
- max runtime minutes
- max token budget
- auto merge enabled

### `ExecutionResult`

- changed files
- changed lines
- lint status
- typecheck status
- test status
- summary
- follow-up needed

### `StopDecision`

- reason
- retryable
- needs human
- recommended label
- comment body

## モジュール責務

MVP では既存アーキテクチャ案を次のように具体化します。

### `cli.py`

- コマンド引数を解釈する
- 設定読込と phase 実行を開始する

### `config.py`

- `.shinobi/config.json` を読む
- default 値を補完する
- unsafe な設定を起動時に拒否する

### `models.py`

- `Mission`, `RunPolicy`, `ExecutionResult` などを定義する

### `github_client.py`

- Issue 検索
- label 更新
- comment 投稿
- PR 作成 / 更新
- CI 状態取得
- merge 実行

### `issue_selector.py`

- ready issue を選ぶ
- 指定 issue の妥当性を判定する

### `context_builder.py`

- Issue とローカル知識から最小実行コンテキストを組み立てる
- 読むべきファイル一覧を返す

### `executor.py`

- 実装エージェントを呼び出す
- 検証コマンドを回す
- 実行結果を構造化する

### `reviewer.py`

- diff / CI / 停止条件を判定する
- 再試行可否を返す

### `merger.py`

- auto-merge 対象かを判定する
- merge と issue close を行う

### `state_store.py`

- `.shinobi/state.json` の保存と復元
- GitHub state との整合性確認を補助する

## Context Builder 設計

MVP の重要点は「必要最小限しか読まない」ことです。

入力:

- Issue 本文
- Issue コメントのうち marker 付きの shinobi 関連ログ
- `.shinobi/summary.md`
- `.shinobi/decisions.md`
- `.shinobi/run.lock`
- 対象ファイル候補

初回 run では `init` が生成した空テンプレートを読む前提にします。欠損時は fatal にはせず、空ファイル相当として扱います。

Shinobi 関連ログは自由文検索ではなく、HTML comment marker の中に固定 schema の key-value block を持つ machine-readable comment を前提にします。最低でも `issue`, `branch`, `phase`, `lease_expires_at`, `pr`, `agent_identity`, `run_id` を含めます。recovery は自由文本文ではなく marker 内 block だけを parse 対象にします。`agent_identity` は `init` が生成する workspace / installation ごとの一意 ID で、複数 runner 間で共有しません。

同じ mission については、開始時に新規作成した mission-state コメントを publish / review / recovery 時に upsert して使い回します。high-risk path などで publish 前に停止する場合でも、PR を作成または更新したなら同じ comment を upsert して `pr` と停止時の phase を最新化します。stale recovery は最新の mission-state comment の marker 内 block の値を truth 候補として参照します。`pr: null` は start から publish 前までの mission に限って許容し、その場合は branch 実体と phase 整合を追加で確認します。publish 済みのはずなのに古い phase や `pr: null` のまま放置されたコメントは resume 根拠に使いません。`agent_identity` が現在設定の一意な `agent_identity` と一致しないコメントは ownership 不一致として resume 根拠から除外します。

出力:

- 任務要約
- 完了条件
- スコープ外
- 参照ファイル一覧
- 禁止事項

対象ファイル候補の作り方:

1. Issue 本文に明示されたパスを優先する
2. ラベルから area を推定する
3. 既存 PR があればその diff 周辺を優先する
4. 候補が広すぎる場合は停止寄りにする

## 安全設計

### 停止すべきケース

- Issue 要件が曖昧で完了条件がない
- 差分が上限を超える
- 危険領域のファイルを変更する必要がある
- CI 失敗が再試行上限を超えた
- 既存 branch / PR の整合性が取れない
- stale な GitHub active mission が再開不能だった

### stale mission recovery

MVP では interrupted run からの自動回復をサポートします。

- active label が付いた Issue を見つけたら、まず lease の期限切れ有無を確認する
- `.shinobi/run.lock` に他 owner の live run が見つかった場合は、その run が同一 `agent_identity` であっても resume せず停止する
- `.shinobi/run.lock` は `heartbeat_at + mission_lease_minutes` を超えたら stale lock とみなし、次の run は recovery 前に current `run_id` でその lock を原子的に takeover してから解放または上書きできる
- `--issue` の対象そのものに lease が有効な active mission があっても、自分が `.shinobi/run.lock` の owner でない限り resume しない
- `--issue` が無い通常 run でも、lease が有効な active mission は live mission とみなし、新規 run からは resume しない
- 指定対象以外に active mission や retryable な local-only mission がある場合は、それが stale であっても新規 run や別 mission への切替、cleanup はせず停止する
- GitHub 上に active label が無くても、local-only mission の branch が残っていて、state または local log に Shinobi 自身が残した retryable な `start` 失敗記録がある場合だけ cleanup 前に resume 可否を判定する
- local-only mission の resume は、state または local log から復元した `agent_identity`, `run_id`, `issue_number`, `branch`, `phase` が branch 実体と矛盾せず、`retryable_local_only` 相当の記録が残り、branch 作成後の retryable な `start` 失敗記録で裏付けられる場合に限る
- lease が期限切れの stale mission は、select phase で `.shinobi/run.lock` を取得済みか、残っていた stale lock を current `run_id` で takeover 済みの run だけが recovery / cleanup を続行できる
- stale mission を resume してよいのは、machine-readable な Shinobi コメントと local / PR metadata から `agent_identity`, `run_id`, `issue_number`, `branch`, `phase`, `pr_number` を整合付きで復元でき、`agent_identity` が現在設定の一意な `agent_identity` と一致する場合に限る。publish 前の stale mission では `pr: null` を許容するが、その場合も branch 実体と pre-publish phase の整合が必須
- つまり resume は stale recovery 専用であり、lease が有効な live mission に別プロセスが合流することは許可しない
- lease が期限切れで、PR / branch / machine-readable な Shinobi コメントからも再開情報を復元できない場合は、active label を除去して `shinobi:needs-human` を付ける
- `agent_identity` が欠損または不一致の active mission は ownership 不明として扱い、自動 resume は行わない。lease が有効な live mission なら GitHub 上の label / comment を変更せずに停止して operator 判断へ委ねる。lease が期限切れの stale mission なら、他 owner の live lock が存在しないことを確認できた run に限って `shinobi:needs-human` への cleanup と ownership 不一致コメントを許可する
- stale recovery を行ったときは Issue に recovery comment を残す
- lease は execute 中の定期更新と、phase 遷移時、retry 時、CI polling 中の heartbeat 更新を前提にする
- そのため lease 期限切れは interruption の強いシグナルとして扱う

### risk policy

MVP では次の 2 種類を分けて扱います。

- `shinobi:risky`: Issue-level の manual merge 指示。publish までは進めるが auto-merge はしない
- high-risk path: execution risk。対象ファイルが `migrations/` `infra/` `auth/` `billing/` などに触れる必要があるなら publish 前でも停止しうる

high-risk path の一次判定は context で候補抽出し、最終判定は execute 完了前に行います。publish 前に確定した場合でも、human handoff に必要な差分があるなら branch を push し、原則 draft PR を作成または更新したうえで `needs-human` か `blocked` に遷移します。差分が無いか共有価値が無い場合だけ PR 未作成で停止します。publish 後の review で追加検知した場合は PR を残したまま `needs-human` に遷移します。

### high-risk path 例

- `migrations/`
- `infra/`
- `auth/`
- `billing/`
- secrets や権限設定を含むファイル

high-risk path は config で追加可能にします。

### 停止時の標準動作

1. state に理由を記録する
2. Issue / PR に短い報告を投稿する
3. `shinobi:needs-human` か `shinobi:blocked` を付ける
4. 危険な自動継続をやめる

## 設定ファイル設計

`.shinobi/config.json` 例:

```json
{
  "repo": "owner/repo",
  "main_branch": "main",
  "agent_identity": "owner/repo#default@mbp14-7f3a2c",
  "mission_lease_minutes": 30,
  "mission_heartbeat_interval_minutes": 5,
  "max_review_loops": 3,
  "max_commits_per_issue": 8,
  "max_changed_files": 20,
  "max_lines_changed": 800,
  "max_runtime_minutes": 30,
  "max_token_budget": 40000,
  "auto_merge": true,
  "use_draft_pr": true,
  "merge_method": "squash",
  "labels": {
    "ready": "shinobi:ready",
    "working": "shinobi:working",
    "reviewing": "shinobi:reviewing",
    "blocked": "shinobi:blocked",
    "needs_human": "shinobi:needs-human",
    "merged": "shinobi:merged",
    "risky": "shinobi:risky"
  },
  "high_risk_paths": ["migrations/", "infra/", "auth/", "billing/"]
}
```

方針:

- MVP では 1 repo 専用 config を前提にする
- 環境変数は token や secret のみを扱う
- `docs/product-spec.md` の flat schema を truth として維持する
- label 名や上限値は config で変えられる
- `agent_identity` は `shinobi init` が workspace / installation ごとに一度だけ生成する一意値とし、複数 runner で共有しない

## エラーハンドリング方針

エラーは次の 3 種に分けます。

### `retryable`

- 一時的な GitHub API failure
- flaky test
- CI 待機タイムアウト

挙動:

- 規定回数だけ再試行する

### `mission_blocked`

- 要件不明
- branch 競合
- 高リスク変更

挙動:

- `blocked` または `needs-human` にして停止する

### `fatal`

- config 不正
- 認証不備
- state 読み書き不能

挙動:

- 実行を停止する
- GitHub 側に active label を付けた後の失敗であれば、補償的に `shinobi:needs-human` へ遷移し理由を Issue に残す
- GitHub への補償操作まで失敗した場合は、ローカルに理由を残し `status` で operator action が必要だと表示する

## 観測性

MVP では大げさな telemetry は入れず、次を残します。

- `.shinobi/logs/<timestamp>.log`
- GitHub Issue / PR への短い進捗コメント
- `.shinobi/summary.md` への短い更新
- `.shinobi/run.lock` の heartbeat

重要なのは完全な思考履歴ではなく、再開に必要な要約です。

## テスト計画

MVP 実装時の最低ライン:

### 単体テスト

- Issue 選択
- label 遷移
- branch 名生成
- stop condition 判定
- auto-merge 判定

### 結合テスト

- `run --issue <id>` の happy path
- `run --issue <id>` 実行時に別 Issue の active mission が残っていた場合の安全停止
- 同一 workspace で 2 つ目の `run` が起動した場合、同じ `agent_identity` でも `run.lock` により停止すること
- stale `run.lock` を select phase で takeover した run だけが recovery / cleanup を続行できること
- review loop 上限超過時の停止
- GitHub state とローカル state の不整合検出
- machine-readable な Shinobi コメントからの recovery
- `agent_identity` 不一致の stale mission は resume せず `needs-human` へ cleanup するが、live mission では cleanup しないこと

### 手動確認

- `init` 後に設定が生成される
- `status` が空 state を扱える
- GitHub 照合失敗時でも `status` がローカル state を表示できる
- `run` が完了または安全停止する

## 将来の拡張ポイント

MVP 後の候補:

- `plan` `execute` `review` `merge` の明示コマンド分離
- `watch` モード
- slash command 連携
- follow-up Issue の自動作成
- metrics 可視化
- repo 特性ごとの risk policy 強化

MVP では、これらのために interface を分けつつも、まだ過剰抽象化しません。
