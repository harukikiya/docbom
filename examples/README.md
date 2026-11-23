# DocBOM M1 例

このディレクトリには、架空製品「ACME MCU100」の検証用文書セットを置く。

## 含まれる形式

- PDF：Datasheet Rev. 1／Rev. 2、Silicon A1向けErrata
- Web：同じWeb Manualの二つの取得版と、URL別資源一覧
- Git文書：特定コミット・パスに固定したMarkdownのReference Manual

`project-old` はA1・Datasheet Rev. 1・Web取得版1を使用する。`project-current` はA2・Datasheet Rev. 2・Web取得版2を使用する。A1向けErrataは `project-current` のロックには含まれない。

M1ではリゾルバをまだ実装しないため、各 `docbom.lock.yaml` は手作業で作成した**期待される解決結果**である。`catalog_revision` とGitのコミットIDも例示用の値である。

## 検証

リポジトリのルートで次を実行する。

```sh
python -m unittest discover -s tests -v
```

スキーマ検証に加え、保存実体のSHA-256、Web資源一覧のハッシュ、二つのプロジェクトの期待状態を検査する。
