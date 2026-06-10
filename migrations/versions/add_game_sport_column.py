"""add sport column to games, make sport_id nullable

Revision ID: a1b2c3d4e5f6
Revises: ec607a19ed0a
Create Date: 2026-06-10

"""
from typing import Sequence, Union
from alembic import op
import sqlalchemy as sa

revision: str = 'a1b2c3d4e5f6'
down_revision: Union[str, None] = 'ec607a19ed0a'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # Add plain sport string column (e.g. "basketball_nba") so games can be
    # created directly from Odds API data without needing a Sport FK row.
    with op.batch_alter_table('games') as batch_op:
        batch_op.add_column(sa.Column('sport', sa.String(length=50), nullable=True))
        batch_op.create_index('ix_games_sport', ['sport'])
        batch_op.alter_column('sport_id', existing_type=sa.Integer(), nullable=True)


def downgrade() -> None:
    with op.batch_alter_table('games') as batch_op:
        batch_op.drop_index('ix_games_sport')
        batch_op.drop_column('sport')
