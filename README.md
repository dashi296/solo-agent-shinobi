# solo-agent-shinobi

GitHub Issues を任務として受け取り、単一の AI agent が実装・修正・レビュー・マージまでを進めるための開発自動化ツールです。

`solo-agent-shinobi` は、複数 agent の並列実行ではなく、**1 Issue = 1 Mission = 1 Pull Request** の原則で継続的に開発を進めます。

## コンセプト

- One mission at a time
- Minimal context
- Small, reviewable changes
- Safety over autonomy
- GitHub as source of truth

Shinobi は repo 全体を毎回読み込まず、対象 Issue と関連ファイル、最小限のローカル state を使って任務を完了させることを目指します。

## 何をするツールか

1. `shinobi:ready` の Issue を選ぶ
2. 作業開始状態に更新する
3. 専用ブランチを切る
4. 必要最小限のコンテキストで実装する
5. Pull Request を作成または更新する
6. CI / lint / typecheck / test を確認して修正する
7. 規定回数内で self-review を繰り返す
8. 条件を満たしたらマージする
9. Issue をクローズする

## 目標

- 単一 agent で継続的な開発を回せること
- GitHub Issues / PR / Labels を主要な状態管理に使えること
- 会話履歴ではなく構造化 state で継続実行できること
- 安全装置付きで自動修正・自動マージできること

## 非目標

- 複数 Issue の同時処理
- 複数 agent の並列オーケストレーション
- repo 全体を毎回読んで全体最適化すること
- 無制限の self-review ループ
- 高リスク変更の無条件自動マージ

## 想定ユースケース

- 小から中規模プロジェクトの継続保守
- 明確に分割された GitHub Issues を順番に消化する運用
- docs 修正、テスト追加、型修正、小さな機能追加、軽微なバグ修正

## 向いていないケース

- 巨大で仕様未確定なタスク
- DB migration や認証基盤変更などの高リスク変更を完全無人で回す運用
- 1 Issue 内でプロジェクト全体にまたがる大規模リファクタ

## ラベル設計

推奨ラベル:

- `shinobi:ready`
- `shinobi:working`
- `shinobi:reviewing`
- `shinobi:blocked`
- `shinobi:needs-human`
- `shinobi:merged`
- `shinobi:risky`

## 想定 CLI

```bash
shinobi init
shinobi status
shinobi run
shinobi run --issue 123
shinobi watch
```

## ドキュメント構成

- [docs/product-spec.md](/Users/shunokada/projects/solo-agent-shinobi/docs/product-spec.md): プロダクト仕様、ワークフロー、状態遷移、CLI と設定の設計
- [docs/architecture.md](/Users/shunokada/projects/solo-agent-shinobi/docs/architecture.md): 内部アーキテクチャ、モジュール責務、ローカル state、テスト方針
- [CLAUDE.md](/Users/shunokada/projects/solo-agent-shinobi/CLAUDE.md): このリポジトリで作業する agent 向けの行動規約

## 現在の状態

このリポジトリは設計段階です。実装前に、Issue 駆動の運用モデル、停止条件、マージポリシー、責務分離を先に固めます。
