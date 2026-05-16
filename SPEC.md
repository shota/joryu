# joryu 仕様書（draft v1）

> Alembic に代わる、SQLAlchemy ベースの Python マイグレーションライブラリ。
> 設計議論のたたき台。実装前に固める。

---

## 1. ゴールと非ゴール

### ゴール
- SQLAlchemy モデルの差分を読み取り、マイグレーションファイルを自動生成する
- 複数 PR の同時進行で **暗黙的な順序衝突を起こさない**（並走安全）
- マイグレーションは「DB 上の `joryu_migrations` テーブルに未登録のものを順次実行」する単純な追記モデル
- 順序を明示できる。明示が無ければ timestamp 昇順
- Python の表現力を活かしたデータ移行・条件分岐が一級市民
- raw SQL を一級市民として書ける（escape hatch）
- 生成 AI が誤りにくい API 設計（hallucination 耐性）
- **途中で止まったマイグレーションを安全に再開できる**（idempotent / resumable, §9）
- **大量データ更新を checkpoint で再開可能に扱える**（§9.5）
- **ユーザ定義のカスタム step** が一級市民（`op.step`, §9.6）
- **既存 Alembic プロジェクトからの移行ツールを最初から提供**（§14）
- MySQL/MariaDB、PostgreSQL、SQLite を初期サポート
- **同一マイグレーションファイルで複数 DB エンジンを跨いで動作可能**（必須要件）

### 非ゴール
- 本番環境向けの自動ロールバック（forward-only。dev での巻き戻しは別途）
- multi-tenant スキーマ切替の自動化
- スキーマ drift の自動修復
- 生成 AI 直接モード (`joryu generate --from-prompt "..."`) — **将来も非搭載**。代わりに公式 skill を repository 内に配布（`.claude/skills/joryu/`）し、ユーザのエディタ側 AI が joryu CLI を駆動する形にする

### 動作環境
- **Python 3.11+** （`tomllib` stdlib / `Self` 型 / Exception Groups / asyncio.TaskGroup / 性能改善を活用）
- 3.10 は除外（EOL 2026-10）
- 型ヒント・`match` 文・PEP 604 union 型 (`X | Y`)・`Self` 型を活用したモダンな書き方を志向

### 公式 AI skill 配布

- joryu の **GitHub repository 内** に `.claude/skills/joryu/` を含める形で配布
- スキルの内容: `joryu generate` / `apply` / `down` 完成 / `verify` 等の CLI を AI が呼べるよう手順を記述
- ユーザは自分のプロジェクトにスキルをコピー（または submodule / symlink）して使う
- 将来 VSCode extension 等の独立配布も検討するが、まずは repo 同梱で広く配布

---

## 2. アーキテクチャ全体像

```
プロジェクト/
├── models/                                # SQLAlchemy モデル（ユーザ管理）
├── migrations/
│   ├── 20260514T093000_add_users.py
│   ├── 20260515T101200_add_email_index.py
│   └── 20260516T120000_seed_default_roles.py
└── joryu.toml                             # プロジェクト設定
```

- **source of truth**: ユーザの SQLAlchemy `MetaData`
- **生成物**: `migrations/*.py`（並走 PR の merge を阻害する集中ファイルは置かない）
- **状態 / 改変検知**: DB 内の `joryu_migrations` テーブル（適用済みファイルの checksum を保持）
- **意味的競合検知**: `joryu verify` による Operations の静的解析（§7）

---

## 3. マイグレーションファイル形式（案 B: Python ファースト）

### 3.1 命名規則

```
<UTC timestamp ISO basic>_<slug>.py
例: 20260514T093000_add_users.py
```

- timestamp は **UTC・秒精度・ISO basic 形式**（`YYYYMMDDTHHMMSS`）
- lexicographic 順 = 時系列順
- スラッグは英小文字 + `_`、最大 60 文字
- 同秒衝突時はファイル末尾に `_2`, `_3` …

Django の per-app sequence (`0001_*.py`) を採用しない理由: 並走 PR で必ず番号衝突する。
Alembic のランダム hex を採用しない理由: 人間が読めない・ソート不能。

### 3.2 ファイル本体（デコレータスタイル）

```python
"""Add users table."""
import joryu
from joryu import op, types as t

@joryu.migration(
    id="20260514T093000_add_users",
    depends_on=[],                       # 空なら timestamp 昇順
    transaction_mode="per_step",         # default
    tags=["schema"],
)
def upgrade():
    op.create_table(
        "users",
        op.column("id",    t.BigInt, primary_key=True, autoincrement=True),
        op.column("email", t.Text,   nullable=False, unique=True),
        op.column("created_at", t.Timestamp, server_default=op.func.now()),
    )

@joryu.downgrade                          # 任意、dev 用
def downgrade():
    op.drop_table("users")
```

**メタデータ項目** (`@joryu.migration(...)` の引数)

| 引数 | 必須 | 説明 |
|---|---|---|
| `id` | yes | ファイル名と一致。状態テーブルに記録される論理 ID |
| `depends_on` | no, default `[]` | 先行マイグレーションの id リスト。空なら timestamp 順 |
| `transaction_mode` | no, default `"per_step"` | `"per_migration"` / `"per_step"` / `"none"` の三択。詳細は §9.3 |
| `dialects` | no | 特定方言限定。例: `["postgresql"]` |
| `tags` | no | 任意ラベル（フィルタ用） |
| `group` | no | ステップグループの ID。同じ group の migration は一連の論理変更として扱われる（§6 参照） |
| `on_mismatch` | no, default `"error"` | ensure 時の挙動。詳細は §9.4.2 |

**設計判断: なぜデコレータか**:
- モダンな Python の慣習（FastAPI / Typer / pytest が広めたパターン）
- 引数が型ヒント付きなので IDE 補完・型検査が効く
- 1 ファイル 1 migration の原則がコード上で明示される（複数の `@joryu.migration` を 1 ファイルに書くと ERROR）
- module-level 属性スタイル（Alembic 風）は廃止

---

## 4. Operations API（Django 風だが SQLAlchemy ネイティブ）

### 4.1 設計方針

- **Alembic の `op.*` の最大の不満点**を解消する：
  - 引数の冗長さ（`sa.Column(...)` を毎回書く）
  - 方言固有オプションの散在（`postgresql_using=...` などが API のあちこちに）
  - escape hatch (`op.execute`) が二級市民
- **Django の Operations クラス**の長所を取り入れる：
  - 宣言的オブジェクトで履歴をリプレイ可能 → 過去のスキーマ状態を再構築できる
  - データ移行 (`RunPython`) と DDL (`AddField` 等) が同じ list に並ぶ
- **SQLAlchemy MetaData / Column と相互運用** — モデルクラスをそのまま渡せる

### 4.2 主要 API

```python
from joryu import op, types as t

# DDL
op.create_table(name, *columns, **table_kwargs)
op.drop_table(name)
op.rename_table(old, new)

op.add_column(table, name, type, **column_kwargs)
op.drop_column(table, name)
op.alter_column(table, name, type=None, nullable=None, server_default=...)
op.rename_column(table, old, new)

op.create_index(name, table, columns, unique=False, concurrent=False, where=None)
op.drop_index(name, table=None)

op.create_unique_constraint(name, table, columns)
op.create_check_constraint(name, table, condition)
op.create_foreign_key(name, source_table, ref_table, source_cols, ref_cols, **fk_kwargs)
op.drop_constraint(name, table)

# Escape hatches (一級市民)
op.execute(sql_or_dict)                         # § 6 参照
op.run_python(callable)                         # 任意の Python を走らせる
op.batch(table)                                 # SQLite の table-rebuild を自動化
```

### 4.3 SQLAlchemy モデルとの統合

```python
from myapp.models import User           # SQLAlchemy モデル

def upgrade():
    op.create_table_from_model(User)    # __table__ をそのまま使う
    op.add_columns_from_model(User, only=["email", "phone"])
```

これにより「モデルに合わせて手でカラム定義を書き直す」二重管理を排除。

### 4.4 batch 操作（SQLite 対応の組込、明示要求）

SQLite は `ALTER TABLE DROP COLUMN`、制約変更等を直接サポートしないため、内部で table-rebuild（新テーブル作成 → データコピー → rename）が必要。

**設計判断: 明示要求とする（暗黙の自動 batch 化はしない）**:
- 数千万行のテーブルで silent な table-rebuild は危険（コピーコスト、ロック挙動、FK 一時 disable が見えない）
- ensure semantics の原則「勝手に状態を変えない」と一貫
- ただし `joryu generate` は SQLite ターゲット時に **自動的に `op.batch` でラップしたコードを生成** する（生成は支援、実行は明示）

```python
def upgrade():
    with op.batch("users") as batch:
        batch.alter_column("email", nullable=False)
        batch.drop_column("legacy_field")
        batch.create_check_constraint("email_lower", "email = LOWER(email)")
    # Postgres/MySQL では普通に ALTER、SQLite では table-rebuild
```

`with op.batch(...)` を書かずに SQLite で非対応 op を呼んだ場合は ERROR で止まる（`UnsupportedOperationOnSQLite`、batch 化を提案するメッセージ付き）。

Alembic の `batch_alter_table` と同思想だが、明示性を高めた設計。

### 4.5 データ移行 (`run_python`)

```python
def upgrade():
    op.add_column("users", "email_normalized", t.Text)

    def normalize(conn, dialect):
        # SQLAlchemy Connection が渡される。アプリのモデルも import 可能
        conn.execute(text("UPDATE users SET email_normalized = LOWER(email)"))
        if dialect.name == "postgresql":
            conn.execute(text("CREATE INDEX ... USING GIN ..."))

    op.run_python(normalize)

    op.alter_column("users", "email_normalized", nullable=False)
```

