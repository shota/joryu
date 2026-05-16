"""Add GIN index on settings JSON column (per-dialect)."""
import joryu
from joryu import op


@joryu.migration(
    id="20260520T100000_settings_index",
    transaction_mode="none",     # uses CREATE INDEX CONCURRENTLY on Postgres
)
def upgrade():
    op.execute({
        "postgresql": "CREATE INDEX CONCURRENTLY users_settings_idx ON users USING GIN (settings)",
        "mysql":      "CREATE INDEX users_settings_idx ON users ((CAST(settings AS CHAR(255))))",
        "sqlite":     "CREATE INDEX users_settings_idx ON users (json_extract(settings, '$'))",
    })


@joryu.downgrade
def downgrade():
    # JORYU-DOWN-HINT: schema-impact:
    #   - drop_index: users_settings_idx
    # JORYU-DOWN-HINT: cross-references: []
    # JORYU-DOWN-HINT: data-loss-risk: low
    # JORYU-DOWN-HINT: requires-app-knowledge: false
    # JORYU-DOWN-HINT: completion-status: complete
    op.execute("DROP INDEX users_settings_idx")
