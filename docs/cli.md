# DocBOM CLI利用手順

M2のCLIは、リポジトリのルートで `python -m docbom` として実行する。Python 3.12を想定する。

```sh
python -m pip install -r requirements-dev.txt
python -m docbom --help
```

## 1. init

### 文書ストア

```sh
python -m docbom init store /path/to/docbom-store
```

`subjects/`、`manifest/`、`store/source/`、`store/capture/`、`state/` などを作成する。既存ファイルは初期化時に上書きしない。

Subjectは `templates/subject.yaml` を基に登録する。例えば、テンプレートを変更せずに使う場合は次のように配置できる。

```sh
cp templates/subject.yaml /path/to/docbom-store/subjects/vendor-product.yaml
```

### プロジェクト

```sh
python -m docbom init project /path/to/project \
  --subject vendor/product \
  --silicon-rev A1 \
  --sdk 1.2
```

`docbom.yaml` を作成する。使用文書はこのファイルの `documents` に追加する。`docbom.yaml` がすでにある場合は上書きせず終了する。

## 2. ingest

### PDF

```sh
python -m docbom ingest pdf ./datasheet.pdf \
  --store /path/to/docbom-store \
  --id vendor/product/datasheet \
  --title "Example Datasheet" \
  --publisher "Example Vendor" \
  --document-type datasheet \
  --document-number EX-DS-001 \
  --subject vendor/product \
  --revision "Rev. 1" \
  --published 2026-01-15 \
  --source-url https://example.invalid/vendor/product/datasheet.pdf
```

PDFの署名を確認し、実体をSHA-256のアドレス付きで `store/source/` に保存する。`--title` を省略すると、PDFのメタデータのタイトル、次にファイル名を使う。目次や高度なPDF構造の抽出はM2の対象外である。

### Git文書

```sh
python -m docbom ingest git https://example.invalid/vendor/docs.git \
  --store /path/to/docbom-store \
  --id vendor/product/reference-manual \
  --publisher "Example Vendor" \
  --document-type reference-manual \
  --subject vendor/product \
  --revision docs-v1.0 \
  --commit 0123456789abcdef0123456789abcdef01234567 \
  --path docs/reference-manual.md \
  --tag docs-v1.0
```

ブランチ名ではなく、40桁のコミットIDとパスで文書を特定する。指定コミットから単一ファイルを取得し、リポジトリURL・コミット・パス・保存実体ハッシュを記録する。M2では複数ファイルの `git_tree` とGit LFSを扱わない。

### 共通の動作

- `--acquired-at` を省略すると現在のUTC時刻を記録する。
- 保存ファイル名は `store/source/先頭2桁/次の2桁/SHA-256.拡張子` とする。
- Document YAMLがまだなければ作成し、既存なら版または実体を追記する。
- 同じ版の既存実体を上書きせず、異なるハッシュの実体は別物として保持する。
- 新規文書のアクセス条件は安全側の `unknown` にする。入手・機密・再配布・外部AI送信の区分は、人が確認して修正する。

## 3. validate

```sh
python -m docbom validate \
  --store /path/to/docbom-store \
  --project /path/to/project
```

プロジェクトディレクトリに `docbom.lock.yaml` があれば、あわせて検査する。ロックだけを指定することもできる。

```sh
python -m docbom validate \
  --store /path/to/docbom-store \
  --lock /path/to/project/docbom.lock.yaml
```

検査内容：

- Subject、Document、Capture Profile、Project、Lockのスキーマ
- Subject IDとDocument IDの重複
- Subject参照、プロジェクトの適用対象、文書関係の参照先
- 保存実体とWeb資源一覧の存在・SHA-256一致
- ロックの版・ハッシュ・Gitコミット・Web取得版が目録と一致するか
- 不完全なWeb取得版がロックされていないか

検査に失敗した場合は終了コード1を返す。

## 4. resolve

```sh
python -m docbom resolve \
  --store /path/to/docbom-store \
  --project /path/to/project \
  --output /path/to/project/docbom.lock.yaml
```

`--catalog-revision` を省略すると、文書ストアが属するGitリポジトリのHEADを記録する。Git管理外や再現性のあるテストでは、40桁の値を明示する。`--generated-at` も省略時は現在のUTC時刻になる。

選択方法：

- `revision`：指定版に固定する。プロジェクトの適用対象に合わない版はエラーにする。
- `selection: applicable`：Subject、Silicon Rev、属性がプロジェクトに適用される全版を選ぶ。適用される版がなければ、その文書はロックに含めない。
- `selection: latest_capture`：適用対象内の最新Web取得版を選ぶ。最新取得版が不完全なら、古い完全版へ自動で退かずエラーにする。

`resolve` を実行した場合だけロックを生成・更新する。中央目録への新版登録だけで既存ロックが変わることはない。

## 5. get

### ロック済み実体のパスを取得

```sh
python -m docbom get vendor/product/reference-manual \
  --store /path/to/docbom-store \
  --lock /path/to/project/docbom.lock.yaml
```

実体のパスを表示する前に、ロック・目録・保存実体のハッシュ一致を再検査する。

### 実体を複写

```sh
python -m docbom get vendor/product/reference-manual \
  --store /path/to/docbom-store \
  --lock /path/to/project/docbom.lock.yaml \
  --output ./reference-manual.md
```

出力ファイルがすでにある場合は上書きしない。

### 本文を抽出

Git文書とUTF-8の汎用ファイルでは `--lines` で1始まりの行範囲を指定できる。

```sh
python -m docbom get vendor/product/reference-manual \
  --store /path/to/docbom-store \
  --lock /path/to/project/docbom.lock.yaml \
  --text --lines 10:20
```

PDFでは `--page` で1始まりのページを指定できる。

```sh
python -m docbom get vendor/product/datasheet \
  --store /path/to/docbom-store \
  --lock /path/to/project/docbom.lock.yaml \
  --text --page 3
```

同じ文書IDに複数の版や実体がロックされている場合は `--revision` または `--sha256` で特定する。Web取得版の本文抽出、PDFの章検索、Markdownの見出し検索はM2では未対応である。

## 既存例での確認

```sh
python -m docbom validate --store examples/demo-store --project examples/project-old
python -m docbom validate --store examples/demo-store --project examples/project-current
python -m unittest discover -s tests -v
```

再生成手順は[架空文書セットのREADME](../examples/README.md)を参照する。
