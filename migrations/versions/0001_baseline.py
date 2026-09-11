"""Baseline: the schema as it stood before the Timetables source.

Deliberately empty. Existing databases are stamped with this revision so the
479 MB production file becomes a first-class migratable artifact rather than
something to be recreated.

Revision ID: 0001
"""

revision = "0001"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    pass


def downgrade() -> None:
    pass
