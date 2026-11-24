# DocBOM 架空文書セット

このディレクトリには、架空製品「ACME MCU100」の検証用文書セットを置く。

## 含まれる形式

- PDF：Datasheet Rev. 1／Rev. 2、Silicon A1向けErrata
- Web：同じWeb Manualの二つの取得版と、URL別資源一覧
- Git文書：特定コミット・パスに固定したMarkdownのReference Manual

`project-old` はA1・Datasheet Rev. 1・Web取得版1を使用する。`project-current` はA2・Datasheet Rev. 2・Web取得版2を使用する。A1向けErrataは `project-current` のロックには含まれない。

各 `docbom.lock.yaml` はM1で手作業した期待結果を保存したもので、`catalog_revision` とGitのコミットIDも例示用の値である。M2のリゾルバは既存ロックを自動更新しない。`project-old` を明示的に再解決すると、要求にある `latest_capture` に従ってWeb取得版2を選ぶ。

## 検証

リポジトリのルートで次を実行する。

```sh
python -m unittest discover -s tests -v
python -m docbom validate --store examples/demo-store --project examples/project-old
python -m docbom validate --store examples/demo-store --project examples/project-current
```

スキーマ検証に加え、保存実体のSHA-256、Web資源一覧のハッシュ、二つのプロジェクトの期待状態、CLIによる取り込み・解決・取得を検査する。

## ロックの再生成と取得

次は `project-current` のロックを別ファイルへ再生成する例である。

```sh
python -m docbom resolve \
  --store examples/demo-store \
  --project examples/project-current \
  --output /tmp/docbom-current.lock.yaml \
  --catalog-revision bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb \
  --generated-at 2026-11-01T11:00:00Z

python -m docbom get acme/mcu100/reference-manual \
  --store examples/demo-store \
  --lock /tmp/docbom-current.lock.yaml \
  --text --lines 1:3
```

`--output` を省略すると、プロジェクトの `docbom.lock.yaml` を更新する。既存のロックを変えたくない場合は、上記のように別ファイルを指定する。
