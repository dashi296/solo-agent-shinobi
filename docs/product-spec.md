# Product Spec

## プロダクト要約

`solo-agent-shinobi` は、GitHub Issue を起点に単一の AI agent が 1 ミッションずつ実装を進める開発自動化ツールです。

基本原則:

- 1 Issue = 1 Mission = 1 Pull Request
- GitHub が主要な source of truth
- 読むコンテキストは最小限
- 止まれることを優先する

## 想定ワークフロー

1. `shinobi:ready` の Issue を 1 件選ぶ
2. `shinobi:working` に更新して開始コメントを投稿する
3. `feature/issue-123-short-slug` 形式の branch を切る
4. Issue 本文の範囲内で実装する
5. PR を作成または更新する
6. diff と CI を見て self-review を行う
7. 規定回数内で修正を繰り返す
8. マージ可能ならマージし、危険なら停止する
9. Issue をクローズして結果を報告する

## GitHub を使った状態管理

GitHub を主要な状態管理として扱います。

- Issue: タスク
- Labels: 状態
- PR: 成果物
- Comments: ログと報告
- Branch protection / required checks: 安全装置

ローカル state は補助情報であり、GitHub より強い truth にしません。

## 推奨ラベル

- `shinobi:ready` - 実行待ち
- `shinobi:working` - 着手中
- `shinobi:reviewing` - PR 作成済み、修正ループ中
- `shinobi:blocked` - 外部要因で停止
- `shinobi:needs-human` - 人間判断が必要
- `shinobi:merged` - マージ済み
- `shinobi:risky` - 自動マージ対象外

追加例:

- `priority:high`
- `priority:medium`
- `priority:low`
- `area:frontend`
- `area:backend`
- `area:docs`

## Issue テンプレート指針

Issue は小さく、完了条件が明確であるほど相性が良くなります。

```md
# [TASK] ログイン画面を追加する

## 背景
ユーザがメールアドレスとパスワードでログインできる必要がある。

## 要件
- email/password フォームを実装する
- バリデーションを入れる
- エラーメッセージを表示する
- 既存の auth client を使う

## 完了条件
- ログイン処理が動作する
- lint/typecheck/test が通る
- 必要に応じてテストを追加する

## 注意点
- UI は既存デザインに合わせる
- 新しい依存追加は避ける
```

## CLI 設計

MVP の公開サブコマンド:

```bash
shinobi init
shinobi status
shinobi run
shinobi run --issue 123
```

将来コマンド候補:

```bash
shinobi plan --issue 123
shinobi execute --issue 123
shinobi review --issue 123
shinobi merge --issue 123
shinobi watch
```

### `shinobi init`

- `.shinobi/` を作成する
- 初期 config を作成する
- `.shinobi/summary.md` と `.shinobi/decisions.md` の空テンプレートを作成する
- GitHub ラベルの推奨セットを案内する

### `shinobi run`

1 ミッション分の一連処理を実行します。

- Issue 選択
- 着手
- 実装
- PR 作成 / 更新
- review loop
- merge 判定

`--issue <id>` を指定した場合は、その Issue を最優先で扱います。対象 Issue 自身の active mission は resume してよいですが、別 Issue の active mission や、Shinobi 自身が retryable と記録した local-only mission が残っている場合は横取りせず停止します。

### `shinobi status`

現在の state と対象 Issue / PR の状況を表示します。
GitHub との照合に失敗した場合でも、ローカル state があれば warning 付きで表示を継続します。

### `shinobi watch` (Future)

MVP では未実装です。一定間隔またはイベント駆動で次の実行機会を待つ将来コマンド候補として扱います。

## 設定例

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
mission_heartbeat_interval_minutes: 5
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

## 停止条件

推奨設定例:

```yaml
max_review_loops: 3
max_commits_per_issue: 8
max_changed_files: 20
max_lines_changed: 800
max_runtime_minutes: 30
max_token_budget: 40000
```

条件に達した場合の標準挙動:

1. 状況を記録する
2. PR / Issue に報告する
3. `shinobi:needs-human` または `shinobi:blocked` にする
4. 危険な継続実行を止める

## 自動マージポリシー

### 自動マージ候補

- docs 変更
- lint / formatting 修正
- 型修正
- 小さなバグ修正
- テスト追加
- 局所的で明確な UI 修正