- DDL とデータ移行が **同じトランザクション内に並ぶ**（Alembic の最大の強み）
- 関数は `(connection, dialect)` を受け取る
- アプリの SQLAlchemy モデルを `import` しても OK（ただし「現時点のモデル」になる点は注意 — 後述）

---

## 5. AI フレンドリーな API 設計

Alembic の `op` API が LLM に書きにくい理由：

1. `sa.Column(...)` のネスト構造（行が長くなる）
2. 方言固有 kwarg が散在（`postgresql_using=`, `mysql_engine=` …）
3. `op.execute(text("..."))` の二段ラッピング
4. 引数の順序が直感的でない（`op.add_column(table, sa.Column(name, type))`）

joryu はこれらを潰す：

| 項目 | Alembic | joryu |
|---|---|---|
| カラム追加 | `op.add_column("u", sa.Column("e", sa.Text(), nullable=False))` | `op.add_column("u", "e", t.Text, nullable=False)` |
| 生 SQL 実行 | `op.execute(text("..."))` | `op.execute("...")` |
| 方言別 SQL | `if op.get_bind().dialect.name == "postgres": op.execute(...)` | `op.execute({"postgresql": "...", "mysql": "..."})` |
| 型 | `sa.Integer()`, `sa.BigInteger()` | `t.Int`, `t.BigInt` (短い・型ヒント完備) |

加えて：

- **すべての op API に型ヒント** → LSP が補完を出せる → LLM も schema を把握できる
- **`joryu generate` の出力を LLM が読みやすい体裁** に統一（決まった import 順、決まった引数順）
- **`joryu explain <id>`** で migration を自然言語化（人間レビューと LLM レビュー両方を助ける）

---

## 6. 複数 DB 方言対応（重要要件）

> 「同じマイグレーションファイルが SQLite でも MySQL でも動く」必要がある。
> ライブラリがネイティブ統一 API を提供する必要はないが、**ユーザが手で書けば可能** な構造にする。

### 6.1 三層モデル

joryu は SQL 表現を 3 つのレイヤで扱う：

1. **Layer 1: Operations 抽象（方言自動）**
   `op.create_table`, `op.add_column` などは joryu が方言別 SQL に翻訳する。
   ユーザは方言を意識しない。最も多くのケースをこれでカバー。

2. **Layer 2: 方言別ディスパッチ（`op.execute(dict)` / `op.run_python`）**
   `op.execute` は文字列・dict のどちらも受ける：
   ```python
   # 単一 SQL — 全方言で同じものを実行
   op.execute("CREATE EXTENSION IF NOT EXISTS pgcrypto")

   # 方言別 SQL
   op.execute({
       "postgresql": "CREATE INDEX CONCURRENTLY ... USING GIN (data)",
       "mysql":      "CREATE INDEX ... ON ...((CAST(data AS CHAR(255))))",
       "sqlite":     "CREATE INDEX ... ON ...(data)",
   })

   # default fallback
   op.execute({
       "postgresql": "CREATE INDEX CONCURRENTLY ...",
       "default":    "CREATE INDEX ...",      # postgresql 以外はこれ
   })
   ```
   key の正規化:
   - `"postgresql"`, `"postgres"`, `"pg"` は同義（最も標準的な `"postgresql"` を推奨）
   - `"mysql"`, `"mariadb"` は別 key（実装が分岐するケースがあるため）。両方に同じ SQL を書きたい時は `default` 使用
   - `"sqlite"`
   - `"default"` は他のどの key にもマッチしない方言で使われる
   - 現方言にも `"default"` にもマッチしなければ ERROR

   または関数で：
   ```python
   def upgrade():
       d = op.dialect.name
       if d == "postgresql":
           op.execute("CREATE TYPE status AS ENUM ('a', 'b')")
       else:
           op.create_table("status_enum", op.column("value", t.Text, primary_key=True))
   ```

3. **Layer 3: ファイル単位の方言限定 + Group**
   どうしても 1 ファイルで両立しない場合、`dialects=` で限定し、`group=` で論理的に束ねる：
   ```python
   # 20260601T120000_pg_partitions.py
   @joryu.migration(
       id="20260601T120000_pg_partitions",
       dialects=["postgresql"],
       group="20260601_partitions",          # 論理 group ID
       depends_on=["20260530T100000_create_events"],
   )
   def upgrade(): ...

   # 20260601T120000_mysql_partitions.py
   @joryu.migration(
       id="20260601T120000_mysql_partitions",
       dialects=["mysql"],
       group="20260601_partitions",          # 同じ group
       depends_on=["20260530T100000_create_events"],
   )
   def upgrade(): ...
   ```
   各環境では自分の方言に合致するもののみ適用される。`group=` の効果:
   - `joryu status` で「同じ論理変更」として束ねて表示
   - 後続 migration が `depends_on=["group:20260601_partitions"]` と書ける（その方言で適用された ID を自動解決）
   - `joryu verify` で「group 内のどれか 1 つは方言ごとに用意されているか」をチェック

### 6.2 推奨ユースケース別の選び方

| ケース | 推奨 Layer |
|---|---|
| カラム追加、テーブル作成、index 作成（型違いは joryu 側で吸収可） | L1 |
| `JSON` vs `JSONB`、`SERIAL` vs `AUTO_INCREMENT` 等の細部 | L1（joryu が方言別にレンダリング） |
| `CREATE INDEX CONCURRENTLY`、partial index、generated column | L2 (`op.execute(dict)`) |
| データ移行で方言固有関数 (`jsonb_set` vs `JSON_SET`) | L2 (`op.run_python` 内分岐) |
| Postgres にしかない機能 (`CREATE EXTENSION`, RLS, materialized view) | L3（dialect 限定ファイル） |
| SQLite だけ `batch` table-rebuild 必須 | L1 + `op.batch`（自動切替） |

### 6.3 型の互換性

joryu の `types` モジュールは方言間の最小公倍数を意識：

| `joryu.types` | Postgres | MySQL | SQLite |
|---|---|---|---|
| `Int`        | INTEGER | INT | INTEGER |
| `BigInt`     | BIGINT  | BIGINT | INTEGER |
| `Text`       | TEXT    | LONGTEXT | TEXT |
| `Json`       | JSONB   | JSON | TEXT (JSON1) |
| `Uuid`       | UUID    | CHAR(36) | TEXT |
| `Timestamp`  | TIMESTAMPTZ | TIMESTAMP | TEXT (ISO8601) |
| `Decimal(p,s)` | NUMERIC | DECIMAL | NUMERIC |
| `Bool`       | BOOLEAN | TINYINT(1) | INTEGER |

**方言固有型**を使いたい場合は `types.dialect("postgresql.tsvector")` のようなエスケープがある。

### 6.4 テスト戦略（unit / integration の二層）

migration の動作検証は **二層構成**:

#### Unit testing（デフォルト、軽量）

```
joryu test                                 # = joryu test --unit
joryu test --unit
```

- **in-memory SQLite** で全 migration を適用 → re-apply（ensure semantics 確認）
- 補助として in-memory な仮想 DB（純 Python の DDL シミュレータ）でも実行し、Operations の正当性を確認
- 数秒で終わる。**通常開発・PR の CI で常時実行**
- 検証: 構文エラー、ensure semantics 整合、`joryu verify` 同等のチェック、checkpoint API 正常動作

#### Integration testing（任意、重い）

```
joryu test --integration                                   # 設定された全 dialect
joryu test --integration --dialects=postgresql,mysql        # 個別指定
```

- **testcontainers** で各 RDBMS の本物のインスタンスを起動して全 migration を適用
- 方言固有の挙動（MySQL の暗黙 commit、Postgres の DDL transactional 性、SQLite の table-rebuild）を実機で検証
- 数分〜数十分。**nightly CI / リリース前の検証で実行**
- testcontainers が無い環境ではスキップ（CI runner で Docker が使えること前提）

#### 設定

```toml
[joryu.test]
default_mode = "unit"                      # "unit" | "integration"
integration_dialects = ["postgresql", "mysql", "sqlite"]
postgresql_image = "postgres:16"
mysql_image = "mysql:8"
```

#### 検証内容

両モードに共通:
- 全 migration の順次 apply が成功
- Re-apply で ensure semantics により全て skip される（冪等性確認）
- `joryu verify` で意味的競合・改変検知が clean
- 方言ごとに最終 schema が論理的に等価（テーブル/カラム/nullable/PK 一致）

### 6.5 制約事項（明文化）

「同一ファイルで複数方言を動かす」ことを支援はするが、**ユーザの責任で互換性を保つ**領域は明示する：

- 方言固有のデータ型を直接書いた場合（L2/L3）
- DDL の挙動差（MySQL の暗黙 commit、SQLite の制約変更不可、Postgres の DDL transactional 等）
- データ移行関数内の SQL 文字列

---

## 7. 並走 PR と整合性検知

> **設計原則**: 並走 PR は **デフォルトで衝突しない**。同じスキーマ要素に触れた場合のみ検出する。
> Atlas の `atlas.sum` のように「全 PR を強制的に conflict させる」モデルは採用しない（並走 OK の要件に反する）。

3 つの独立した仕組みで安全性を担保する：

### 7.1 改変検知 — `joryu_migrations.checksum`（DB 側）

- §9.1 の `joryu_migrations.checksum` に適用時のファイルハッシュを保存
- `joryu apply` / `joryu verify` 時、**適用済み** ファイルのディスク上ハッシュが DB の値と異なれば **エラー**
- 「過去マイグレーションの後付け改変」を本番／CI で検知できる
- 未適用ファイルにはノーチェック（ローカルで書き直し放題）
- ディスク上の集中ファイルは不要 → PR で衝突しない

