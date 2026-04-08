# Architecture

## 目的

この文書は、`solo-agent-shinobi` の内部構造と実装責務を定義します。

`README.md` は概要、`docs/product-spec.md` は外部仕様、ここでは内部設計を扱います。

## システム概要

```text
GitHub Issues / PRs
  └─ source of truth

solo-agent-shinobi
  ├─ dispatcher   # 次の Issue を選ぶ
  ├─ worker       # 実装と修正
  ├─ reviewer     # PR / CI / diff を確認
  ├─ merger       # マージ条件を判定
  └─ state store  # ローカル最小 state

GitHub Actions
  └─ lint / typecheck / test / build
```

## ローカル state

使用予定のローカル領域:

```text
.shinobi/
  state.json
  summary.md
  decisions.md
  run.lock
  logs/
  cache/
```

### `.shinobi/state.json`

- 現在の issue 番号
- 現在の PR 番号
- 現在の branch 名
- 現在の agent_identity
- 現在の run_id
- 現在の phase
- review loop 回数
- local-only mission を retry 可能とみなすかどうか
- active mission の lease 期限
- 最終実行結果
- 最終エラー
- 直近の完了または停止 mission の要約

### `.shinobi/run.lock`

- 同一 workspace で live run を 1 つに制限するローカル排他ファイル
- `agent_identity`, `run_id`, `pid`, `started_at`, `heartbeat_at` を保持する
- `heartbeat_at + mission_lease_minutes` を過ぎた lock は stale とみなし、次の run が recovery 開始前に解放または上書きできる
- stale でない live mission への二重 attach を防ぐ

### `.shinobi/summary.md`

- プロジェクト全体の短い圧縮サマリ
- 最近の重要な設計判断
- 次回以降の実行に必要な最低限の前提

### `.shinobi/decisions.md`

- 継続的に参照すべき設計判断
- やってはいけないこと
- 合意済みの実装方針

長大な思考ログを保存する設計にはしません。

## 推奨モジュール構成

```text
src/shinobi/
  cli.py
  config.py
  models.py
  github_client.py
  issue_selector.py
  context_builder.py
  executor.py
  reviewer.py
  merger.py
  state_store.py
```

### 役割

- `cli.py`: コマンド入口
- `config.py`: 設定読み込み
- `models.py`: ドメインモデル
- `github_client.py`: GitHub API 操作
- `issue_selector.py`: 次 Issue 選択
- `context_builder.py`: 最小コンテキスト生成
- `executor.py`: 実装フェーズ
- `reviewer.py`: review / retry 判定
- `merger.py`: マージ可否判定
- `state_store.py`: ローカル state 管理

## 実装優先順位

### Phase 1: Foundations

- config
- CLI
- GitHub client
- state store
- domain models

### Phase 2: Mission lifecycle

- issue selection
- start working
- branch creation
- PR creation

### Phase 3: Review loop

- CI status retrieval
- diff review
- retry / refactor loop
- stop conditions

### Phase 4: Merge control

- auto-merge eligibility
- risky issue detection
- issue close flow

### Phase 5: Ergonomics

- watch mode
- slash commands
- metrics
- better reporting

基盤を飛ばして複雑な機能に進まないことを前提にします。

## Prompt / Agent 設計原則

将来 `solo-agent-shinobi` が AI agent を呼び出す際の基本方針:

- 対象 Issue に集中させる
- 読むファイルを限定する
- スコープ拡大を防ぐ
- 結果を短く要約させる
- follow-up issue を使わせる
- 不明点がある場合は保守的に振る舞わせる
- ユーザとの対話は簡潔で落ち着いた忍者風の口調にする

基準プロンプト:

```text
You are Shinobi.
You execute one mission at a time.
Read only what you need.
Prefer small safe changes.
If scope grows, split it.
If risk rises, stop and report.
Use a concise and calm ninja-like tone.
```

## テスト方針

### 単体テスト

- 設定読み込み
- ラベル遷移
- issue 選択ロジック
- 停止条件判定
- 自動マージ可否判定

### 結合テスト

- Issue から PR 作成までのフロー
- review loop の遷移
- 失敗時の `needs-human` 化
- `--issue` 指定時に別 mission を横取りしないこと
- `agent_identity` 不一致の stale mission を resume しないこと
- 状態 label 正規化で `merged` と `needs-human` などが同居しないこと

### 手動確認

- `shinobi run --issue <id>` の基本動作
- state の更新
- コメント / ラベル操作の整合性

## 依存コンポーネント候補

- Python 3.11+
- Git
- GitHub CLI (`gh`) または GitHub API client
- GitHub Actions
- AI coding agent 実行環境

候補ライブラリ:

- `typer` または `click`
- `pydantic`
- `PyGithub` または `httpx`
- `GitPython` または subprocess
- `rich`
- `PyYAML`
