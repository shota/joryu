"""TODO: describe this migration."""
import joryu
from joryu import op, types as t


@joryu.migration(id="20260101T000000_example_empty")
def upgrade():
    pass


@joryu.downgrade
def downgrade():
    # JORYU-DOWN-HINT: schema-impact: []
    # JORYU-DOWN-HINT: cross-references: []
    # JORYU-DOWN-HINT: data-loss-risk: none
    # JORYU-DOWN-HINT: requires-app-knowledge: false
    # JORYU-DOWN-HINT: completion-status: stub
    pass
