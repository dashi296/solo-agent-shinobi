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

加えて、`.shinobi/` 配下には次の 2 種類のローカル情報があります。

- `state`: 実行中 mission の復元、ownership、phase、cleanup 判定に使う機械状態
- `knowledge`: 次回以降にも再利用できる再発防止ルール

`knowledge` は task 固有ログを保存する場所ではありません。生の review コメントや議論は GitHub に残し、`.shinobi/` 側には汎用化したルールだけを残します。`.shinobi/` 配下の knowledge / template ファイルはローカル生成物として扱い、追跡対象の配布元から `shinobi init` が workspace へコピーします。

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

Issue は小さく、完了条件が明確で、解釈幅が小さいほど相性が良くなります。

最低でも次の情報を持たせます。

- `背景`: なぜ今やるのか
- `目的`: この Issue で達成したい変化を 1 文で書く
- `対象`: 触ってよいファイル、モジュール、CLI、状態
- `要件`: 実装すべき項目
- `完了条件`: レビュー時に yes/no で判定できる確認項目
- `スコープ外`: 今回やらないこと
- `注意点`: リスク、順序制約、依存関係

書き方の基準:

- `目的` は 1 Issue で 1 つの変化に絞る
- `対象` は `必要に応じて` だけで広げすぎない
- `完了条件` には観測可能な結果を書く
- `スコープ外` で隣接タスクを切り離す
- `注意点` には安全条件や依存 Issue を書く

避けたい書き方:

- `いい感じにする`
- `最小限で対応する` だけで具体条件がない
- `必要なら広く直す`
- `動くようにする`
- `テストも必要に応じて`

代わりに、次のように具体化します。

- `shinobi run が select の次に start まで進む`
- `branch / local state / GitHub label が整合する`
- `tests/unit/test_executor.py に失敗ケースを 1 件追加する`
- `PR 作成と review loop は本 Issue では扱わない`

```md
# [TASK] ログイン画面を追加する

## 背景
ユーザがメールアドレスとパスワードでログインできる必要がある。

## 目的
既存 auth client を使った email/password ログインを、既存 UI 導線の中で完了できるようにする。

## 対象
- `src/ui/login_page.tsx`
- `src/auth/client.ts` の既存呼び出し
- `tests/ui/test_login_page.tsx`

## 要件
- email/password フォームを実装する
- バリデーションを入れる
- エラーメッセージを表示する
- 既存の auth client を使う

## 完了条件
- 未入力時に送信できない
- 認証失敗時に既定のエラーメッセージが表示される
- 認証成功時に dashboard へ遷移する
- 対象テストが追加または更新される
- lint/typecheck/test が通る

## スコープ外
- SSO ログイン
- パスワード再設定
- auth client 自体の仕様変更

## 注意点
- UI は既存デザインに合わせる
- 新しい依存追加は避ける
```

レビューで揉めにくくする追加ルール:

1. `目的` と `完了条件` の間に飛躍を作らない
2. `対象` にない大きな編集が必要なら follow-up Issue に分ける
3. `スコープ外` に書いたものを PR で混ぜない
4. `完了条件` には挙動、整合性、検証の 3 種類を最低 1 つずつ含める
5. 依存 Issue がある場合は本文で番号を明記する

## レビュー効率化の運用

同じ種類の指摘を繰り返さないため、review の結果は会話だけで終わらせず運用知識に変換します。

- `.shinobi/review-notes.md` を review knowledge の蓄積先とする
- 記録するのは指摘本文そのものではなく、再発防止ルール、trigger、確認観点とする
- 新しい Issue の着手前に notes 全文を読むのではなく、関連カテゴリだけを確認する
- PR 前セルフレビューには `.shinobi/templates/self-review.md` を使う
- review 指摘を rule 化する場合は `.shinobi/templates/review-note-rule.md` を使う
- 同じ指摘が 2 回以上出たら、notes だけで済ませず次の優先順位で仕組みに昇格する
  1. test: 挙動の再発を検出できる場合
  2. helper: 定型判断や cleanup を共通化できる場合
  3. template: Issue / PR / review 記述の不足が原因の場合
  4. lint: 静的に検出可能な場合

推奨する `review-notes.md` のカテゴリ:

- `state-transition`
- `cleanup-recovery`
- `test-coverage`
- `scope-control`
- `docs-consistency`

推奨する `review-notes.md` の最小フォーマット例:

```md
# Review Notes

## state-transition
- rule: start 前に lock owner と issue ownership の一致を確認する
  trigger: stale mission recovery
  check: pre-start
  promote_if_repeated: helper

## cleanup-recovery
- rule: failure path でも state, lock, label cleanup を確認する
  trigger: phase failure
  check: pre-pr
  promote_if_repeated: test

## test-coverage

## scope-control

## docs-consistency
```

## CLI 設計

MVP の公開サブコマンド:

```bash
shinobi init
shinobi status
shinobi run
shinobi run --issue 123
shinobi review
```

将来コマンド候補:

