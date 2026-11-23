# DocBOM データモデル

状態：v0.1草案。項目名と型は、実在文書による検証後に変更する。

## 1. 設計原則

- 文書の意味と提供形式を分ける
- 文書系列、文書の版、実体、派生物を分ける
- 公式配布物と自分たちの取得版を分ける
- 人が確認した情報と機械が記録した情報を分ける
- 最新版の把握とプロジェクトへの採用を分ける
- 不明な版や日付を推測で補わない

## 2. 主要な概念

| 概念 | 役割 | 例 |
|---|---|---|
| Subject | 文書の適用対象 | ACME MCU100、Silicon A1 |
| Document | 文書系列 | ACME MCU100 Errata |
| Revision | 公式上の文書版 | Rev. 1 |
| Source Artifact | ベンダーが配布した実体、または固定されたGit文書 | PDF、Git文書、SDK ZIP |
| Capture | Webなどから取得した不変の証跡 | WARC、HTML資源群 |
| Derivative | 実体から生成した利用用データ | 展開HTML、OCR、テキスト |
| Relation | 文書同士の意味関係 | amends、supersedes |
| Project Requirements | プロジェクトの採用条件 | MCU100、A1、SDK 1.2 |
| Project Lock | 解決済みの文書集合 | 版、実体ハッシュ、取得版ID |

`document_type` は文書の意味を表し、`artifact.kind` は提供・格納形式を表す。例えば `document_type: errata` と `kind: web_snapshot` を組み合わせられる。

## 3. Subject

```yaml
id: acme/mcu100
kind: chip
manufacturer: ACME
name: MCU100
attributes:
  family: MCU100
```

シリコンRevや製品版をSubject自体に含めるか、適用条件の属性として持つかは、実例を使って決定する。初期実装では、Subjectは製品を表し、シリコンRevなどは適用条件に置く案を検証する。

## 4. DocumentとRevision

原則として一文書系列一ファイルとし、版の一覧を持つ。

```yaml
id: acme/mcu100/errata
document_type: errata
title: ACME MCU100 Errata
publisher: ACME
document_number: ACME-ER-001
language: en

subjects:
  - ref: acme/mcu100
    silicon_revs: [A1]

relations:
  - type: amends
    target: acme/mcu100/datasheet

access:
  source_access: public
  confidentiality: public
  redistribution: unknown
  ai_egress: restricted

versions:
  - revision:
      label: "Rev. 1"
      label_raw: "Rev. 1"
      published: 2026-01-15

    artifacts:
      - role: source
        kind: pdf
        source_url: "https://example.invalid/acme/mcu100/errata-r1.pdf"
        acquired_at: "2026-11-23T10:00:00+09:00"
        storage_path: "store/source/ab/cd/abcd....pdf"
        sha256: "abcd..."
```

### 版ごとの上書き

`subjects`、`relations`、`language`、アクセス条件などは、版によって変わる可能性がある。共通値を文書の上位に置きつつ、必要なものは各版にも置けるようにする。

特に、新版のエラッタで対象シリコンRevが変わる場合を想定する。

### 不明な値

版や発行日が不明な場合は `null` とし、`label_raw` や取得記録に原文を残す。取得日やファイル更新日から公式版を推測しない。

## 5. ArtifactとCapture

### Source Artifact

ベンダーが配布したPDF、ZIP、CHM、SDKなどを指す。実体を加工せず保存し、SHA-256で識別する。

主な候補項目：

- `role: source`
- `kind`
- `source_url` または入手元情報
- `acquired_at`
- `storage_path`
- `sha256`
- 外側パッケージがある場合は `package` と `inner_path`

### Git文書

Gitで配布・管理される文書は、ブランチ名だけではなくコミットIDで固定する。初期実装では単一のMarkdownファイルを `kind: git_file` として扱い、元リポジトリ、コミット、パス、保存したファイル実体のハッシュを記録する。

```yaml
- role: source
  kind: git_file
  repository_url: "https://example.invalid/acme/mcu100-docs.git"
  commit: "0123456789abcdef0123456789abcdef01234567"
  source_path: "docs/getting-started.md"
  acquired_at: "2026-11-23T10:15:00+09:00"
  storage_path: "store/source/98/76/9876....md"
  sha256: "9876..."
```

複数ファイルで構成されるGit文書は、後続で `git_tree` とファイル一覧のハッシュを検討する。

### Capture

Web、API、アプリなどから自分たちが取得した証跡を指す。

