---
name: ccreview
description: PR/MR やブランチをマージ前にゲート判定するときに使う。REVIEW.md と .claude/review-gates.yaml を読み、ccreview workflow で判定・自動修正のループを回し、合否とコメント案を返す。投稿とマージはしない。
license: MIT
allowed-tools: Read, Grep, Glob, Bash, Write, Workflow
---

# ccreview — マージ時ゲート（試作）

設計判断の要約は `${CLAUDE_PLUGIN_ROOT}/docs/decisions.md` にあります。この手順の「決定 N」や「e2e D5」などの呼び名は、そこを指します。手順を進めるために読む必要はありません。呼び名の理由を確かめたいときだけ、その項を読みます。
このスキルはメイン側の手順だけを持ち、判定と修正のループはプラグインの workflow `ccreview:ccreview-loop`（`${CLAUDE_PLUGIN_ROOT}/workflows/ccreview-loop.js`）が回します。スキル（`ccreview:ccreview`）と workflow は同じ名前空間に入るので、名前を分けています（同じ名前にすると、スキルが workflow に隠れます）。
修正 agent（型 `ccreview:ccreview-fixer`）の Bash と編集は、プラグインの PreToolUse hook（`${CLAUDE_PLUGIN_ROOT}/hooks/gate.py`）が run ごとの許可リストに絞ります（決定 21・24）。

## やらないこと

- MR/PR への投稿は、利用者の明示の指示があるときだけ行います。`/code-review --comment` は使いません。
  - 投稿には gh か glab を使います。読み込まれている指示（CLAUDE.md など）が glab の代わりのラッパーを決めていれば、そちらを使います。
  - 認証が切れていたら、自分で認証を試みず、利用者に知らせて待ちます。
- push とマージはしません。最終承認は人です。
- main には触りません。修正コミットは、対象ブランチを checkout した分離 worktree にだけ積みます。
- メインが書き出すファイルは、手順で決めたもの（手順 1 の `<diffDir>`、手順 3 の許可リスト、リポジトリの外の一時ファイルに書く workflow の結果とコメント案）だけです。手順 1 の checkout・worktree の作成と、git の操作は別です。worktree を抜けた先の共有の checkout には書きません（決定 29。e2e D2 では、訂正プランをそこへ `cp` しようとしました。`cp` は git ではないので、worktree の関門に掛かりません）。
- 結果を、頼まれずにプランやタスクとして保存しません。残すかどうかと置き場所（issue tracker、タスク管理の仕組みなど）は利用者が決め、頼まれたときは指示された場所に書きます。

## 手順

1. **対象を決める**
   - 作業は分離 worktree で行います（EnterWorktree。すでに worktree 内ならそのまま）。
   - PR 番号なら `gh pr view <n> --json headRefName,baseRefName` でブランチを調べ、`gh pr checkout <n>` で取り出します。ブランチ名ならそのまま checkout します。
   - `git fetch origin` のあと、`git merge-base origin/<base> HEAD`（既定の base は `main`）で起点の SHA を求め、range を `<起点 SHA>..HEAD` にします。ローカルの `main` は使いません（古いと、関係のない変更が範囲に混ざるため。決定 14）。
   - 変更ファイルは `git -c core.quotepath=off diff --name-only <range>` で取ります。`core.quotepath=off` が無いと、日本語などの名前が `"\343..."` の形で返り、ゲートの `paths` にも当たらなくなるためです。
   - 差分の本文は、git にファイルへ書かせます。`git rev-parse --show-toplevel` の結果に `/.ccreview` を付けた絶対パスを `<diffDir>` とし、以後の手順はすべてこの文字列を使います（agent の cwd がどこでも同じファイルを指すように）。`mkdir <diffDir>` を実行し、続けて `git diff <range> --output=<diffDir>/diff-r1.patch` を実行します（git は書き先のフォルダを作りません）。観点ごとの agent は git を実行できないので、このファイルを Read で読みます（決定 16・26）。
   - `mkdir` に `-p` を付けません。`<diffDir>` がすでにあれば `mkdir` が失敗するので、そこで止めて利用者に伝えます。前の run の残りなら `write_allowlist.py --remove --diff-dir <diffDir>` で消してからやり直します。変更そのものが `.ccreview` を含むときは、信頼の判定によらず止めます。変更に `.ccreview/diff-r1.patch` という名前のシンボリックリンクがあると、`--output` がリンク先（worktree の外でも）を上書きするためです。
   - 差分の本文を args に書き写しません。モデルが書き写すと壊れることがあるためです（e2e セッション B では、修正 agent が書き写した差分が二重のエスケープで壊れ、2・3 ラウンド目の観点役が壊れた差分を読みました）。`<diffDir>` は未追跡のまま置き、手順 6 で消します。
   - **信頼を判定します（決定 18）。** `trusted` か `untrusted` のどちらかで、既定はありません。
     - 利用者が明示したら、それに従います。
     - 明示が無ければ、PR 番号のときは `gh pr view <n> --json author --jq .author.login` が `gh api user --jq .login` と同じときだけ `trusted` です。利用者が自分でローカルのブランチ名を指定したときも `trusted` です。
     - それ以外（他人の PR、fork、作者を確かめられない MR）は `untrusted` です。
     - 判定と根拠を、最初に利用者へ示します。