**他ライブラリでの採用状況**:
- 採用: Flyway, Liquibase, Prisma Migrate, Atlas（enterprise / schema-as-code 系）
- 非採用: Alembic, Django migrations, yoyo, goose, golang-migrate, Diesel, sqlx（スクリプト寄り古典系）

**実用上の価値**: 開発者が「typo 直すだけ」と過去のマイグレーションを編集する事故、rebase での意図しない変更、「適用後にちょっと修正して再適用」を CI で検知できる。Alembic でこの種の事故が起きると本番 DB と migration history が乖離して新環境構築時に違うスキーマになる、というデバッグ困難な障害になる。コストはほぼゼロ（ハッシュ計算と列 1 つ）。

**正当な編集が必要なとき**: `joryu repair <id>` で checksum を更新（こっそり編集を強制しない明示的な手段）。

### 7.2 意味的競合検知 — `joryu verify`（CI 側）

各 Operation は静的に「触れる対象」を `(table, column)` ペア単位で列挙できる。
`joryu verify` は **未適用マイグレーション全体** をスキャンし、**非可換な op の組** だけを ERROR として検出する。warning カテゴリは設けない（無視される運命なので）。

| 並走する 2 つの Op | 可換性 | 判定 |
|---|---|---|
| `add_column(users, A)` + `add_column(users, B)` (A≠B) | 可換 | **無音** |
| `add_column(users, A)` + `alter_column(users, B)` (A≠B) | 可換 | **無音** |
| `add_column(t1, ...)` + 任意の `t2` への変更 (t1≠t2) | 可換 | **無音** |
| `alter_column(users, email)` + `alter_column(users, email)` | × | **ERROR** |
| `add_column(users, X)` + `drop_column(users, X)` | × | **ERROR** |
| 何らかの `users` への変更 + `drop_table(users)` | × | **ERROR** |
| 何らかの `users.X` への変更 + `rename_column(users, X, Y)` | × | **ERROR** |
| 何らかの `users` への変更 + `rename_table(users, accounts)` | × | **ERROR** |
| 一方が `op.execute(raw)` / `op.run_python(...)` | 静的解析不能 | **無音**（人間レビュー責任） |

**設計判断**: 「通常運用でノイズにならない」ことを最優先。同じテーブルに別カラムを順次追加する普通の運用では何も発火しない。本当に危ない時だけ止まる。

### 7.3 順序の保証 — `depends_on`

- 順序を強制したいなら `migration.depends_on = ["先行 id", ...]` を書く
- 書かなければ「順序は問わない（タイブレークは timestamp 昇順）」という宣言
- 既存マイグレーションの **改変は禁止**（§7.1 の checksum で検出）。新規追加のみ
- 適用時は depends_on の DAG をトポロジカルソート

### 7.4 並走 PR のシナリオ別

| ケース | 結果 |
|---|---|
| 完全に独立な変更（A は users 追加、B は orders 追加） | 衝突なし、両方 merge して順次適用 |
| 同テーブルに別カラム追加 | **無音**、両方 merge して順次適用 |
| 同カラムを両方が変更 | `joryu verify` ERROR。片方を rebase して `depends_on` を付ける |
| 順序が重要（B が A 前提） | B の `depends_on` に A を書く |
| A が先に merge & 適用済み、その後 B を merge | B は未適用のまま残り、次回 `joryu apply` で実行 |
| 適用済みファイルを誰かが改変 | `joryu apply` / `verify` で checksum 不一致 → ERROR、必要なら `joryu repair` |

---

## 8. 自動生成（SQLAlchemy 差分）

### 8.1 コマンド

```
joryu generate "add users table"
joryu generate "..." --empty             # 空テンプレを作る
joryu generate "..." --against=db        # 現在の DB と比較
joryu generate "..." --against=replay    # 既存マイグレーションを再生して比較（CI 向け）
```

1. `joryu.toml` の `target` (例: `myapp.models:Base.metadata`) をロード
2. 比較対象スキーマと差分検出
3. **Operations のリストとして** Python ファイル生成

### 8.2 比較対象

| モード | 比較対象 | 用途 |
|---|---|---|
| `--against=db` (default) | 実 DB の現在スキーマ | dev DB あり |
| `--against=replay` | 既存マイグレーションをメモリ上で再生して得た仮想スキーマ | CI、DB なし生成 |

Alembic の `--autogenerate` は DB 必須で CI 不向き。joryu は両対応する。

### 8.3 生成結果

- 危険操作 (`drop_table`, `drop_column`, NOT NULL 追加) には `# WARNING: ...` コメント挿入
- 不可逆操作は別ファイルに分割提案
- データ移行が必要なケース（NOT NULL 追加で既存行を埋める等）は **空の `op.run_python` プレースホルダ** を入れて人間に書かせる

---

## 9. 実行モデル（中断・再開・大量データを正面から扱う）

> Alembic / Django 等の従来型は「migration は最後まで実行されるか、丸ごと rollback されるか」の二択しか持たない。
> 数千万行の `UPDATE` でこのモデルは破綻する（rollback が本体より重い、long-running transaction が他クエリを止める、MySQL は DDL を暗黙 commit してそもそも transactional 保証が崩れる）。
> joryu は **「途中で止まる」を一級市民として扱い、Operations を idempotent / resumable に設計** する。

### 9.1 設計の柱

1. **Ensure-style Operations**: `op.add_column` 等は「その状態であることを保証」する意味論。既にその名前・型で存在すれば no-op、不整合なら ERROR。再実行可能
2. **Per-step state tracking**: 「どの migration が完了したか」だけでなく「migration 内のどの step まで完了したか」を DB に記録
3. **3 つの transaction mode**: `per_migration` / `per_step` (default) / `none` から選択
4. **Batched data migrations**: 大量行更新は `op.batched_update` で batch + checkpoint
5. **Resume**: 中断したマイグレーションは `joryu apply` 再実行で続きから

### 9.2 状態テーブル

```sql
CREATE TABLE joryu_migrations (
    id              VARCHAR(120) PRIMARY KEY,
    checksum        VARCHAR(80)  NOT NULL,
    status          VARCHAR(20)  NOT NULL,     -- 'running' | 'applied' | 'failed'
    started_at      TIMESTAMP    NOT NULL,
    finished_at     TIMESTAMP    NULL,
    joryu_version   VARCHAR(20)  NOT NULL,
    dialect         VARCHAR(20)  NOT NULL
);

CREATE TABLE joryu_migration_steps (
    migration_id    VARCHAR(120) NOT NULL,
    step_index      INTEGER      NOT NULL,
    op_fingerprint  VARCHAR(80)  NOT NULL,     -- op の種類と引数のハッシュ
    status          VARCHAR(20)  NOT NULL,     -- 'running' | 'done' | 'failed'
    started_at      TIMESTAMP    NOT NULL,
    finished_at     TIMESTAMP    NULL,
    progress        TEXT         NULL,         -- batched_update のチェックポイント等
    PRIMARY KEY (migration_id, step_index)
);
```

- `status='applied'` の migration は二度と実行しない（Alembic と同じ）
- `status='running'` / `'failed'` は再開対象。`joryu_migration_steps` を見て done でない step から再開
- `op_fingerprint` は再開時にコード変更を検知（同じ index に違う op があれば ERROR）

### 9.3 Transaction Mode（三択）

`migration.transaction_mode` に応じて挙動が変わる：

| Mode | 挙動 | 適する場面 |
|---|---|---|
| `"per_migration"` | migration 全体を 1 トランザクションで包む | 小規模 DDL のみ。Postgres/SQLite で DDL を atomic にしたい時 |
| `"per_step"` (**default**) | 各 op を個別トランザクションで実行・commit | 大半の運用。途中で止まっても完了済み step は残る |
| `"none"` | トランザクション無し（暗黙 commit に任せる） | `CREATE INDEX CONCURRENTLY`、`VACUUM`、MySQL の重い DDL |

**なぜ `per_step` がデフォルトか**:
- MySQL は DDL が暗黙 commit するので `per_migration` は嘘になる。揃えて `per_step` の方が一貫性がある
- 数千万行への UPDATE を 1 トランザクションで包むのは現実的でない（rollback がコストになる、lock 保持時間が長い、binlog が肥大）
- step 単位 commit なら、N step 中 K step まで完了した状態が DB に残り、続きから再開できる
- 「冪等な op」と組み合わせると、再実行で完了済み step は skip され、止まった step から再開される

#### 9.3.1 方言別の transaction 実態

DDL のトランザクション挙動は方言で大きく異なる。joryu は実態を隠さず明示する：

| 方言 | DDL の atomic 性 | データ DML | `per_migration` の現実 | `per_step` の現実 |
|---|---|---|---|---|
| **PostgreSQL** | 完全に transactional（ほぼ全 DDL が rollback 可） | 通常通り | 期待通りに動く（CONCURRENTLY 系を除く） | 期待通り |
| **MySQL 8.0+ (InnoDB)** | 各 DDL は **暗黙 commit** (atomic DDL は per-statement のみ) | 通常通り | **嘘**: 最初の DDL で commit され、それ以降は rollback 不能 | DDL は実質 `none` 相当、データ DML は tx 内 |
| **MariaDB** | 同上（暗黙 commit） | 通常通り | 嘘 | MySQL と同じ |
| **SQLite** | transactional | 通常通り | 期待通り | 期待通り |

**実用上の指針**:
- **MySQL 環境では `per_migration` を選ぶ意味がほぼない**。明示的に選ぶと joryu は warning を出す
- MySQL で「複数 DDL を atomic に」したいケースは諦めるしかない（DB 側の制約）。代わりに **migration を細かく分ける** (1 migration = 1 DDL を志向) ことを推奨
- 大量データ UPDATE (`batched_update`) は方言問わず正常に動作する（DML の transaction は MySQL でも普通に効く）
- `per_step` がデフォルトなのは、MySQL でも Postgres でも「最低限 step 単位の進捗が残る」最小公倍数だから