```yaml
- role: capture
  kind: web_snapshot
  capture_id: "2026-11-23T103000+0900-abcd"
  capture_profile: acme-web-manual/v1
  acquired_at: "2026-11-23T10:30:00+09:00"
  storage_path: "store/capture/12/34/1234....warc"
  sha256: "1234..."
  resource_manifest_sha256: "5678..."
  completeness: complete_within_declared_scope
```

比較用の正規化本文ハッシュは、厳密な実体識別子とは別に記録する。正規化結果の一致を、文書全体の無変更とみなさない。

`completeness` の候補：

- `complete_within_declared_scope`
- `partial`
- `unknown`

Webサイト全体の完全性を意味しないよう、必ず取得プロファイルの範囲と組み合わせて解釈する。

## 6. Derivative

展開HTML、OCR、テキスト、章立て、文字起こし、検索索引などを指す。

少なくとも次を記録する。

- 生成元の `sha256` または `capture_id`
- 派生物の種別
- 生成ツールと版
- 生成日時
- 派生物自身のハッシュ
- 検証状態

派生物の詳細は人間用のDocument YAMLへ直接列挙せず、`derived/` と `index/` の生成メタデータで管理する。

## 7. Relation

初期の候補：

| 関係 | 意味 |
|---|---|
| `supersedes` | 対象の旧版を置き換える |
| `amends` | 対象文書を訂正・補足する |
| `applies_to` | 対象に適用される。対象はSubject側にも表現する |
| `requires` | 利用時に併読が必要 |
| `references` | 参照関係がある |
| `translation_of` | 対象文書の翻訳である |
| `part_of` | パッケージや文書集の一部である |
| `distributed_with` | 特定SDKなどと一緒に配布される |

関係は片側だけを正本として書き、逆引きは `index/` に生成する。版によって異なる関係は、文書共通ではなく版の側に持たせる。

## 8. アクセス条件

次を独立した項目として扱う。

| 項目 | 意味 |
|---|---|
| `source_access` | 公開、アカウント必須、制限付きなどの入手条件 |
| `confidentiality` | 公開、社内、NDAなどの機密区分 |
| `redistribution` | 再配布可否。不明を許容する |
| `ai_egress` | 外部AI可、ローカルのみ、禁止など |

`public` という一つの値で、入手・再配布・AI送信をまとめて表現しない。

## 9. プロジェクトの採用条件

`docbom.yaml` の例：

```yaml
context:
  subject: acme/mcu100
  silicon_rev: A1
  sdk: "1.2"

documents:
  - id: acme/mcu100/datasheet
    revision: "Rev. 1"

  - id: acme/mcu100/reference-manual
    revision: "Rev. 3"

  - id: acme/mcu100/errata
    selection: applicable
```

この形式は確定仕様ではない。まず、固定版と適用条件による選択を区別できることを優先する。

## 10. プロジェクトのロック

`docbom.lock.yaml` の例：

```yaml
lock_version: 1
catalog_revision: "中央目録のGitコミット"
resolver_version: "0.1.0"

documents:
  - id: acme/mcu100/datasheet
    revision: "Rev. 1"
    artifacts:
      - role: source
        kind: pdf
        sha256: "aaaa..."

  - id: acme/mcu100/errata
    revision: "Rev. 1"
    artifacts:
      - role: source
        kind: pdf
        sha256: "bbbb..."
```

Web文書では `capture_id` と取得版ハッシュも固定する。

ロックファイルは生成物だが、プロジェクトのGitへコミットする。中央目録の状態が変わっても、明示的に再解決しない限り使用文書集合を変えない。

## 11. 出典位置

出典位置をPDFページだけに限定しない。

| 形式 | 位置の例 |
|---|---|
| PDF | PDFページ、印刷ページ |
| HTML・Web | 内部パスまたはURL、アンカー、capture_id |
| CHM | CHM内部パス、アンカー |
| Git | コミットID、パス、見出しまたは行 |
| Office | 章、シート、セル、スライド |
| JSON・XML | レコードID、JSON Pointer、XPath |
| 動画・音声 | 時間範囲 |

共通の `locator` として扱い、実際の表現は形式別に拡張する。

## 12. 未決事項

- Subjectの階層と、シリコンRevなどの属性の持ち方
- 一文書系列一ファイルが大きくなった場合の分割基準
- 文書版と取得版の関係を解決する最小アルゴリズム
- 関係の向きと、版ごとの関係の上書き規則
- `latest_known` を導出できる条件
- Captureの完全性と等価性の判定方法
- ロックファイルに記録する中央目録・解決器の再現情報
