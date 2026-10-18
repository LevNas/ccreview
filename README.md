# ccreview（v0.1、試作）

PR/MR やブランチを、マージの前にゲート判定するプラグインです。観点別のレビュー、宣言された command ゲート、上限付きの自動修正ループを Workflow で回し、合否とコメント案を返します。投稿とマージはしません。

設計判断の要約は [`docs/decisions.md`](docs/decisions.md) にあります。この README とコードの「決定 N」や「e2e D5」などの呼び名は、そこを指します。

## 導入

marketplace `levnas-plugins` から入れます。

```text
/plugin marketplace add LevNas/claudecode-plugins
/plugin install ccreview@levnas-plugins
```

入れたあと、セッションを始め直します（agent の型と hook は、セッションの開始時にしか読み込まれません）。

手元の clone を試すときは、レビューするリポジトリのルートで、プラグインを読み込んでセッションを始めます。

```sh
claude --plugin-dir <ccreview のパス>
```

## 宣言の書き方

レビューされるリポジトリには、次の 2 つが要ります。[`examples/`](examples/) を写して直すのが早道です。

| ファイル | 中身 |
|---|---|
| `REVIEW.md`（リポジトリのルート） | 重大度の定義、category の語彙、観点共通の決まり、観点ごとの本文の節（例: [`examples/REVIEW.md`](examples/REVIEW.md)） |
| `.claude/review-gates.yaml` | ゲートの宣言。観点のゲートは `ref` で `REVIEW.md` の節の見出しを指します（例とキーの説明: [`examples/review-gates.yaml`](examples/review-gates.yaml)） |

PR を `untrusted` と判定したときは、この 2 つを作業ツリーではなく base のブランチから読みます。PR がゲートを書き換えて弱めるのを防ぐためです。base に無ければ止まります。

## 使い方

セッションの中で `/ccreview:ccreview`（スキル）に、PR 番号かブランチ名を渡します。

修正 agent が `.claude/` の下のファイル（ナレッジベースの文書など）を直すときは、Claude Code の保護されたパスの確認が出ます（manual・acceptEdits のとき。auto では分類器が判断し、dontAsk ではその編集が拒否されて、残りの指摘だけが直されます）。settings の allow 規則では外せず、プラグインの hook が許しても外れませんでした（e2e D1）。確認は背景の修正 agent の編集について、ccreview を起動したセッションに出ます。利用者が承認するか、このセッションの間 `.claude` フォルダの編集を許すかを選ぶまで、run は止まります（決定 31）。

ほかのプラグインには依存しません。結果は合否とコメント案（リポジトリの外の一時ファイル）で返します。`fail` のときの次の一手も、同じファイルの投稿しない節に入ります。プランやタスクとして残すかどうかと置き場所は利用者が決めます（決定 29）。

## 構成

| パス | 役割 |
|---|---|
| `skills/ccreview/SKILL.md` | メイン側の手順（対象と信頼の判定、宣言の読み込み、許可リストの書き出し、workflow の起動、検算、報告とコメント案） |
| `workflows/ccreview-loop.js` | 判定と修正のループ（`ccreview:ccreview-loop`）。スキルと同じ名前にすると、スキルが workflow に隠れるため、名前を分けている |
| `agents/ccreview-reviewer.md` | 観点役・分類役・確認役。Read・Grep・Glob だけ（決定 16） |
| `agents/ccreview-fixer.md` | 修正 agent。Bash と編集は hook で絞られる（決定 21） |
| `hooks/gate.py` | 修正 agent の Bash / Edit / Write / NotebookEdit を、run ごとの許可リストで allow / deny に決める。ほかの agent とメインには何もしない（決定 21・24） |
| `scripts/write_allowlist.py` | 許可リストを worktree の git dir に書く。`ask` 規則に当たる command ゲートは保留にする（決定 23・25） |
| `scripts/verify_fixes.py` | 修正 agent の自己申告を git で検算する（決定 19） |
| `scripts/extract_result.py` | workflow の出力ファイルから結果を取り出し、リポジトリの外のファイルに書く。メインが読み方を組まずに済むようにする（決定 42） |
| `docs/decisions.md` | 設計判断（決定 1〜43）と、その根拠になった検証の記録の要約 |
| `examples/` | `REVIEW.md` と `.claude/review-gates.yaml` の例 |

## テスト

プラグインのルートで実行します。

```sh
node tests/loop-sim.mjs
python3 tests/gate_test.py
python3 tests/write_allowlist_test.py
python3 tests/verify_fixes_test.py
python3 tests/extract_result_test.py
```

## ライセンス

MIT です（`LICENSE`）。