これにより「動いてると思ってたら MySQL でだけ rollback されてなかった」事故を防ぐ。

### 9.4 Ensure-Style Operations（冪等性の中核）

各 op は **意図された状態 (desired state)** を宣言し、実行前に現状を確認する：

| Operation | 既に desired 状態 | 部分一致 (例: 名前あり、型違い) | 未存在 |
|---|---|---|---|
| `add_column(t, c, type)` | **skip** | ERROR（型不一致を勝手に変えない） | 作成 |
| `drop_column(t, c)` | （存在しないなら）**skip** | — | skip |
| `alter_column(t, c, ...)` | **skip** | ALTER 実行 | ERROR |
| `create_table(t, ...)` | **skip** | ERROR（カラム集合が違う） | 作成 |
| `drop_table(t)` | （存在しないなら）**skip** | — | skip |
| `create_index(name, ...)` | **skip** | ERROR（定義違い） | 作成 |
| `rename_column(t, old, new)` | new あり old なし → **skip** | 両方ありは ERROR | old → new |

これにより：
- 中断後の再実行で **既に成功した op は単に skip される**
- 手動で部分的に修正した DB に対しても、ensure 意味論で揃えにいく
- Alembic の「カラム既に存在エラーで死ぬ」問題が消える

**設計判断**: Alembic は「カラムを追加する」という命令を出す。joryu は「カラムが存在することを保証する」という意図を出す。差は小さく見えるが、運用ではこの差が再開可能性を生む。

「**勝手に状態を変えない**」も重要: 型違いを発見したら ERROR にする。silent な mutation は事故の元。型を変えたいなら明示的に `alter_column` を書く。

#### 9.4.1 型違い (mismatch) の典型パターン

実際に「現状と desired がズレる」のは以下のシナリオ。joryu はこれらを区別する：

| 発生源 | 例 | joryu の扱い |
|---|---|---|
| 手動 DDL drift | 誰かが psql で `phone VARCHAR(20)` を直接追加、migration は `t.Text` を期待 | **ERROR** |
| 環境間 drift | dev だけ hotfix で型を変更、migration を全環境で流すと prod だけ ERROR | **ERROR** |
| 並走 PR の型差 | PR1=`Text`, PR2=`Varchar(20)` で同名カラム追加 | 後者で **ERROR**（CI の `joryu verify` で先に検知できればなお良い） |
| 方言レンダリング差 | `t.Text` が SQLite では TEXT、MySQL では LONGTEXT | **誤検知しない**（type 抽象層で同一視） |
| migration 自体の編集 | 適用後に `Text → Varchar(255)` に書き換え | checksum 違反で **先に止まる** (§7.1)、ensure 到達せず |

#### 9.4.2 型違い時の挙動オプション

デフォルトは厳格 (silent mutation 一切無し) だが、必要に応じて局所的に緩められる：

```python
op.add_column("users", "phone", t.Text)                         # default: on_mismatch="error"
op.add_column("users", "phone", t.Text, on_mismatch="alter")    # 明示的に揃えに行く
op.add_column("users", "phone", t.Text, on_mismatch="skip")     # 違っても放置 (drift 受容)
```

- `"error"` (default): 不一致で止まる。安全側
- `"alter"`: 暗黙で `ALTER COLUMN` を実行して揃える。VARCHAR 縮小等の破壊的変更も実行されうるので **明示宣言した時のみ**
- `"skip"`: ログに warn を出すだけで進む。drift を許容する運用向け

migration 単位で一括設定したい場合は `migration.on_mismatch = "alter"` も可（非推奨だが可能）。

### 9.5 Resumable Data Migrations

> **設計判断**: joryu は `batched_update` のような汎用 batching API を **提供しない**。
> 理由:
> - WHERE 句と index の整合はライブラリが静的検証できない。「使えば安全」という誤解を生む
> - batching 戦略は data shape と index 構造に依存（cursor / range / ctid / SKIP LOCKED）。汎用 API は必ず leaky になる
> - batching loop 自体は小さい。ライブラリの真の価値は **checkpoint 永続化と resume** だけ
>
> 代わりに、**checkpoint インフラだけを提供** し、batching loop はユーザが書く。

#### 9.5.1 `op.run_python` の checkpoint API

`op.run_python(fn)` の `fn` は `(connection, dialect, checkpoint)` を受け取る。`checkpoint` は dict-like なオブジェクトで `joryu_migration_steps.progress` に永続化される：

```python
def upgrade():
    op.add_column("users", "email_normalized", t.Text, nullable=True)

    def backfill(conn, dialect, checkpoint):
        cursor = checkpoint.get("last_id", 0)
        while True:
            rows = conn.execute(text(
                "SELECT id, email FROM users "
                "WHERE id > :c AND email_normalized IS NULL "
                "ORDER BY id LIMIT 10000"
            ), {"c": cursor}).fetchall()
            if not rows:
                return
            conn.execute(text(
                "UPDATE users SET email_normalized = LOWER(email) "
                "WHERE id = ANY(:ids)"
            ), {"ids": [r.id for r in rows]})
            cursor = rows[-1].id
            checkpoint.set("last_id", cursor)   # ← commit + 永続化

    op.run_python(backfill)
    op.alter_column("users", "email_normalized", nullable=False)
```

`checkpoint.set(key, value)` の挙動:
- 値を `joryu_migration_steps.progress` に書き込み
- joryu 制御下で commit する（ユーザの batch UPDATE と同一 transaction or 直後の commit、§9.5.3 で詳述）
- 中断後の再実行時、`fn` が呼ばれる前に `checkpoint` には保存値がロードされている

#### 9.5.2 冪等性ガイドライン（`run_python` を書く側の責任）

`run_python` の中身は任意 Python なので、冪等性は **ユーザの責任**。joryu はこれを強制できない。実用ガイドライン：

| パターン | 良い例 | 悪い例 |
|---|---|---|
| **WHERE で「未処理行のみ」を識別** | `WHERE email_normalized IS NULL` | `UPDATE users SET email_normalized = LOWER(email)`（再実行で全行 rescan） |
| **追加処理は ON CONFLICT / WHERE NOT EXISTS** | `INSERT ... ON CONFLICT DO NOTHING` | 素の `INSERT`（再実行で重複） |
| **cursor で順序ある進行** | `id > :cursor ORDER BY id` | offset/limit のみ（中断時に飛ばし or 重複） |
| **副作用は DB 内のみ** | DB UPDATE のみ | HTTP 呼び出し、メール送信、外部 API（再実行で重複発火） |
| **計算は決定論的** | 同じ入力で同じ出力 | `random()`、現在時刻依存（再実行で結果ずれ） |
| **checkpoint を batch ごとに保存** | 1 batch ごとに `checkpoint.set` | ループ全体終了後にだけ set（中断時に進捗ロスト） |

**index リンクの注意**: `WHERE email_normalized IS NULL AND id > :cursor` のような条件は `(email_normalized, id)` または少なくとも `id` に index がないと毎回フルスキャンになる。`run_python` を書くときは **必要な index を先行 step で作る** ことを意識する：

```python
def upgrade():
    op.add_column("users", "email_normalized", t.Text, nullable=True)
    # 未処理行の絞り込み用 partial index（処理完了後に drop してもよい）
    op.create_index("tmp_users_unnormalized", "users", ["id"],
                    where="email_normalized IS NULL")
    op.run_python(backfill)
    op.drop_index("tmp_users_unnormalized")
    op.alter_column("users", "email_normalized", nullable=False)
```

#### 9.5.3 Checkpoint と batch の transaction 関係

`checkpoint.set()` は **ユーザの直前の DML と同一 transaction でコミット** される。具体的には：

```python
# ユーザのコード:
conn.execute(UPDATE...)        # batch UPDATE
checkpoint.set("last_id", x)   # ← ここで joryu が COMMIT を実行
```

これにより「UPDATE は走ったが checkpoint が保存されず、再実行で同じ行を再 UPDATE」という重複処理を防ぐ。冪等な WHERE があれば重複しても無害だが、保証層として用意する。

ユーザが明示的に transaction 制御したい場合は `transaction_mode = "none"` にして自分で `conn.commit()` + `checkpoint.set()` を呼ぶ：

```python
migration.transaction_mode = "none"

def backfill(conn, dialect, checkpoint):
    while ...:
        with conn.begin():
            conn.execute(UPDATE...)
            checkpoint.set("last_id", x)   # commit はこの with 抜けで
```

### 9.6 適用アルゴリズム (`joryu apply`)

1. アドバイザリロック取得（§9.7）
2. `joryu_migrations` の `status='running'/'failed'` を **resume 対象** として認識
3. 適用済み (`status='applied'`) と現在の方言で除外したものを除く、未適用 migration 集合を作る
4. `depends_on` で DAG を構築、トポロジカルソート（同位は timestamp 昇順）
5. resume + 未適用を順番に処理:
   - 既存 `joryu_migrations` 行が無ければ INSERT (`status='running'`)、checksum 確認
   - `transaction_mode` に応じて transaction を開く
   - 各 step を順に実行:
     - `joryu_migration_steps` を見て `done` なら skip
     - `running` 中の batched op なら `progress` から再開
     - 未処理なら `INSERT step (status='running')` → op 実行 → `UPDATE step (status='done')`
     - 失敗時は `UPDATE step (status='failed')` → migration を `failed` に → 停止
   - 全 step 完了で `UPDATE migrations (status='applied', finished_at=...)`
6. lock 解放