### 自動マージ非推奨

- DB migration
- 認証 / 権限
- 課金
- セキュリティ関連
- 大規模依存更新
- 複数モジュールにまたがる大規模リファクタ

迷ったら自動マージしません。

high-risk path は context で候補抽出し、execute 完了前に publish 可否を最終判定します。publish 前に確定した場合でも、human handoff に必要な差分があるなら branch を push し、原則 draft PR を作成または更新してから `shinobi:needs-human` か `shinobi:blocked` へ遷移します。差分が無いか共有価値が無い場合だけ PR を作らず停止します。publish 後に review で追加検知した場合は PR を残したまま `shinobi:needs-human` へ遷移します。

## Interrupted Run Recovery

MVP では interrupted run からの回復を手動 cleanup 前提にしません。

- `shinobi:working` または `shinobi:reviewing` が残っている場合、tool は lease と PR / branch の生存確認で stale 判定する
- GitHub 上に active label が無くても、`start` 未完了の local-only mission が branch と state に残り、かつ Shinobi 自身が retryable と記録した場合に限って resume 可否を先に判定する
- local-only mission を resume してよいのは、state に保存した `run_id`, `issue`, `branch`, `phase` が branch 実体と整合し、`retryable_local_only: true` が残っている場合に限る
- lease は execute 中に `mission_heartbeat_interval_minutes` ごとに定期更新し、加えて phase 遷移、retry、CI polling のたびに heartbeat 更新する
- `--issue <id>` 指定時は、その Issue 自身の active mission だけ resume 対象にする
- `--issue <id>` の対象外に active mission や retryable な local-only mission がある場合は、別 mission の横取りを避けるため停止する
- 通常 run では stale でない active mission が 1 件だけある場合に限って、その same mission を resume する
- stale な mission を自動 resume してよいのは、machine-readable な Shinobi コメントと local / PR metadata から `run_id`, `issue`, `branch`, `phase`, `pr` を整合付きで復元できる場合に限る
- stale で、かつ上記の再開情報を復元できなければ、PR / branch が残っていても active label を外して `shinobi:needs-human` に遷移する
- recovery や cleanup を行った場合は Issue にコメントを残す

## 典型的な状態遷移

通常:

```text
shinobi:ready
  -> shinobi:working
  -> shinobi:reviewing
  -> shinobi:merged
```

停止:

```text
shinobi:working
  -> shinobi:blocked
or
  -> shinobi:needs-human

shinobi:reviewing
  -> shinobi:blocked
or
  -> shinobi:needs-human
```

補助ルール:

- `shinobi:ready` `shinobi:working` `shinobi:reviewing` は相互排他的に扱う
- `shinobi:working` を付けるときは `shinobi:ready` を外す
- `shinobi:reviewing` を付けるときは `shinobi:ready` と `shinobi:working` を外す
- 終端ラベルを付けるときは `shinobi:ready` `shinobi:working` `shinobi:reviewing` を外す
- `shinobi:risky` は補助ラベルなので自動では外さない

## コメントと PR テンプレート例

開始時:

```md
<!-- shinobi:mission-state -->
Shinobi Start

任務 #123 に着手します。
- issue: 123
- branch: feature/issue-123-login-form
- phase: start
- pr: null
- lease_expires_at: 2026-04-09T10:30:00+09:00
- run_id: 20260409T100000-issue-123
- scope: issue body の要件内に限定
```

開始・recovery 用の Shinobi コメントは、自由文だけではなく machine-readable な marker と固定 key を含めます。最低キーは `issue`, `branch`, `phase`, `lease_expires_at`, `pr`, `run_id` です。

同じ mission では、この comment を publish / review / recovery のたびに upsert し、`phase` `pr` `lease_expires_at` を最新値へ更新します。stale recovery は最新の machine-readable comment と local / PR metadata が整合する場合だけ行います。

レビュー中:

```md
Shinobi Report

現在の対応内容:
- login form を実装
- auth client 連携を追加
- lint エラーを修正

残り:
- typecheck failure の修正
- テストの追加
```

完了時:

```md
Shinobi Complete

PR #456 をマージし、この任務を完了しました。
主な変更:
- ログインフォーム追加
- バリデーション実装
- エラーハンドリング追加
```
