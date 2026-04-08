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
- `config.yml` の雛形を作る
- `.shinobi/summary.md` と `.shinobi/decisions.md` の空テンプレートを作る
- 初回動作に必要な前提を表示する

### `shinobi status`

- ローカル state を読む
- GitHub 上の Issue / PR 状態を照合する
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

- run 開始時に GitHub の Issue / PR / label 状態と `.shinobi/state.json` を reconciliation する
- stale な active mission が state にだけ残っている場合は、GitHub の状態を優先して state を修復する
- GitHub 側に `shinobi:working` / `shinobi:reviewing` が残っている場合は lease と PR / branch の生存確認で stale 判定する
- stale でなく再開可能なら、その Issue / PR / branch から local state を再構築して同じ mission を再開する
- stale かつ再開不能なら、active label を外して `shinobi:needs-human` に遷移し、回復不能理由を Issue に記録する
- `--issue` があればその Issue を対象にする
- なければ `shinobi:ready` を優先度順で 1 件選ぶ
- reconciliation 後も lease が生きている別の active mission が GitHub 上に確認できる場合だけ停止する

出力:

- `MissionCandidate`

### Phase 2: start

処理:

- 実行可能 label を確認する
- `feature/issue-<id>-<slug>` branch を作る
- provisional な `.shinobi/state.json` を更新する
- `shinobi:working` を付与する
- `shinobi:ready` を除去する
- lease 情報を含む開始コメントを投稿する

fatal 時の補償動作:

- branch 作成または state 更新後に GitHub 更新へ失敗した場合は local state を retryable に残す
- GitHub の active label 更新後に fatal が起きた場合は、active label を外して `shinobi:needs-human` を付け、fatal 理由を Issue に投稿する

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
- diff と CI を review phase から参照できる状態にする

### Phase 6: review

処理:

- diff 規模を確認する
- CI 結果を確認する
- review loop 上限内なら再試行する
- 危険変更なら `needs-human` に遷移する

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

`shinobi:risky` が付いた Issue は実行対象からは外しません。MVP では「実装と PR 更新までは行うが、自動マージはせず `needs-human` に寄せる」扱いにします。

### Phase 8: finalize

処理:

- Issue に完了または停止コメントを投稿する
- label を `shinobi:merged` / `shinobi:blocked` / `shinobi:needs-human` に更新する
- `shinobi:ready` `shinobi:working` `shinobi:reviewing` を必ず除去する
- issue を close するか、継続可能な状態に残す
- `.shinobi/state.json` を完了状態へ更新する

## 状態モデル

### GitHub 側の状態

```text
ready -> working -> reviewing -> merged
                     └-> blocked
                     └-> needs-human
```

補助ルール:

- `working` と `reviewing` は同時に付けない
- `merged` と `blocked` は終端扱い
- `risky` は補助属性であり phase ではない
- `risky` は start を止める label ではなく、auto-merge を止める label として扱う

### ローカル state

`.shinobi/state.json` の想定例:

```json
{
  "active_issue_number": 123,
  "active_pr_number": 456,
  "active_branch": "feature/issue-123-login-form",
  "phase": "review",
  "review_loop_count": 1,
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
- state は再開補助であり truth ではない
- GitHub 側と矛盾したら GitHub を優先する
- run の先頭で reconciliation してから active mission 判定を行う
- active mission には lease を持たせ、期限切れなら stale recovery 対象にする

### ラベル遷移ルール

各 phase で更新する label は次の通りです。

- start: `shinobi:working` を付与し、`shinobi:ready` を除去する
- publish: `shinobi:reviewing` を付与し、`shinobi:ready` と `shinobi:working` を除去する
- finalize merged: `shinobi:merged` を付与し、`shinobi:ready` `shinobi:working` `shinobi:reviewing` を除去する
- finalize blocked: `shinobi:blocked` を付与し、`shinobi:ready` `shinobi:working` `shinobi:reviewing` を除去する
- finalize needs-human: `shinobi:needs-human` を付与し、`shinobi:ready` `shinobi:working` `shinobi:reviewing` を除去する

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

- `config.yml` を読む
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
- Issue コメントのうち shinobi 関連ログ
- `.shinobi/summary.md`
- `.shinobi/decisions.md`
- 対象ファイル候補

初回 run では `init` が生成した空テンプレートを読む前提にします。欠損時は fatal にはせず、空ファイル相当として扱います。

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
- lease が有効で、対応する PR または branch が存在する場合は active mission とみなし、新規 run は停止する
- lease が期限切れでも、対応する PR または branch から state を再構築できる場合は、その mission を resume する
- lease が期限切れで、PR / branch / 最新の Shinobi コメントからも再開情報を復元できない場合は、active label を除去して `shinobi:needs-human` を付ける
- stale recovery を行ったときは Issue に recovery comment を残す

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

`config.yml` 例:

```yaml
repo: owner/repo
main_branch: main
ready_label: shinobi:ready
working_label: shinobi:working
reviewing_label: shinobi:reviewing
blocked_label: shinobi:blocked
needs_human_label: shinobi:needs-human
merged_label: shinobi:merged
risky_label: shinobi:risky
mission_lease_minutes: 30
max_review_loops: 3
max_commits_per_issue: 8
max_changed_files: 20
max_lines_changed: 800
max_runtime_minutes: 30
max_token_budget: 40000
auto_merge: true
use_draft_pr: true
merge_method: squash
high_risk_paths:
  - migrations/
  - infra/
  - auth/
  - billing/
```

方針:

- MVP では 1 repo 専用 config を前提にする
- 環境変数は token や secret のみを扱う
- `docs/product-spec.md` の flat schema を truth として維持する
- label 名や上限値は config で変えられる

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
- review loop 上限超過時の停止
- GitHub state とローカル state の不整合検出

### 手動確認

- `init` 後に設定が生成される
- `status` が空 state を扱える
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