```bash
shinobi plan --issue 123
shinobi execute --issue 123
shinobi merge --issue 123
shinobi watch
```

### `shinobi init`

- `.shinobi/` を作成する
- 初期 config を作成する
- workspace / installation ごとに一意な `agent_identity` を生成して `.shinobi/config.json` に書き込む
- `.shinobi/summary.md` と `.shinobi/decisions.md` の空テンプレートを作成する
- `.shinobi/run.lock` を初期化可能な状態にする
- `.shinobi/review-notes.md` の初期テンプレートを作成する
- `.shinobi/templates/self-review.md` を作成する
- `.shinobi/templates/review-note-rule.md` を作成する
- GitHub ラベルの推奨セットを案内する

既存の `review-notes.md` や `templates/` 配下ファイルがある場合、`shinobi init` は原則として上書きしません。
テンプレート本体は追跡対象の配布元に置き、`init` はそれらを workspace の `.shinobi/` 配下へコピーします。

### `shinobi run`

1 ミッション分の一連処理を実行します。

- Issue 選択
- 着手
- 実装
- PR 作成 / 更新
- review loop
- merge 判定

実装前には `.shinobi/review-notes.md` 全体を読むのではなく、今回の task に関連するカテゴリだけを確認します。`shinobi run` の context phase では、その選定結果を local state の構造化 `mission_context` として保持します。関連カテゴリが不明確な場合でも、確認対象は最大 2 カテゴリまでに抑えます。

PR 前セルフレビューでは `.shinobi/templates/self-review.md` を使います。review で新しい指摘を受けた場合は、必要に応じて `.shinobi/templates/review-note-rule.md` に沿って `.shinobi/review-notes.md` へ再発防止ルールを追記します。

`--issue <id>` を指定した場合は、その Issue を最優先で扱います。resume を許可するのは、その Issue 自身の stale mission、または branch 実体と retryable 記録で裏付けられたその Issue 自身の local-only mission に限ります。lease が有効な live mission には別プロセスから attach しません。`.shinobi/run.lock` の owner でない run は live mission の継続に参加せず停止します。別 Issue の active mission や、別 Issue に属する retryable local-only mission が残っている場合は横取りせず停止します。

実装順序としては、run 開始時にまず `.shinobi/run.lock` を確認します。他 owner の stale でない lock が見つかった場合は、その workspace で live run が進行中とみなして停止します。stale lock を見つけた場合は、run は select phase 内でその lock を明示的に takeover してから recovery / cleanup を行えます。lock が存在しない場合は、その run 自身が select phase で live run 用の lock を取得してから stale mission の recovery / cleanup を進めます。`start` では、同じ lock ownership を維持したまま branch 作成と state 更新へ進みます。

### execute phase の検証コマンド契約

MVP の executor は AI によるコード編集をまだ行わず、検証コマンドを実行して構造化結果を返します。

既定の検証コマンド:

- `lint`: 未定義
- `typecheck`: `env PYTHONPYCACHEPREFIX=/tmp/pycache python3 -m compileall src tests`
- `test`: `python3 -m unittest tests.test_cli`

`lint` は、この repo に専用 linter がまだ導入されていないため未定義です。未定義コマンドは成功扱いにせず、`not_configured` の結果と `verification command <name> is not configured` というメッセージを返します。コマンドが終了コード 0 なら `passed`、0 以外なら `failed`、起動自体に失敗した場合は `error` として扱います。

将来 repo ごとの差異に対応できるよう、検証コマンドは config の `verification_commands` で上書き可能な形にします。

### `shinobi status`

現在の state と対象 Issue / PR の状況を表示します。
GitHub との照合に失敗した場合でも、ローカル state があれば warning 付きで表示を継続します。

### `shinobi watch` (Future)

MVP では未実装です。一定間隔またはイベント駆動で次の実行機会を待つ将来コマンド候補として扱います。

## 設定例

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
  "verification_commands": {
    "lint": [],
    "typecheck": [
      "env",
      "PYTHONPYCACHEPREFIX=/tmp/pycache",
      "python3",
      "-m",
      "compileall",
      "src",
      "tests"
    ],
    "test": ["python3", "-m", "unittest", "tests.test_cli"]
  },
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

high-risk path は context で候補抽出し、execute 完了後の pre-publish stop で publish 可否を最終判定します。現行実装では publish 前に high-risk path が確定した時点で push / PR 作成前に停止し、start-phase mission を `shinobi:needs-human` へ handoff します。publish 後に review で追加検知した場合は PR を残したまま `shinobi:needs-human` へ遷移します。差分共有のために PR を残したまま handoff する挙動は将来拡張として扱います。

publish 直前に現在の Issue がすでに `shinobi:blocked` または `shinobi:needs-human` を持つ場合は、人手の停止判断を優先し、push / PR 作成前に publish を中止します。

## Interrupted Run Recovery

MVP では interrupted run からの回復を手動 cleanup 前提にしません。