### 9.7 アドバイザリロック

複数プロセス同時実行を防ぐ：
- Postgres: `pg_advisory_lock`
- MySQL: `GET_LOCK`
- SQLite: `BEGIN EXCLUSIVE` または file lock

### 9.8 失敗時の運用フロー

```
joryu apply
  → migration X の step 3 (batched_update) で 50% 進んだところで OOM kill
  → status='failed', steps[3].status='failed', steps[3].progress='{"cursor": 5012345}'

調査 (DB の負荷確認、原因特定)

joryu status              # X が failed であることを表示、進捗も
joryu apply --resume      # step 1, 2 は skip, step 3 を cursor から続行
```

`--resume` を明示しない apply はデフォルトで resume する（明示的に止めたいときは `--no-resume`）。

`joryu mark <id> --as=applied` や `joryu mark <id> --as=pending` で手動状態修正も可能（最後の手段）。

### 9.9 DDL 多段失敗（典型シナリオ）

データ移行ではなく **複数 DDL の途中で失敗** するパターン。実運用で頻出する：

```python
def upgrade():
    op.add_column("users", "col1", t.Text)
    op.add_column("users", "col2", t.Text)
    op.create_index("idx_col2", "users", ["col2"])   # ← 失敗（同名 index が既存等）
    op.add_column("users", "col3", t.Text)
```

per_step モードでの動作:

```
Step 1: ALTER TABLE ADD col1   → BEGIN; ...; COMMIT;  ✓
Step 2: ALTER TABLE ADD col2   → BEGIN; ...; COMMIT;  ✓
Step 3: CREATE INDEX idx_col2  → 失敗 → ROLLBACK (Postgres) / 暗黙状態 (MySQL)
Step 4: ALTER TABLE ADD col3   → 未実行で停止
```

**実 DB の状態**: col1/col2 あり、idx_col2 なし、col3 なし。
**`joryu_migration_steps`**: 1=done, 2=done, 3=failed, 4=pending。

復旧パスは 3 通り:

| パス | 操作 | 結果 |
|---|---|---|
| **A. 原因除去して継続** | 既存 idx_col2 を drop、`joryu apply` 再実行 | step 1,2 は ensure semantics で skip、step 3 retry → step 4 実行 |
| **B. ファイル修正** | index 名を `idx_col2_v2` に変えて push、`joryu apply` | failed 中なので checksum 変更可（§9.10）、step 3 が新 fingerprint で実行 → step 4 |
| **C. 放棄** | `joryu mark <id> --as=pending` + col1/col2 を手 drop、または `--as=applied` で完了扱い | 後者は嘘だが、後続 migration は ensure semantics で現状認識して整合 |

### 9.10 Failed 中の checksum / op_fingerprint ポリシー

| 状態 | ファイル checksum 変更 | step の op_fingerprint 変更 |
|---|---|---|
| `applied` | **禁止** (§7.1)、`joryu repair` 必須 | — |
| `failed` | **許可**（修正して再 push が通常運用） | 失敗 step 以前: 一致必須。失敗 step 以降: 変更可、新内容で実行 |
| `running` | 通常は起きない（同時実行は advisory lock で防止） | — |
| 未実行 | 自由 | — |

「失敗 step 以前の fingerprint 変更を ERROR にする」のは大事。step 1 を勝手に書き換えて再実行すると、DB の col1 が既に存在するため ensure で skip → 変更が反映されない、という silent な事故になる。

### 9.11 Failed migration がある時の後続 migration

migration X が `failed` のとき、別の migration Y を実行するか？

- **デフォルト: halt**。failed が 1 つでもあれば `joryu apply` は何もせず終了し、`joryu status` を見るよう促す
- **明示許可**: `joryu apply --continue-past-failed` で、failed に depends_on で繋がっていない migration のみ実行
- 理由: 失敗は例外イベント。気付かないうちに別 migration が動いて状態が複雑化することを避ける

### 9.12 Downgrade の現実と AI フレンドリーなアプローチ

> Downgrade は「逆順に消す」だけでは動かない。FK・index・依存関係を考慮した順序が必要で、Alembic の auto 生成 down は実運用で **そのままでは動かない** ことが多い。
> joryu は本番 down を非推奨とした上で、**dev 用 down を AI が完成させやすい構造** にする。

#### 9.12.1 なぜ素朴な逆順が動かないか

典型的な失敗：
- `create_index` の逆 `drop_index` が、その index が FK に参照されていて drop 不可
- `create_table` の逆 `drop_table` が、別テーブルからの FK で参照されて drop 不可
- `add_column NOT NULL` の逆 `drop_column` で、その列を参照する view・FK・index が残っている
- データ移行 (`run_python` で値を変換) の逆処理が原理的に書けない（情報損失）

Alembic の auto-generated downgrade はこれらを考慮せず素朴な逆順を吐くので、実運用では人間が書き直すか、down を諦めることになる。

#### 9.12.2 `JORYU-DOWN-HINT:` 構造化コメント仕様（v1 固定）

> **言語ポリシー**: HINT のフィールド名・enum 値はすべて **英語固定** とする（CLAUDE.md の言語ポリシーに従う）。AI ツールが安定して読み取れることが目的。

`joryu generate` は upgrade と同時に **downgrade のスケルトン** と **構造化ヒント** を出力する。完成は AI または人間に任せる。HINT は YAML 風 key-value で書かれ、`JORYU-DOWN-HINT:` プレフィクスを持つ：

```python
def downgrade():
    # JORYU-DOWN-HINT: schema-impact:
    #   - drop_column: users.email_normalized
    #   - drop_index: idx_users_email_normalized
    # JORYU-DOWN-HINT: cross-references: []
    # JORYU-DOWN-HINT: data-loss-risk: high
    # JORYU-DOWN-HINT: data-loss-reason: column data cannot be reconstructed from remaining schema
    # JORYU-DOWN-HINT: order-constraint:
    #   - drop_index before drop_column
    # JORYU-DOWN-HINT: requires-app-knowledge: false
    # JORYU-DOWN-HINT: completion-status: stub

    op.drop_index("idx_users_email_normalized", "users")
    op.drop_column("users", "email_normalized")
```

**Field vocabulary (v1, 固定)**:

| Field | Type | Description | Required |
|---|---|---|---|
| `schema-impact` | list of `<verb>: <target>` | What the downgrade removes/restores. Verbs: `drop_table`, `drop_column`, `drop_index`, `drop_constraint`, `drop_view`, `drop_enum`, `restore_column_type`, `restore_nullable`, `restore_default`, `restore_data` | yes |
| `cross-references` | list of `<kind>: <name> -> <target>` | Other DB objects in the current schema that reference what we're dropping. Kinds: `foreign_key`, `index`, `view`, `materialized_view`, `trigger`, `policy`. Empty list `[]` if none detected | yes |
| `data-loss-risk` | enum: `none` / `low` / `medium` / `high` / `irreversible` | Whether running downgrade loses data. `irreversible` means the downgrade cannot recover the original state even with the migration code | yes |
| `data-loss-reason` | string | Free-text explanation. Required when `data-loss-risk >= medium` | conditional |
| `order-constraint` | list of `<a> before <b>` | Operations that must run in a specific order within the downgrade | no |
| `requires-app-knowledge` | bool | True if the downgrade depends on facts not derivable from schema alone (e.g., business invariants, external system state) | yes |
| `app-knowledge-needed` | list of strings | Specific unknowns the human/AI must resolve. Required when `requires-app-knowledge: true` | conditional |
| `completion-status` | enum: `stub` / `partial` / `complete` / `manual-review-required` | Set by generator (`stub`), updated by AI/human as they edit. CI can warn on `stub` if `joryu down` is part of the workflow | yes |
| `manual-steps` | list of strings | Non-SQL steps (e.g., "restart application servers", "purge cache") that must accompany the downgrade | no |

**Verb vocabulary for `schema-impact`**:
- `drop_table: <table>` / `drop_column: <table>.<column>` / `drop_index: <name>` / `drop_constraint: <name>` / `drop_view: <name>` / `drop_enum: <name>`
- `restore_column_type: <table>.<column> from <new_type> to <old_type>`
- `restore_nullable: <table>.<column>` / `restore_default: <table>.<column>`
- `restore_data: <table>.<column>` (when downgrade attempts to undo a data transformation)

**Cross-reference kinds**:
- `foreign_key: <fk_name> -> <referenced_table>.<col>`
- `index: <index_name> -> <table>.<columns>`
- `view: <view_name> -> <table>.<col>`
- `materialized_view: <name> -> ...`
- `trigger: <name> -> <table>`
- `policy: <name> -> <table>` (RLS)

**Generator の責任**:
- `schema-impact`, `order-constraint`: upgrade の op から機械的に導出
- `cross-references`: 生成時点の DB スキーマ（or `--against=replay` で再構築した仮想スキーマ）をスキャンして検出
- `data-loss-risk`: heuristic — `drop_column` / `drop_table` / `run_python` を含めば `high`、純粋な index/constraint drop は `low`、何もデータに触れない構造変更は `none`
- `requires-app-knowledge`: `run_python` / `op.execute(raw_sql)` を含むなら `true`

**AI の責任** (downgrade を完成させるとき):
- `schema-impact` を読んで drop 順序を決定（`order-constraint` を尊重）
- `cross-references` を読んで先行で drop すべき対象を追加
- `data-loss-risk: irreversible` の場合は downgrade をコメントアウトして人間に判断を委ねる
- 完成したら `completion-status: complete` に更新

#### 9.12.3 AI が利用するインプット

AI ツール（Claude Code 等）が downgrade を補完するとき、以下を組み合わせる：