2. **宣言を読む**
   - `untrusted` のときは、`.claude/review-gates.yaml` と `REVIEW.md` を作業ツリーからではなく `git show origin/<base>:<path>` で base 側から読みます。PR がゲートや観点を書き換えて弱めるのを防ぐためです。base に無ければ、そこで止めて利用者に伝えます。
   - `.claude/review-gates.yaml` を読みます。`paths` を持つゲートは、変更ファイルがその glob に当たるときだけ残します。
   - command ゲートの `run` の `{files}` を、当たった変更ファイル（空白区切り）に置き換えます。
   - 展開の前の `run` も、`template` として控えておきます（手順 3 の許可リストで、ゲートのスクリプトを保護するのに使います）。
   - `REVIEW.md` から「重大度の定義」「category の語彙」「観点共通の決まり」の節の本文と、語彙の一覧（`` ` `` で囲んだ語）を取り出します。perspective ゲートには `ref` の節の本文を `sectionText` として付けます。`passes` は yaml の値をそのまま渡します（省略時は 1）。
   - **command ゲートはここで実行します**（事前チェック）。リポジトリのルートで `run` を実行し、`{id, exitCode, outputTail（末尾 40 行ほど）, failures}` を集めます。`failures` は出力から読み取れるファイルごとの失敗（path・line・summary）で、読み取れなければ空にします。workflow の中で command のために agent を起動すると、1 体あたり約 20k 以上のトークンがかかるためです。
   - `untrusted` のときは、command ゲートを実行しません。コマンドもそれが呼ぶスクリプトも PR の中身なので、実行すると PR のコードを動かすことになるためです。`commandResults` は空にします。
3. **修正 agent の許可リストを書く**（`trusted` のときだけ。`untrusted` では修正 agent が動かないので書きません）
   - `python3 ${CLAUDE_PLUGIN_ROOT}/scripts/write_allowlist.py --range <range> --max-rounds <maxRounds> --diff-dir <diffDir> --gates '<json>'` を実行します。`--gates` は command ゲートの `[{id, run（展開済み）, template（展開前）, blocking}]` です。
   - 許可リストは worktree の git dir（`<git rev-parse --absolute-git-dir>/ccreview/allowlist.json`）に書かれます。作業ツリーの外なので、修正 agent は書き換えられません。中身は、range の変更ファイル、読み取り専用の git コマンド、`git diff <range>`、次のラウンドの差分を書く `git diff <range> --output=<diffDir>/diff-r<N>.patch`（N は 2 から maxRounds まで。最終ラウンドは修正しないので、それより先は要りません）、ラウンドごとのコミットメッセージ、実行を認めた command ゲート、ゲートのスクリプト（保護するファイル）、期限（6 時間）です。
   - **決定 23**: スクリプトは、command ゲートを有効な settings（managed・user・project・local）の `ask` / `deny` 規則と照らします。当たるもの、またはシェルのメタ文字（`&&`・`|`・リダイレクトなど）を含むものは許可リストに入れず、出力の `deferred` に理由付きで返します。背景の修正 agent が `ask` 規則に当たると、人が来るまで止まるためです（hook の `allow` は `ask` 規則に負けます）。
   - 出力の `deferred` を、そのまま `args.deferredGates` に渡します。`settingsErrors` があれば（settings が読めなければ）、すべてのゲートが保留になります。利用者に伝えます。
   - **決定 31**: range に `.claude/` の下のファイル（`.claude/worktrees/` を除く）があれば、起動の前に利用者に伝えます。修正 agent がそれを編集すると、Claude Code の保護されたパスの確認を通ります。settings の allow 規則では外せず（公式の資料）、hook の `allow` でも外れませんでした（e2e D1。資料には書かれていません）。manual と acceptEdits では、背景の修正 agent の編集について確認が出て、利用者が答えるまで run が止まります。auto では分類器が判断します。dontAsk ではその編集が拒否され、修正 agent はその指摘を飛ばして残りだけを直してコミットします（全部拒否されればコミットが無く、`fail` で止まります）。bypassPermissions では通ります。
4. **workflow を起動する**
   - 起動の直前に `git rev-parse HEAD` を記録します（手順 5 の検算で使います）。
   - `Workflow({name: "ccreview:ccreview-loop", args})`。`trusted` で名前が見つからないときだけ、`Workflow({scriptPath: "${CLAUDE_PLUGIN_ROOT}/workflows/ccreview-loop.js", args})` で起動します。
   - `untrusted` のときは名前で起動し、見つからなければ止めて利用者に伝えます。作業ツリーの `scriptPath` は使いません（ccreview 自身を見直す PR では、PR 側の版になるため）。
   - args: `{ range, changedFiles, diffDir: "<diffDir>", maxRounds, trust, gates, review: { severityText, categoriesText, categories, commonText }, commandResults, deferredGates }`（`trust` が無いと workflow は起動時に止まります）
   - 観点ごとの agent は、指摘に重大度と category を自分で付けます。分類用の agent は、それが無い指摘（任意の `kind: review` ゲートなど）にだけ使われます（決定 12）。
   - 観点ごとの agent は、指摘の出どころ（`origin`）も付けます。「この変更を元に戻しても、この問題はまったく同じ形で残るか」で決め、残るなら `pre-existing`（もとからの問題）です。`pre-existing` の指摘は、判定にも修正にも使いません（決定 33）。値が無い指摘、知らない値の指摘、観点以外のゲートの指摘は判定に入ります。
   - 観点ごとの agent と分類用の agent は、プラグインの型 `ccreview:ccreview-reviewer`（Read・Grep・Glob だけ、`omitClaudeMd`）で動きます。Bash を持たないので、変更のコードを実行できません（決定 16。プロンプトでの禁止は e2e で守られませんでした）。2 ラウンド目以降の差分は、修正 agent がコミット後に `git diff <range> --output=<diffDir>/diff-r<次のラウンド>.patch` で書き、次のラウンドの観点役がそれを読みます。
   - 型と hook はセッションの開始時に読み込まれます（プラグインを `--plugin-dir` で読み込んで始めたセッションで起動します）。型が見つからないと、観点のゲートは `indeterminate` で止まります（Bash を持つ型には戻しません）。
   - workflow の中で Bash を持つのは修正 agent だけで、hook が許可リストの範囲に絞ります。1 呼び出しに 1 コマンド、編集は range の変更ファイルだけ（range が変えていても守られるファイルがあります。主なものは `.git`・settings・hook・agent の型・workflow・skill・どの階層の `.claude/rules/` と `CLAUDE.md`・根元の `REVIEW.md` と `.claude/review-gates.yaml`・ゲートのスクリプトです。守るファイルは 3 か所で決まります。決まったパスは `hooks/gate.py` の `PROTECTED_SEGMENTS` と `PROTECTED_GLOBS`、command ゲートが実行するスクリプトは run ごとの許可リストの `protected`（`scripts/write_allowlist.py` が作ります）、ccreview プラグイン自身のディレクトリは `gate.py` の `edit()` が別に断ります。そこへの指摘は修正 agent の `skipped` と結果の `remaining` でメインに戻ります。決定 32）、コードの実行は不可です。リスト外は理由付きの `deny` で返り、背景の run は止まりません。修正 agent はコミット後に、保留でない command ゲートを実行します。`untrusted` では、workflow は修正 agent も command ゲートも動かさず、読み取り専用の観点役だけでレビューします（決定 18）。blocking の指摘があれば `fail`、無くても実行しなかった blocking の command ゲートがあれば `indeterminate` です。
   - **決定 25**: 2 回目以降の command ゲートは、修正 agent がコミットしたあとに実行して報告します。`deferredGates` のゲートと、報告が欠けたゲートは、workflow が agent で実行することはせず「保留」（結果の `deferred`）にします。保留はループを止めません。ただし最後に `pass` になるところで blocking の保留が残っていれば、verdict は `indeterminate`（「deferred to the main session」）になります。
   - `maxRounds` は yaml の `max_rounds`（利用者の指定があればそれを優先します）。
   - **結果を受け取る（決定 42）**: 終わると、タスクの完了の通知が届きます。通知の `<result>` は途中で切れることがあります（e2e D8 では先頭の 8,000 字のあとに「`... (truncated 10741 chars, full result in <ファイル>)`」）。通知の `<output-file>` の絶対パスを、そのまま次のコマンドに渡します。`cd` も相対パスも使わず、自分で読み方を組みません（e2e D8 では、形を読み違えたあと、`cd` を含む複合コマンドが許可の確認で止まり、利用者が戻るまで待ちました）。
     `python3 ${CLAUDE_PLUGIN_ROOT}/scripts/extract_result.py --output <output-file の絶対パス> --out <リポジトリの外の一時ファイルの絶対パス>`
     - レビューする worktree の中で実行します。`--out` がリポジトリの中かは、実行した場所のリポジトリで判定するためです。
     - `--out` は、たとえばセッションのスクラッチパッドの下の `ccreview-result.json` です（無ければ `/tmp` の下）。手順 5 の検算の `--result <結果>` にも、同じパスを渡します。
     - 出力ファイルの中の workflow の戻り値（`result` キー）を `--out` に書き、件数の要約だけを表示します。手順 5 では、`--out` のファイルを Read で読みます。
     - 終了コード 2 は、形が違う・パスが相対・`--out` がリポジトリの中か `--output` と同じか書けないなどで、何も書いていません。表示された理由を利用者に伝え、自分で読み方を組んで回り込みません。
5. **結果を報告する**
   - `verdict`（pass / fail / indeterminate）、`stopReason`、ラウンドごとの不合格キーと修正コミットを要約します。
   - 下の検算と command ゲートで直したあとの最終の判定が `fail` か `indeterminate` なら、コメント案の投稿する部分に、止まった理由を平易な 1 文で書きます（決定 37。e2e D7 では「2 巡で止まりました」だけで、承認する人は理由も、確認役が確かめたかも読み取れませんでした）。`pass` に直したときは書きません。理由は `stopReason` の種類から日本語で書き、英文・キー・`repeat_of` の番号・観点役の要旨や引用は写しません。同じ理由で止まったとき（`same reason failed twice`）は、その 1 文の後ろに、下の `confirmations` の答えと検算の `fixedOutsideCommit` に応じて次の定型文を、文言を変えずに続けます（当てはまるものすべて。n の数え方は各項のとおり）。
     - `present` が 1 件以上: 「修正の前後を知らない確認役が、今のファイルで残っていることを確かめました（n 件）。」 n は指摘の件数です。
     - `none` が 1 件以上: 「確認役の答えが無く、確かめられていない指摘が n 件あります。」 n は指摘の件数です。
     - `stopReason` のキー（`ゲート|パス|カテゴリ`）のパスが、止まった回（`rounds` の最後）の 1 つ前の回の検算の `fixedOutsideCommit` にある（決定 38）: 「ただし、n 件のファイルは修正のコミットが変えておらず、直そうとされていない可能性があります。」 n は、当てはまるパスの数（重複を除く）で、指摘の件数ではありません。このときは、止まった理由の 1 文でも「直したが効かなかった」とは書かず、「修正のあとも残った」と書きます。
   - 最終の判定が `fail` か `indeterminate` なら、コメント案の投稿する部分に「残った指摘（マージを止めるもの）」の節を置き、`remaining` のうち `severity` が `blocking` で、キー（`ゲート|パス|カテゴリ`）が最後の回の `rounds[].keys` にある指摘を、すべて載せます（決定 41。e2e D8 では、ここに `client.py:43` しか無く、判定に入っていたほかの blocking の 4 件は、2 件が下の「もとからの問題（判定に使っていません）」に載り、2 件はどこにも載りませんでした）。`pass` では `keys` が空なので、この節は置きません。検算やメインでの command ゲートで `fail` に直したときなど、載せる指摘が無いときも置かず、失敗したゲートはその項のとおりに書きます。同じ問題を複数の観点が挙げるので、パスと要旨で 1 件にまとめます。まとめた項には、次の 2 つの条件をどちらも満たすときだけ、次の定型文を文言を変えずに添えます。
     - 「この変更が触れていない行への指摘です。変更の前からある問題かもしれません。」
     - 条件 1: まとめる前の `remaining` の項のすべてが、下の検算の `introducedOnUnchangedLines` に、ゲート・パス・行が同じで、回が最後の回の項としてある（`remaining` の項には回が付いていません）。
     - 条件 2: コメント案に書く行（実物のファイルで確かめた行）が、その `introducedOnUnchangedLines` の項の行と同じ。検算が「変更が触れていない」と判定したのは観点役が付けた行なので、確かめた行が違えば、定型文が事実とは限りません（決定 30 の行番号の誤り）。
     - まとめる前の項の一部だけが `introducedOnUnchangedLines` にあるとき、または条件 1 を満たして条件 2 を満たさないときは、添えずに、利用者への報告と「次の一手」の節に、その項と、満たさなかった条件（外れた項のゲート・行）を書きます。`introducedOnUnchangedLines` に 1 つも無い項は、何も書き足しません。
   - 直した指摘は `rounds[].fixedFindings` から取ります（決定 34）。修正 agent が「直した」と申告した指摘で、ゲート・パス・行・要旨が付いています。申告であって確かめた結果ではないので、「直したと申告した」と書き、確かめたのは次のラウンドの観点役と検算だと分かるようにします。同じ問題を複数の観点が挙げるので、パスと要旨で 1 件にまとめます。行は観点役が修正の前のファイルに付けた番号なので、まとめた指摘ごとに観点役どうしで食い違うことがあります（e2e D5 では 37 と 38）。コメント案には、修正のコミットのファイル（`git show <修正の sha>:<パス>`）で確かめた行を書きます。検算の `fixedOutsideCommit`（下）に出た指摘は、そのコミットがファイルを変えていないので、「直したと申告したが、修正のコミットはそのファイルを変えていない」と添えます。
   - `indeterminate` は「判定できなかった」であり、合格ではありません。そう明記します。
   - `outOfRange` は、range 外のファイルへの指摘です（観点の agent が参照先のファイルに触れたときや、任意の `/code-review` ゲートがローカルの main と比べたときに出ます）。判定にも修正にも使っていません。件数と要旨だけを「範囲外の参考」として添えます。
   - `outOfRangeBlocking` が 1 以上なら、マージを止めるゲートが blocking と付けた指摘が範囲外に回っています。パスの書き方を正規化できずに外れた指摘かもしれないので（e2e D1 では 7 件）、判定がどうであれ、コメント案の**冒頭**に件数と、その指摘のパスと要旨を載せます。範囲の変更ファイルを指しているなら、判定の見直しを利用者に求めます。件数は、ゲート・パス・要旨が同じ指摘をラウンドをまたいで 1 件と数えます。`/code-review` の指摘は重大度が付く前に範囲外へ回るので、この件数に入りません（`outOfRange` には載ります）。
   - `preExisting` は、観点役が「この変更の前からある」（`origin: pre-existing`）とした、範囲内のファイルへの指摘です（決定 33）。判定にも修正にも使っていません。コメント案に「範囲内の、もとからの問題（判定に使っていません）」として、パス・行・要旨を載せます。同じ指摘がラウンドごとに出るので、ゲート・パス・要旨が同じものは 1 件にまとめます。ただし、上の「残った指摘」で定型文を添えた項と、パスと行が同じものは、ここに載せません（決定 41。e2e D8 では、ほかの観点が `pre-existing` と付けた `client.py:11` が、correctness の `introduced` で判定に入ったまま、この見出しの下に載りました）。比べる行は、どちらもコメント案に書くために実物のファイルで確かめた行です。`preExisting` の項の行を確かめられない（`line` が `null`、または実物に当たる行が見つからない）ときは、外さずに載せます。`preExistingBlocking` は、そのうちマージを止めるゲートが blocking と付けた件数です。外した項も含めて workflow が数えた値なので、コメント案に件数を書くときは `preExistingBlocking` を写さず、載せた項から数えます。
   - `disputed` は、観点役が「直したはずの指摘がまた出た」（`repeat_of`）と言い、確認役が今のファイルで見つけられなかった指摘です（決定 22）。判定にも修正にも使っていません。`pass` でも省かずに、パス・観点役の主張と引用（`evidence`）・確認役の引用（`confirmerQuote`）を並べてコメント案に載せ、人が確かめられるようにします。見逃しがあれば、ここに出ます。
   - `confirmations` は、確認役に回したすべての指摘と、その答えです（決定 37）。`answer` は `present`（今のファイルにまだある）、`absent`（見つからない。`disputed` に入ったもの）、`none`（その指摘への確認役の答えが無い。agent の失敗など）のどれかです。`present` と `none` の指摘は、どちらも判定に残り、繰り返しと数えられるので、その回のループは必ず「same reason failed twice」で止まります。確認役に回るのは観点のゲートの繰り返しだけで、key で比べるゲート（`/code-review` と command）の繰り返しは回りません。そのため `stopReason` のその文では、番号（`repeat of #N`）は観点のゲートのキーにだけ付き、件数（`confirmer: N still present, M no answer`。キーではなく指摘の件数）は、その回に `present` か `none` が 1 件以上あるときだけ末尾に付きます。投稿する部分には、上の定型文だけを書きます。確認役の引用（`quote`）と番号は投稿せず、判定がどうであれ利用者への報告に載せ、`fail` と `indeterminate` のときは「次の一手」の節にも載せます。`absent` のものは、上の `disputed` の項のとおりに扱います。
   - `repeatMismatches` は、落ちたゲートの blocking の指摘のうち、観点役が `repeat_of` に付けた番号の「直したと申告された指摘」が別のファイルのものだった指摘です（決定 35）。繰り返しとは扱わず、新しい指摘として判定と次の修正に入れています。観点役が番号を誤った印で、PR の作者に向けた情報ではないので、コメント案の投稿する部分には載せません。空でなければ、判定がどうであれ（`pass` でも）利用者への報告に、パス・要旨・付いた番号・番号が指すファイル（`claimedPath`）を載せます。`fail` と `indeterminate` のときは、下の「次の一手」の節にも載せます。同じ誤りがラウンドごとに出ることがあるので、ゲート・パス・要旨・番号が同じものは 1 件にまとめます。修正がコードを別のファイルへ移して問題が移った先に残った場合や、観点役が番号をずらして別のファイルの指摘を指した場合も、ここに出ます。同じファイルの別の指摘を指すずれは繰り返しと扱われ、ここには出ません（どちらも繰り返しとは扱わないので、ループが上限まで回ることがあります）。
   - `introduced` は、範囲内のファイルへの観点役の指摘のうち `origin: introduced` のものを、回の番号付きで並べた一覧です（決定 36）。判定に入ったままの指摘で、下の検算の材料です。この一覧そのものは報告せず、検算が返す `introducedOnUnchangedLines` を報告します。
   - `deferred` は、メインでの実行を待っている command ゲートです（決定 25）。下の「メインでの command ゲートの実行」で確定させます。
   - `notRun` は、`untrusted` のために実行しなかった command ゲートです。「実行していない」と明記し、合格とは書きません。必要なら、利用者が中身を確かめてから `trusted` で再実行します。
   - **毎回、観点役が読んだ差分と修正 agent の自己申告を git で検算します（決定 19）。** `trusted` でも `untrusted` でも、修正が無くても行います。1 ラウンド目の差分ファイルも検算の対象だからです。手順 4 の `extract_result.py` が書いた、リポジトリの外の結果のファイルを使い（worktree の中に置くと未追跡のファイルが残り、`git worktree remove` が止まります）、`python3 ${CLAUDE_PLUGIN_ROOT}/scripts/verify_fixes.py --base <起点 SHA> --start <手順 4 で記録した HEAD> --result <結果> --diff-dir <diffDir>` を実行します。
     - `status: mismatch`（申告の無いコミット、または差分ファイルが無いか git と食い違う）なら、verdict を `indeterminate` に直します。観点役が、実物と違う差分を読んだことになるためです。`invalid` の理由を示します。
     - `notes`（ファイル一覧の食い違い）は、判定を変えずに添えます。
     - `preExistingConflicts` は、`pre-existing` とされた指摘のうち、その行を変更が足したか変えたもの（決定 33）です。この変更の前からある問題とは言えないので、外したのが誤りかもしれません。判定は変えませんが、空でなければ、判定がどうであれ、利用者への報告の冒頭とコメント案の冒頭に、件数とパス・行・要旨を載せ、判定の見直しを利用者に求めます。行番号がずれた指摘でも出ることがあるので、実物のファイルで確かめてから書きます。
     - `introducedOnUnchangedLines` は、逆の向きです（決定 36）。範囲内のファイルへの観点役の指摘のうち、`introduced` とされ、その行を変更が足しても変えてもいないもので、行を消しただけの差分では、消した位置の前後の行は変えた行と数えます。もとからある問題に観点役が `introduced` と付けたのかもしれません（e2e D7 では、差分ファイルを読まなかった観点役 1 体が、土台の問題 4 件にそう付けました）。ただし、変更が表に出した問題（本来は `exposed`）や、行番号の誤りでも出ます。判定は変えず、指摘も判定に入ったままです。この項は観点役の分類の話で、PR の作者に向けた情報ではないので、コメント案の投稿する部分には載せません。そのうち上の「残った指摘」の条件に当たる指摘（判定に入っている blocking）は、そこに載り、2 つの条件を満たせば定型文が付きます（決定 41）。nit など、判定に入っていない項は「残った指摘」には載りません。空でなければ、判定がどうであれ（`pass` でも）利用者への報告に、件数とパス・行・要旨を載せます。`fail` と `indeterminate` のときは「次の一手」の節にも載せ、その指摘で判定を落としてよいかを人が確かめられるようにします。ゲート・パス・要旨が同じものは、ラウンドをまたいで 1 件にまとめます。行は実物のファイルで確かめてから書きます。
     - `fixedOutsideCommit` は、修正 agent が「直した」と申告した指摘（`rounds[].fixedFindings`）のうち、そのパスをその回の修正のコミットが変えていないものです（決定 38。e2e D7 の観察 5）。直そうとされていない手がかりですが、ファイル間の食い違いを相手側のファイルで直した正しい修正でも出ます。どちらが多いかは、実地ではまだ見ていません。照らすのはパスだけなので、同じコミットが変えた別のファイルのパスを書いた誤りの申告は、ここに出ません。判定は変えません（理由は `${CLAUDE_PLUGIN_ROOT}/docs/decisions.md` の決定 38）。空でなければ、判定がどうであれ（`pass` でも）利用者への報告に、回・パス・要旨を載せます。`fail` と `indeterminate` のときは「次の一手」の節にも載せます。止まった理由に当たるときだけ、投稿する部分に上の定型文を足します（直した指摘の一覧への添え書きは別で、上の決定 34 の項のとおり、判定に関わらず付けます）。
   - `unexpectedFiles` は、修正のコミットが range にも指摘にも無いファイルを含んだものです（e2e 5 回目では、別のプラグインの hook が書いたセッションの記録を stage しました）。workflow の値は修正 agent の申告から作られているので、検算で得た値で報告します。判定には使いません。パスを示し、残すか修正コミットから外すかを利用者に確かめます。
   - **メインでの command ゲートの実行**: 修正が 1 回以上入ったとき、または `deferred` が空でないときは、command ゲートをすべてメインで実行します（`ask` 規則に当たるものは、利用者の確認を経て実行されます）。2 回目以降の結果は修正 agent の自己申告であり、保留のゲートにはそもそも結果が無いためです。
     - verdict が `pass`、または保留だけが理由の `indeterminate`（stopReason が「command gate(s) deferred to the main session」）なら、すべて通れば `pass`、1 つでも失敗すれば `fail` に直します。失敗したゲートと、それが保留だったか修正 agent の報告と食い違ったかを書きます。
     - `fail` と、それ以外の理由の `indeterminate` は変えず、実行結果を添えるだけにします。
   - MR/PR コメント案をリポジトリ外の一時ファイルに書き、パスを示します。投稿は指示を待ちます。
     - コメント案に載せる行番号は、実物のファイルで確かめてから書きます。観点役が差分ファイルの中の行番号を付けることがあるためです（決定 30。e2e D2 では、`client.py` の 11 行目の指摘に 65 行目が付きました）。メインが差分ファイル（`diff-r<N>.patch`）を読んだときも、その中の行番号を使いません（e2e D5 では、メインが `diff-r1.patch` の 42 行目を、`sync.py` の 38 行目の修正の行として書きました）。行番号は判定には使っていないので、食い違っても判定は変えません。
     - `fail` と `indeterminate` のときは、同じファイルのコメント案の後ろに、「利用者向け（投稿しない）」と見出しを付けた「次の一手」の節を置きます。残った指摘ごとに直す内容、停止理由、修正 agent が直さなかった理由（`rounds[].fix.skipped` の文。守るファイルの指摘はここで分かり、`stopReason` には出ません。決定 32。上限の回で止まったときは最後の回に `fix` が無いので、上限で止まったと書き、残った指摘のパスが守るファイルかを上の 3 か所で確かめて添えます）、再実行の前に人が決めること（`untrusted` の command ゲートを実行するか、`disputed` をどう扱うか など）、`repeatMismatches` と `introducedOnUnchangedLines`、「残った指摘」で条件を満たさず定型文を添えなかった項（決定 41）、`fixedOutsideCommit`、`confirmations` の `present` と `none` の引用と番号（空でなければ。上の各項のとおり）です。この節が訂正プランを兼ねます（決定 29）。投稿するのはコメント案の部分だけです。`untrusted` の PR では、この節の内容が作者に見えると、手元の判断の材料を渡すことになります。
     - コメント案はレビューの対象（PR/MR やブランチ）について書きます。会話の中で進めている別の issue やタスクに結び付けて名前や番号を付けません（e2e D2 では、試験の種への訂正プランに、会話の issue の番号が付きました）。