- `shinobi:working` または `shinobi:reviewing` が残っている場合、tool は lease と PR / branch の生存確認で stale 判定する
- 同一 workspace の同時実行は `.shinobi/run.lock` で防ぎ、lock owner でない run は同じ `agent_identity` の mission でも resume しない
- `.shinobi/run.lock` は `heartbeat_at + mission_lease_minutes` を超えたら stale lock とみなし、次の run は recovery に入る前にその lock を current `run_id` で原子的に takeover してから解放または上書きできる
- GitHub 上に active label が無くても、`start` 未完了の local-only mission の branch が残り、state または local log に Shinobi 自身の retryable 記録がある場合に限って resume 可否を先に判定する
- local-only mission を resume してよいのは、state または local log から復元した `agent_identity`, `run_id`, `issue_number`, `branch`, `phase` が branch 実体と整合し、かつその `agent_identity` が現在設定の一意な `agent_identity` と一致し、`retryable_local_only` 相当の記録と branch 作成後の retryable な `start` 失敗記録で裏付けられる場合に限る
- lease は execute 中に `mission_heartbeat_interval_minutes` ごとに定期更新し、加えて phase 遷移、retry、CI polling のたびに heartbeat 更新する
- `--issue <id>` 指定時は、その Issue 自身の stale mission だけを resume 対象にする
- `--issue <id>` の対象外に active mission や retryable な local-only mission がある場合は、それが stale であっても別 mission の横取りや cleanup を避けるため停止する
- 通常 run では stale でない active mission は live mission として扱い、resume ではなく停止要因にする
- stale な mission を自動 resume してよいのは、machine-readable な Shinobi コメントと local / PR metadata から `agent_identity`, `run_id`, `issue_number`, `branch`, `phase`, `pr_number` を整合付きで復元でき、現在設定の一意な `agent_identity` と一致する場合に限る。publish 前の mission では `pr: null` を許容するが、その場合は branch 実体と pre-publish phase が整合している必要がある
- つまり interrupted run recovery は stale recovery を指し、lease が有効な live mission に新しい run が合流することは許可しない
- stale で、かつ上記の再開情報を復元できなければ、PR / branch が残っていても active label を外して `shinobi:needs-human` に遷移する
- `agent_identity` が欠損または不一致の active mission は自分の mission とみなさず、自動 resume は行わない。lease が有効な live mission なら GitHub 上の label / comment も変更せず停止する。lease が切れた stale mission なら、他 owner の live lock が存在しないことを確認できた run に限って `shinobi:needs-human` への cleanup だけを許可し、ownership 不一致で resume しなかった理由を Issue に残す
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

shinobi:blocked
  -> shinobi:ready
or
shinobi:needs-human
  -> shinobi:ready
```

補助ルール:

- `shinobi:ready` `shinobi:working` `shinobi:reviewing` は相互排他的に扱う
- `shinobi:risky` を除く状態ラベルは常に 1 つだけ残るよう正規化する
- `shinobi:working` を付けるときは `shinobi:risky` を除く他の状態ラベルを外す
- `shinobi:reviewing` を付けるときは `shinobi:risky` を除く他の状態ラベルを外す
- 終端ラベルを付けるときは `shinobi:risky` を除く他の状態ラベルを外す
- `shinobi:risky` は補助ラベルなので自動では外さない
- `shinobi:blocked` と `shinobi:needs-human` は open issue 上の停止ラベルであり、blocker や human action が解消したら人手で停止理由を確認し、停止ラベルを外して `shinobi:ready` に戻せる

## コメントと PR テンプレート例

開始時:

```md
<!-- shinobi:mission-state
issue: 123
branch: feature/issue-123-login-form
phase: start
pr: null
lease_expires_at: 2026-04-09T10:30:00+09:00
agent_identity: owner/repo#default@mbp14-7f3a2c
run_id: 20260409T100000-issue-123
-->
Shinobi Start

任務 #123 に着手します。
- scope: issue body の要件内に限定
```

開始・recovery 用の Shinobi コメントは、自由文に埋もれた箇条書きではなく、HTML comment marker の中に固定 schema の key-value block を置きます。最低キーは `issue`, `branch`, `phase`, `lease_expires_at`, `pr`, `agent_identity`, `run_id` です。自由文本文は人間向けでよいですが、recovery は marker 内の block だけを parse 対象にします。`agent_identity` は `init` が生成する workspace / installation ごとの一意 ID で、複数 runner 間で共有しません。

同じ mission では、この comment を publish / review / recovery のたびに upsert し、marker 内の `phase` `pr` `lease_expires_at` を最新値へ更新します。現行実装の high-risk path 停止は publish 前 handoff なので、machine-readable な mission-state comment は start phase のまま `pr: null` を維持します。stale recovery は最新の machine-readable block と local / PR metadata が整合し、`agent_identity` も現在設定の一意な `agent_identity` と一致する場合だけ行います。publish 前の mission では `pr: null` を許容しますが、その場合は branch 実体と pre-publish phase の整合確認を必須にします。

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