1. `JORYU-DOWN-HINT:` の構造化フィールド
2. SQLAlchemy モデル定義（`models/` 配下、FK・関係）
3. `joryu schema-snapshot --format=json` が出力する現在 DB schema
4. 同 migration の `upgrade()` 本体

**AI 向け標準プロンプト** (README で配布):

```
Complete the downgrade() in this migration file. Use the JORYU-DOWN-HINT: comments
as the source of truth for what must be undone. Cross-check with models/*.py for
FK relationships. If data-loss-risk is "irreversible", comment out the downgrade
body and add a clear note explaining why. Update completion-status when done.
Do not modify the upgrade() function.
```

#### 9.12.4 down の位置づけ（明文化）

| 環境 | down の使用 | 推奨 |
|---|---|---|
| ローカル dev | OK（試行錯誤・branch 切り替え） | 通常運用 |
| ステージング | 限定的（FK 等で破綻しやすい） | 慎重に |
| 本番 | **非推奨** | PITR / バックアップで戻す |

`joryu down` は **--allow-prod なしでは production-like な接続を拒否** する（DSN や設定で本番判定）。事故防止。

### 9.13 過去スキーマの参照（履歴リプレイ）

Django と同じく、Operations が宣言的なので joryu は **任意の時点のスキーマを再構築可能**。
データ移行関数内で「過去のモデル形」を参照したい場合：

```python
def upgrade():
    OldUser = op.historical_model("users")    # 当該時点のスキーマ
    def backfill(conn, dialect, checkpoint):
        for row in conn.execute(select(OldUser).where(OldUser.c.email.is_(None))):
            ...
    op.run_python(backfill)
```

これにより「アプリの `models.User` を import すると現状最新形になっていて壊れる」という Alembic でよくある事故を回避できる。

#### 9.13.1 リプレイ方式: Operations replay (snapshot ファイルなし)

joryu は **Operations replay 方式** を採用。snapshot JSON ファイル（Drizzle 流）は持たない：
- `joryu generate` 時、対象 migration の前までの全 Operations をメモリ上の virtual schema に順次適用して「現在状態」を構築
- そこに対して新 migration の op を適用して比較・差分を出す
- snapshot ファイルを増やさないので PR 衝突源にならない、git diff がノイジーにならない

#### 9.13.2 Raw SQL / run_python の扱い

`op.execute(raw_sql)` や `op.run_python(...)` は中身が不透明で virtual schema に自動反映できない。これらを含む migration では、ユーザが **declarative ヒント** を併記する:

```python
def upgrade():
    op.execute("CREATE EXTENSION IF NOT EXISTS pgcrypto")
    op.declare_schema_change(extension_added="pgcrypto")     # replay 用ヒント

    op.execute("ALTER TABLE users ADD COLUMN api_key TEXT GENERATED ALWAYS AS (...) STORED")
    op.declare_schema_change(
        column_added=("users", "api_key", t.Text, {"nullable": False, "generated": True})
    )

    def transform(conn, dialect, checkpoint):
        conn.execute(text("UPDATE users SET ..."))
    op.run_python(transform)
    # データ変更は schema に影響しないので declare 不要
```

**ヒントが無い場合**:
- replay は当該 migration を「opaque box」として扱い、以降の `historical_model()` はその migration より前の状態に固定される
- `joryu generate` は warning を出す: "historical schema may be stale after migration X due to undeclared raw SQL"
- ただしエラーにはしない（最も多くの run_python はデータ変更のみで schema 影響なし）

`op.declare_schema_change(...)` の引数は `op.add_column` 等と同じ語彙のサブセット（`column_added`, `column_dropped`, `index_added`, `extension_added`, `table_added`, `table_dropped` など）。

### 9.14 ユーザ定義 step (`op.step`)

> Migration は「op を順に並べた step 列」だが、ユーザが任意の処理を 1 step として登録できる。
> 完了判定をカスタマイズでき、resume / pause も組み込みの step と同じ扱いになる。

#### 9.14.1 基本形

```python
@op.step
def wait_for_replication(conn, dialect, checkpoint):
    """前段の DDL がレプリカに反映されるまで待つカスタム step。"""
    if checkpoint.get("ready"):
        return True

    lag = conn.execute(text("SHOW SLAVE STATUS")).scalar()
    if lag < 1:
        checkpoint.set("ready", True)
        return True
    raise op.PauseStep(f"replica lag={lag}s, retry later")
```

`op.step(fn)` は decorator または直接呼び出しで使える:
```python
op.step(my_func)              # 関数を渡す
op.step(my_func, name="...")  # name を上書き
```

#### 9.14.2 シグネチャと返り値

シグネチャ: `fn(conn, dialect, checkpoint) -> bool | None`
- 同期関数 / `async def` の両方サポート（`inspect.iscoroutinefunction` で判別、必要に応じて `asyncio.run`）
- 引数を不要なら `*args, **kwargs` で受けて無視するか、`fn()` のような無引数定義も許可

返り値の意味:

| 返り値 | 意味 | step status |
|---|---|---|
| `True` / `None` (return 省略) | 完了 | `done` |
| `False` | 完了ではないが ERROR でもない（再実行待ち） | `pending` のまま、次回 apply で続行 |
| 例外 `op.PauseStep(reason)` | 外部要因で待機（migration 全体を停止、再実行で再開） | step `pending`、migration `paused` |
| 例外 `op.SkipStep(reason)` | この step は skip して次へ | step `skipped` |
| その他例外 | 失敗 | step `failed`、migration `failed` |

#### 9.14.3 SQL セッション

- `conn` は引数で渡される（sync: SQLAlchemy `Connection`、async: `AsyncConnection`）
- 自分で engine を作りたい場合は `op.get_engine()` でアクセス可能（同じ DB 接続情報）
- transaction 制御は migration の `transaction_mode` に従う。step 内で明示的に transaction を切りたければ `with conn.begin():` を書く

#### 9.14.4 通常の op との違い

| 項目 | 通常 op (`add_column` 等) | `op.step` |
|---|---|---|
| 静的解析対象 (`joryu verify`) | ◯ | ×（黒箱扱い、`run_python` と同じ） |
| 自動 schema 影響反映 | ◯ | ×（`op.declare_schema_change` で明示） |
| ensure semantics | ◯ | 関数内でユーザ実装 |
| 完了判定 | 例外なく終わったら done | 返り値で制御 |
| `PauseStep` 対応 | × | ◯ |

### 9.15 Checkpoint API 詳細仕様

#### 9.15.1 メソッド

```python
checkpoint.get(key, default=None)         # 値取得（None safe）
checkpoint.set(key, value)                # 単一 key 更新（atomic UPDATE + commit）
checkpoint.update({k1: v1, k2: v2})       # 複数 key を 1 UPDATE で atomic 更新
checkpoint.clear()                        # progress 全消去（再開時に最初から）
checkpoint.snapshot()                     # 現在の dict 全体（read-only コピー）
```

#### 9.15.2 永続化

- `joryu_migration_steps.progress` 列に **JSON テキスト** として保存
- `set` / `update` は内部で `BEGIN; UPDATE joryu_migration_steps SET progress=... WHERE ...; COMMIT;`
- 並行プロセスは advisory lock (§9.7) で排除されるので single writer

#### 9.15.3 Transaction 関係

`set` / `update` はユーザの直前の DML と **同一 transaction で commit**:
```python
conn.execute(UPDATE users SET ...)        # ユーザ DML
checkpoint.set("cursor", x)               # ← この時点で COMMIT
```
これにより「DML は走ったが checkpoint 未保存で重複処理」事故を防ぐ。

`transaction_mode = "none"` の場合はユーザが自分で transaction 制御:
```python
with conn.begin():
    conn.execute(UPDATE...)
    checkpoint.set("cursor", x)            # with 抜けで commit
```

#### 9.15.4 シリアライズ可能型

JSON 互換のみ:
- `str`, `int`, `float`, `bool`, `list`, `dict`, `None`
- `datetime` / `date` は `isoformat()` 文字列に自動変換
- `Decimal` は文字列化
- それ以外（カスタムオブジェクト等）は ERROR

カスタム encoder/decoder hook (`migration.checkpoint_codec`) は v1.1+ で検討。

#### 9.15.5 サイズと運用

- soft limit: 1 MB／step。超えたら warn
- 大量データを保持したいケースは専用テーブルを作る方が筋が良い（cursor のような「次に処理すべき位置」だけを checkpoint に置くべき）
- 読込は初回 `get` で lazy load、以降はメモリキャッシュ

### 9.16 中途半端な失敗時のインタラクティブ復旧

step 2 が `run_python` で 50% 完了 → step 3 で ERROR、のような中途半端な状態では、`joryu apply --resume` で再実行する際に **ユーザに方針を尋ねる**:

```
$ joryu apply
Migration 20260620T030000_backfill_email_normalized is in failed state.

  ✓ step 1 add_column(users.email_normalized)  done
  ⚠ step 2 run_python(backfill)                done at last_id=15003421 (30%)
  ✗ step 3 alter_column(... NOT NULL)          failed: NotNullViolation

How would you like to proceed?
  [1] Resume from step 3 (re-run only failed/pending steps)
  [2] Restart from step 2 (re-run from a chosen step)  ← prompt for step number
  [3] Restart from step 1 (full restart, clears all checkpoints)
  [4] Skip step 3 and continue (mark as skipped)
  [5] Abort (do nothing)

Choose [1-5]:
```

- デフォルトは `[1]` (resume)
- `--non-interactive --on-failure=resume|restart|abort` で CI/自動化で挙動指定可能
- `[3]` 全消去はチェックポイントを破棄、ensure semantics の op は再実行で skip されるが run_python は最初から走る → ユーザの WHERE 句が冪等であれば安全
- `[4]` skip はメタ情報として記録され、後で `joryu status` で見える