6. **後片付け**
   - 検算のあと、`trusted` でも `untrusted` でも `python3 ${CLAUDE_PLUGIN_ROOT}/scripts/write_allowlist.py --remove --diff-dir <diffDir>` を実行します。許可リスト（あれば）と、`<diffDir>` の差分ファイル（`diff-r<N>.patch`）を消します。許可リストは期限切れでも hook が拒否しますが、残しません。差分ファイルは未追跡なので、残すと `git worktree remove` が止まります。`<diffDir>` に run が書いていないファイルがあれば、フォルダごとは消さずに残します。hook の判定の記録は、許可リストと同じ場所の `hook-decisions.jsonl` に残ります（決定 27）。git dir が求まらなかった判定は `${CLAUDE_PLUGIN_DATA}/hook-decisions.jsonl` に `cwd` 付きで残り、どちらにも書けなかった判定は理由文の末尾に「This decision was not logged」と出ます。修正 agent の拒否を確かめるときは、この 3 か所を見ます。
   - 修正コミットは対象ブランチに残したまま、push するかは利用者に委ねます。
   - 使い捨てのブランチや worktree を消すときは、`-D` が要るなら先に利用者の確認を取ります。

## テスト

`node ${CLAUDE_PLUGIN_ROOT}/tests/loop-sim.mjs` — agent をモックしてループの判定（合格・修正後合格・判定不能・同一理由 2 連続・集合が減らない・command 失敗は blocking・ゲート間のパス正規化・range 外の除外・観点による自己採点と passes・`repeat_of` による同一理由の判定と確認役・範囲外のコミットの報告・`untrusted` での修正と command の停止・`trust` の必須化・型の名前空間・保留の扱い・スキルと workflow の名前が衝突しないこと・差分ファイルの受け渡しと、書かれなかったときの注記・最終ラウンドでは修正せず次の差分も求めないこと・絶対パスや `b/` 付きのパスを変更ファイルに結び付けること・範囲外に回った blocking の件数・観点役に行番号の取り方を伝えること・もとからの問題を判定と修正から外し、`origin` が無いか知らない値なら判定に入れること・ラウンドごとに直したと申告された指摘をパスと行付きで返すこと・`repeat_of` の番号が別のファイルの指摘を指すときは繰り返しと扱わず `repeatMismatches` に載せること・`introduced` とされた範囲内の観点の指摘を、回の番号付きで返すこと・確認役の答えを、残っている・無い・答えなしの 3 つとも返し、止まった理由の文に番号と件数を書くこと・観点役のプロンプトで差分の節を変更ファイルの指示より前に置くこと）を確かめます。49 シナリオです。

