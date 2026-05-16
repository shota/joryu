"""Backfill users.email_normalized for 50M rows (resumable)."""
import joryu
from joryu import op, types as t
from sqlalchemy import text

BATCH_SIZE = 10_000


@joryu.migration(
    id="20260620T030000_backfill_email_normalized",
    depends_on=["20260619T120000_add_email_normalized_col"],
    transaction_mode="per_step",   # default, but explicit
)
def upgrade():
    # Step 1: ensure semantics — skipped if already present.
    op.add_column("users", "email_normalized", t.Text, nullable=True)

    # Step 2: partial index for narrowing unprocessed rows (drop after backfill).
    op.create_index(
        "tmp_users_unnormalized",
        "users",
        ["id"],
        where="email_normalized IS NULL",
    )

    # Step 3: user-written batching loop; joryu persists the checkpoint.
    def backfill(conn, dialect, checkpoint):
        cursor = checkpoint.get("last_id", 0)
        while True:
            rows = conn.execute(
                text(
                    "SELECT id FROM users "
                    "WHERE id > :c AND email_normalized IS NULL "
                    "ORDER BY id LIMIT :n"
                ),
                {"c": cursor, "n": BATCH_SIZE},
            ).fetchall()
            if not rows:
                return
            conn.execute(
                text(
                    "UPDATE users SET email_normalized = LOWER(email) "
                    "WHERE id = ANY(:ids)"
                ),
                {"ids": [r.id for r in rows]},
            )
            cursor = rows[-1].id
            checkpoint.set("last_id", cursor)   # commit + persist

    op.run_python(backfill)

    # Step 4: drop the temporary index.
    op.drop_index("tmp_users_unnormalized")

    # Step 5: with every row populated, enforce NOT NULL.
    op.alter_column("users", "email_normalized", nullable=False)


@joryu.downgrade
def downgrade():
    # JORYU-DOWN-HINT: schema-impact:
    #   - restore_nullable: users.email_normalized
    #   - drop_index: tmp_users_unnormalized
    #   - restore_data: users.email_normalized
    #   - drop_column: users.email_normalized
    # JORYU-DOWN-HINT: cross-references: []
    # JORYU-DOWN-HINT: data-loss-risk: irreversible
    # JORYU-DOWN-HINT: data-loss-reason: backfilled email_normalized values cannot be regenerated without the source emails being unchanged
    # JORYU-DOWN-HINT: order-constraint:
    #   - restore_nullable before drop_column
    # JORYU-DOWN-HINT: requires-app-knowledge: true
    # JORYU-DOWN-HINT: app-knowledge-needed:
    #   - whether downstream consumers rely on email_normalized
    # JORYU-DOWN-HINT: completion-status: manual-review-required

    # NOTE: data-loss-risk is irreversible. A human must decide
    # whether to drop the column or leave the data intact.
    pass