### 9.17 本番判定とローカル安全策

`joryu down` のような破壊的コマンドを誤って本番で実行しないための判定方針:

#### 9.17.1 自動判定 (heuristic)

DB 接続文字列に以下のいずれかを含むなら **ローカル**と推定:
- `localhost` / `127.0.0.1` / `::1`
- ホスト名に `.local` / `local-` / `-local` を含む
- ファイルパス（SQLite）
- `host.docker.internal`
- 環境変数 `JORYU_ENV` が `local` / `dev` / `test`

それ以外は **production-like** と判定し、`joryu down` は `--allow-prod` 必須。

#### 9.17.2 明示宣言

設定ファイルや init API で本番宣言:
```python
import joryu
joryu.set_environment("production")    # 明示宣言、heuristic を上書き
```
または `joryu.toml`:
```toml
[joryu]
environment = "production"            # local / staging / production
```

`environment != "local"` のとき:
- `joryu down`: `--allow-prod` 必須
- `joryu apply`: `--continue-past-failed` がより慎重なプロンプトを出す
- `joryu mark`: 確認プロンプト

#### 9.17.3 ドキュメント方針

manual に「本番環境の判定方法」専用セクションを置き、heuristic の限界、明示宣言の推奨、CI/CD への組み込み方を網羅する。誤判定が事故にも利便性低下にもなるため、判定の透明性が重要。

---

## 10. CLI

```
joryu init                              # 初期セットアップ
joryu generate <slug> [--empty] [--against=db|replay]
joryu apply [--target=<id>] [--dry-run] [--no-resume] [--continue-past-failed]
                                        #   [--non-interactive --on-failure=resume|restart|abort]
                                        #   [--retry-paused --retry-interval=30s]
joryu status                            # 適用済み/未適用/失敗/paused 一覧、step 進捗付き
joryu down [--steps=N | --to=<id>] [--allow-prod]   # dev 専用、production-like 接続は明示許可必須
joryu schema-snapshot [--format=json|sql]           # AI 補助用に現在 schema を出力
joryu verify                            # CI 向け：改変検知 + 意味的競合検知 (§7)
joryu repair <id>                       # 適用済み migration の checksum を更新
joryu mark <id> --as=applied|pending|failed       # 手動状態修正（最後の手段）
joryu mark <id>.<step> --as=done|pending|skipped  # 個別 step の状態修正
joryu show <id>                         # 詳細表示
joryu explain <id>                      # 自然言語化（AI 補助）
joryu test [--unit | --integration] [--dialects=postgresql,mysql,sqlite]
joryu import alembic --alembic-dir=./alembic --output-dir=./migrations
                                        #   [--migrate-state] [--drop-alembic-table] [--report]
```

---

## 11. 設定ファイル `joryu.toml`

```toml
[joryu]
migrations_dir = "migrations"

[metadata]
target = "myapp.models:Base.metadata"

[database]
url = "env:DATABASE_URL"

[generate]
include_schemas = ["public"]
exclude_tables  = ["spatial_ref_sys"]

[dialects]
# 開発時にテストしたい方言一覧（joryu test のデフォルト）
test_targets = ["postgresql", "mysql", "sqlite"]
```

---

## 12. Alembic / Django との比較

| 項目 | Alembic | Django | joryu |
|---|---|---|---|
| 主役言語 | Python | Python | Python |
| マイグレーション ID | random hex | per-app sequence | UTC timestamp |
| 順序モデル | linked-list (`down_revision`) | per-app sequence + cross-app deps | DAG (`depends_on` set) |
| 並走 PR | `alembic merge` 儀式必要 | 番号衝突 → `makemigrations --merge` | 独立変更は merge OK、同オブジェクト変更のみ `joryu verify` で検知 |
| 自動生成 | DB 必須 | アプリのモデルから | モデルから (DB なしも可) |
| 履歴リプレイ | 不可（op 命令型） | 可（Operations 宣言的） | 可（Operations 宣言的） |
| データ移行 | 同居可 | `RunPython` で同居 | `op.run_python` で同居 |
| 多方言 | 手書きで分岐 | Django ORM 経由で限定的に吸収 | 三層モデルで明示支援 |
| escape hatch | `op.execute(text(...))` | `RunSQL("...")` | `op.execute("..."|dict)` 一級市民 |
| 整合性検証 | なし | なし | `joryu verify`（DB checksum + Operations 静的解析）|
| forward-only | 思想なし | 実質そう | 明文化 |
| **op の意味論** | 命令的（既存ありで死ぬ） | 命令的 | **Ensure-style（冪等）** |
| **中断・再開** | 不可（手で DB 戻して再実行） | 同じ | **step 単位 resume、batched op は cursor 保存** |
| **transaction** | 1 migration = 1 tx 固定 | DB 任せ | `per_migration` / `per_step` (default) / `none` |
| **大量データ更新** | 自分で書く（resume なし） | 自分で書く（resume なし） | ユーザが書いた loop に **checkpoint API** で resume 機能だけ提供 |

---

## 13. Alembic からの移行ツール

> joryu は **v1 リリース時から Alembic 移行ツール (`joryu import alembic`) を提供** する。
> 既存 Alembic ユーザを獲得するため、移行コストを最小化する。

### 13.1 アプローチ

```
joryu import alembic --alembic-dir=./alembic --output-dir=./migrations
```

#### Phase 1: 構造変換（自動）
- `versions/*.py` を全スキャン
- `revision` / `down_revision` の linked-list を `depends_on` の DAG に変換
- ファイル名を `<timestamp>_<slug>.py` に正規化（ランダム hex → timestamp に推定変換、`alembic history` を解析して順序を推測 + 元の hex を `tags` に保持）
- `op.add_column(..., sa.Column(name, type, ...))` → `op.add_column(..., name, type, ...)` への引数構造変換
- `op.execute(text("..."))` → `op.execute("...")`
- `upgrade()` / `downgrade()` を `@joryu.migration` / `@joryu.downgrade` でラップ

#### Phase 2: ヒューリスティクス変換（半自動、確認プロンプト付き）
- `op.batch_alter_table(...)` → `with op.batch(...)`
- `if op.get_bind().dialect.name == ...` → `op.execute({dialect: ...})` への書き換え提案
- `op.execute("CONCURRENTLY ...")` を検出 → `transaction_mode="none"` の付与提案
- データ移行 (`op.execute("UPDATE ...")` の長い形) → `op.run_python` への抽出提案

#### Phase 3: 手動レビュー領域（コメントで残す）
- 元コードに残ったまま `# JORYU-IMPORT-TODO: ...` コメントを付与
- `joryu import alembic --report` で全 TODO 一覧を出力
- `op.create_table` 内の方言固有 kwarg (`postgresql_using=`, `mysql_engine=`) は元のまま保持し、joryu 側で互換 wrapper を提供

### 13.2 状態テーブルの引き継ぎ

```
joryu import alembic --migrate-state
```

- 既存 `alembic_version` テーブルから現在 revision を読み取り
- 変換後の `joryu_migrations` に、対応する全 migration id を「適用済み」として INSERT
- `alembic_version` テーブルは保持（ロールバック用）。`joryu import alembic --drop-alembic-table` で明示削除

### 13.3 並走運用 (gradual migration)

「Alembic と joryu を一定期間並走させたい」というニーズに対応:
- `joryu apply` は `alembic_version` を読まない（独立）
- 移行期は両方の CLI を順番に実行する想定
- 推奨フロー: import → 全 PR で Alembic 凍結 → joryu のみで運用開始 → `alembic_version` テーブル削除

### 13.4 制約

- `op.bulk_insert(...)` のような Alembic 固有 op は同等機能を提供しないので `op.run_python` への手動書き換え必須
- branch labels や複数 head は単一 DAG に flatten される（merge revision は `depends_on` で複数親を持つ migration として表現）
- カスタム migration template は変換対象外

---

## 14. オープン課題

### 解決済み（記録）

- ✅ **migration の宣言スタイル**: `@joryu.migration(...)` デコレータ採用 (§3.2)
- ✅ **`op.execute(dict)` のキー正規化**: `postgresql`/`mysql`/`mariadb`/`sqlite` を独立 key、`default` あり、文字列単体も可 (§6.1)
- ✅ **`op.batch` の自動発動**: 明示要求とする。`generate` 時のコード生成では自動ラップ (§4.4)
- ✅ **履歴リプレイの粒度**: Operations replay 主体、`run_python` / raw SQL は `op.declare_schema_change()` ヒント併記 (§9.13)
- ✅ **複数方言の論理 group**: `group=` パラメータで束ねる (§6.1 Layer 3)
- ✅ **生成 AI 直接モード**: 非搭載ポリシーを永続化、公式 skill を提供 (§1 非ゴール)
- ✅ **Alembic 移行ツール**: v1 から提供 (§13)
- ✅ **`joryu test` の実装**: unit (in-memory) + integration (testcontainers) の二層 (§6.4)
- ✅ **中途半端な失敗の扱い**: インタラクティブな 5 択プロンプト (§9.16)
- ✅ **本番判定**: heuristic + 明示宣言の二段構え (§9.17)
- ✅ **checkpoint API 詳細**: §9.15 で完全仕様化
- ✅ **op.step ユーザ定義 step**: §9.14 で仕様化

### 残課題

1. **`op.declare_schema_change` の語彙完全性**: §9.13.2 で示した語彙がすべての raw SQL 影響をカバーできるか、漏れがあれば追加
2. **`joryu import alembic` のエッジケース**: branch labels、複数 head、`bulk_insert`、カスタム migration template の扱い詳細
3. **integration test の CI 推奨設定**: nightly で動かす GitHub Actions / GitLab CI のテンプレートを公式提供すべきか
4. **`op.step` の async runtime**: 既存 event loop の中で動く場合の挙動。`anyio` で抽象化するか asyncio 直接か