`python3 ${CLAUDE_PLUGIN_ROOT}/tests/verify_fixes_test.py` — 一時 git リポジトリで、検算（申告どおり・ファイルの申告漏れ・1 ラウンド目と修正ラウンドの差分ファイルの欠落と食い違い・後のラウンドが読まない修正の差分は問わないこと・2 ラウンドの修正・申告の無いコミット・別の cwd から絶対パスでの検算・UTF-8 でないファイル・もとからとされた指摘が、その巡の状態で変わった行にあるか・`introduced` とされた指摘が、その巡の状態で変わっていない行にあるか〔行を消しただけの位置の前後は変わった行と数える〕・直したと申告された指摘のパスを、その回の修正のコミットが変えているか〔日本語のファイル名。workflow が作っていないコミット、コミットの無い回、オブジェクトでない項目は見ない〕）を確かめます。

`python3 ${CLAUDE_PLUGIN_ROOT}/tests/gate_test.py` — hook の判定（修正 agent だけを判定する、Bash の許可の形とメタ文字、Edit の範囲・`.git`・シンボリックリンク・保護パス、保護パスの stage には Edit と同じ理由の文を返すこと、許可リストが無い・期限切れ・壊れたときの fail-closed、判定の記録の 3 段：許可リストの隣・`CLAUDE_PLUGIN_DATA`・理由文）を確かめます。

