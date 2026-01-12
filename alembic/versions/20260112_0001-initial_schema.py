"""Initial schema

Revision ID: 20260112_0001
Revises:
Create Date: 2026-01-12

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '20260112_0001'
down_revision: Union[str, None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Create initial tables."""
    # Create users table
    op.create_table(
        'users',
        sa.Column('discord_id', sa.String(), nullable=False),
        sa.Column('puuid', sa.String(), nullable=False),
        sa.Column('summoner_name', sa.String(), nullable=False),
        sa.Column('region', sa.String(), nullable=False),
        sa.PrimaryKeyConstraint('discord_id')
    )
    op.create_index('ix_users_discord_id', 'users', ['discord_id'])
    op.create_index('ix_users_puuid', 'users', ['puuid'], unique=True)
    op.create_index('ix_users_summoner_name', 'users', ['summoner_name'])
    op.create_index('ix_users_region', 'users', ['region'])

    # Create matches table
    op.create_table(
        'matches',
        sa.Column('match_id', sa.String(), nullable=False),
        sa.Column('puuid', sa.String(), nullable=False),
        sa.Column('queue_id', sa.Integer(), nullable=False),
        sa.Column('win', sa.Boolean(), nullable=False),
        sa.Column('timestamp', sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(['puuid'], ['users.puuid']),
        sa.PrimaryKeyConstraint('match_id', 'puuid')
    )
    op.create_index('ix_matches_match_id', 'matches', ['match_id'])
    op.create_index('ix_matches_puuid', 'matches', ['puuid'])

    # Create ranked_cache table
    op.create_table(
        'ranked_cache',
        sa.Column('summoner_id', sa.String(), nullable=False),
        sa.Column('queue_type', sa.String(), nullable=False),
        sa.Column('json', sa.JSON(), nullable=True),
        sa.Column('ts', sa.DateTime(), nullable=True),
        sa.PrimaryKeyConstraint('summoner_id', 'queue_type')
    )

    # Create match_cache table
    op.create_table(
        'match_cache',
        sa.Column('match_id', sa.String(), nullable=False),
        sa.Column('region', sa.String(), nullable=True),
        sa.Column('json', sa.JSON(), nullable=True),
        sa.Column('ts', sa.DateTime(), nullable=True),
        sa.PrimaryKeyConstraint('match_id')
    )


def downgrade() -> None:
    """Drop all tables."""
    op.drop_table('match_cache')
    op.drop_table('ranked_cache')
    op.drop_index('ix_matches_puuid', 'matches')
    op.drop_index('ix_matches_match_id', 'matches')
    op.drop_table('matches')
    op.drop_index('ix_users_region', 'users')
    op.drop_index('ix_users_summoner_name', 'users')
    op.drop_index('ix_users_puuid', 'users')
    op.drop_index('ix_users_discord_id', 'users')
    op.drop_table('users')