### 後日まとめて対応
- **SPEC.md の章立て整理 + 目次追加** → 英語化のタイミングで一斉に実施
- **SPEC.md → English 翻訳** → 設計凍結後に全体を英訳、ルート CLAUDE.md の言語ポリシー (SPEC 例外) を解除

### 解決済み（追加）

- ✅ **Python バージョン**: 3.11+ (§1 動作環境)
- ✅ **AI skill 配布**: repository 内 `.claude/skills/joryu/` 同梱 (§1 公式 AI skill 配布)

---

## 付録 A: マイグレーション例

### A.1 単純な DDL（方言自動）

```python
"""Add users table."""
import joryu
from joryu import op, types as t

@joryu.migration(id="20260514T093000_add_users")
def upgrade():
    op.create_table(
        "users",
        op.column("id",    t.BigInt, primary_key=True, autoincrement=True),
        op.column("email", t.Text,   nullable=False, unique=True),
        op.column("created_at", t.Timestamp, server_default=op.func.now()),
    )

@joryu.downgrade
def downgrade():
    op.drop_table("users")
```

### A.2 データ移行を含む

```python
"""Normalize emails and enforce NOT NULL."""
import joryu
from joryu import op, types as t
from sqlalchemy import text

@joryu.migration(
    id="20260517T140000_normalize_emails",
    depends_on=["20260514T093000_add_users"],
)
def upgrade():
    op.add_column("users", "email_normalized", t.Text)

    def backfill(conn, dialect, checkpoint):
        conn.execute(text("UPDATE users SET email_normalized = LOWER(email) WHERE email_normalized IS NULL"))

    op.run_python(backfill)
    op.alter_column("users", "email_normalized", nullable=False)
```

### A.3 方言別 SQL（Layer 2）

```python
"""Add GIN index on settings JSON column."""
import joryu
from joryu import op

@joryu.migration(
    id="20260520T100000_settings_index",
    transaction_mode="none",     # CREATE INDEX CONCURRENTLY を含むため
)
def upgrade():
    op.execute({
        "postgresql": "CREATE INDEX CONCURRENTLY users_settings_idx ON users USING GIN (settings)",
        "mysql":      "CREATE INDEX users_settings_idx ON users ((CAST(settings AS CHAR(255))))",
        "sqlite":     "CREATE INDEX users_settings_idx ON users (json_extract(settings, '$'))",
    })

@joryu.downgrade
def downgrade():
    op.execute("DROP INDEX users_settings_idx")
```

### A.4 方言限定ファイル + group（Layer 3）

```python
"""Postgres-only: enable pgcrypto and create RLS policy."""
import joryu
from joryu import op

@joryu.migration(
    id="20260525T093000_pg_rls",
    dialects=["postgresql"],
    group="20260525_rls",
    depends_on=["20260514T093000_add_users"],
)
def upgrade():
    op.execute("CREATE EXTENSION IF NOT EXISTS pgcrypto")
    op.execute("ALTER TABLE users ENABLE ROW LEVEL SECURITY")
    op.execute("CREATE POLICY users_self ON users USING (id = current_setting('app.user_id')::bigint)")
```

### A.5 SQLite 対応の制約変更（batch）

```python
"""Make users.email NOT NULL."""
import joryu
from joryu import op

@joryu.migration(id="20260601T120000_email_not_null")
def upgrade():
    with op.batch("users") as batch:
        batch.alter_column("email", nullable=False)
    # Postgres/MySQL: ALTER TABLE users ALTER COLUMN email SET NOT NULL
    # SQLite: 新テーブル作成 → データコピー → rename
```

### A.6 SQLAlchemy モデルから直接

```python
"""Create orders table from model."""
import joryu
from joryu import op
from myapp.models import Order

@joryu.migration(id="20260605T140000_add_orders")
def upgrade():
    op.create_table_from_model(Order)

@joryu.downgrade
def downgrade():
    op.drop_table("orders")
```

### A.7 大量データ更新（resumable, ユーザ実装の batching loop）

数千万行への `UPDATE` を本番で安全に流す例。joryu は checkpoint インフラだけ提供、batching loop はユーザが書く。中断しても保存された cursor から再開できる。

```python
"""Backfill users.email_normalized for 50M rows."""
import joryu
from joryu import op, types as t
from sqlalchemy import text

BATCH_SIZE = 10_000

@joryu.migration(
    id="20260620T030000_backfill_email_normalized",
    depends_on=["20260619T120000_add_email_normalized_col"],
    transaction_mode="per_step",   # default だが明示
)
def upgrade():
    # Step 1: ensure semantics — 既存なら skip
    op.add_column("users", "email_normalized", t.Text, nullable=True)

    # Step 2: 未処理行絞り込み用の partial index（処理後に drop）
    op.create_index("tmp_users_unnormalized", "users", ["id"],
                    where="email_normalized IS NULL")

    # Step 3: ユーザが書いた batching loop。joryu は checkpoint を永続化
    def backfill(conn, dialect, checkpoint):
        cursor = checkpoint.get("last_id", 0)
        while True:
            rows = conn.execute(text(
                "SELECT id FROM users "
                "WHERE id > :c AND email_normalized IS NULL "
                "ORDER BY id LIMIT :n"
            ), {"c": cursor, "n": BATCH_SIZE}).fetchall()
            if not rows:
                return
            conn.execute(text(
                "UPDATE users SET email_normalized = LOWER(email) "
                "WHERE id = ANY(:ids)"
            ), {"ids": [r.id for r in rows]})
            cursor = rows[-1].id
            checkpoint.set("last_id", cursor)   # commit + 永続化

    op.run_python(backfill)

    # Step 4: 一時 index 削除
    op.drop_index("tmp_users_unnormalized")

    # Step 5: 全行埋まったので NOT NULL 化
    op.alter_column("users", "email_normalized", nullable=False)
```

**中断・再開シナリオ**:
```
$ joryu apply
→ Step 3 で 30% (15M/50M 行) 進んだところで OOM kill
→ DB 状態: status='failed', steps[3].progress='{"last_id": 15003421}'

$ joryu status
20260620T030000_backfill_email_normalized  failed
  ✓ step 1 (add_column)         done
  ✓ step 2 (create_index)       done
  ⚠ step 3 (run_python)         failed at last_id=15003421 (30%)
  · step 4 (drop_index)         pending
  · step 5 (alter_column)       pending

$ joryu apply
→ step 1, 2 skip、step 3 を last_id=15003421 から再開、step 4, 5 実行
```

### A.8 DDL 多段で途中失敗（最頻パターン）

```python
"""Add three columns and an index on col2."""
import joryu
from joryu import op, types as t

@joryu.migration(id="20260622T100000_add_columns_and_idx")
def upgrade():
    op.add_column("users", "col1", t.Text)
    op.add_column("users", "col2", t.Text)
    op.create_index("idx_col2", "users", ["col2"])
    op.add_column("users", "col3", t.Text)
```

**実行例 1: step 3 で失敗 → 原因除去 → 継続**

```
$ joryu apply
→ step 1, 2 完了 (col1, col2 追加)
→ step 3 (CREATE INDEX idx_col2) 失敗: index 'idx_col2' already exists
→ migration failed, step 4 未実行

$ joryu status
20260622T100000_add_columns_and_idx  failed
  ✓ step 1 add_column(users.col1)
  ✓ step 2 add_column(users.col2)
  ✗ step 3 create_index(idx_col2)        ← failed
  · step 4 add_column(users.col3)        pending

# 原因対処: 既存の idx_col2 を drop または rename

$ joryu apply
→ step 1, 2 skip (ensure: 既存)
→ step 3 retry → 成功
→ step 4 実行 → 成功
→ applied
```

**実行例 2: 失敗を見てファイルを修正 → push**

index 名を `idx_users_col2` に変えてコミット。`joryu apply` 再実行：

- migration 状態が `failed` → checksum 変更を許可（§9.10）
- step 1, 2 の fingerprint は不変 → skip
- step 3 の fingerprint 変更 → 新内容 (`idx_users_col2`) で実行
- step 4 実行 → 完了

### A.9 Ensure-style の挙動例

```python
"""Add phone column — re-runnable safely."""
import joryu
from joryu import op, types as t

@joryu.migration(id="20260625T100000_add_phone")
def upgrade():
    # 1 回目: カラム追加
    # 2 回目以降: 既に存在し型も一致 → skip（ensure semantics）
    # もし手動で phone INTEGER で追加されていた → 型不一致 ERROR
    op.add_column("users", "phone", t.Text, nullable=True)
```

### A.10 ユーザ定義 step (`op.step`) と PauseStep

```python
"""Wait for replication lag before continuing."""
import joryu
from joryu import op
from sqlalchemy import text

@joryu.migration(id="20260701T100000_replication_aware_change")
def upgrade():
    op.add_column("users", "feature_flag", t.Bool, server_default="false")

    @op.step
    def wait_for_replication(conn, dialect, checkpoint):
        if checkpoint.get("ready"):
            return True
        lag = conn.execute(text("SELECT EXTRACT(EPOCH FROM (NOW() - pg_last_xact_replay_timestamp()))")).scalar()
        if lag is None or lag < 1.0:
            checkpoint.set("ready", True)
            return True
        raise op.PauseStep(f"replica lag={lag:.1f}s, retry later")

    op.alter_column("users", "feature_flag", nullable=False)
```

`PauseStep` で停止した migration は `joryu apply` の再実行で続きから（resume）。CI 等で「自動的に N 秒待ってから retry」したいなら `joryu apply --retry-paused --retry-interval=30s`。