`python3 ${CLAUDE_PLUGIN_ROOT}/tests/write_allowlist_test.py` — `ask` / `deny` 規則の照合、保留の理由、保護するファイル、書いた許可リストを hook が受け入れること、差分を書く `--output` の形だけが許され、書いた差分ファイルを修正 agent が stage も編集もできないこと、`--remove` が差分ファイルを片付けることを確かめます。

`python3 ${CLAUDE_PLUGIN_ROOT}/tests/extract_result_test.py` — workflow の出力ファイルからの結果の取り出し（`result` が object でも JSON の文字列でも取り出すこと・結果だけのファイルも受けること・無い一覧は件数を null にすること・`result` が無い、`verdict` が無い、JSON でない〔通知の切れた文を含む〕、先頭が配列、UTF-8 でない、入れ子が深すぎる、ファイルが無いときは何も書かずに止まり、理由にはキーの名前だけを出して値を出さないこと・相対パスと、リポジトリの中〔シンボリックリンク越しを含む〕の `--out` と、`--output` と同じ `--out` を断ること・書けない `--out` でも traceback を出さずに止まること）を確かめます。

## 規模の目安（実測）

- 観点 4 つ × 1 回のレビュー: 1 ラウンドあたり約 140k トークン（観点 1 体あたり約 27〜47k、Explore 型・Sonnet）。
  専用型 `ccreview-reviewer` では、観点 1 体の起動時の文脈が約 8〜14k です（e2e 5 回目。Explore 型では約 21k）。
- 修正 agent は、既定型では 1 回あたり起動時に約 55k かかりました。専用型（`omitClaudeMd`）では、スパイクの型で約 7.9k でした。作業の往復が加わります。
- 見つけにくい種 8 件の計測では、観点 4 つ × 1 回の和が 3 回とも 8/8 でした。組み込み `/code-review` 単発は平均 6/8 で、3 回の和でも 7/8 でした。
- 小さな変更でも、1 ラウンドで十数万トークン、修正を含めると数十万トークン規模になります。実行前に利用者へ見込みを伝えます。
