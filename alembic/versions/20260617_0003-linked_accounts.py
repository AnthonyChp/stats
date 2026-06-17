"""linked_accounts: comptes smurf rattachés à un compte Discord

Revision ID: 20260617_0003
Revises: 20260603_0002
Create Date: 2026-06-17
"""
from alembic import op
import sqlalchemy as sa

revision = '20260617_0003'
down_revision = '20260603_0002'
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        'linked_accounts',
        sa.Column('id', sa.Integer(), autoincrement=True, nullable=False),
        sa.Column('discord_id', sa.String(), nullable=False),
        sa.Column('puuid', sa.String(), nullable=False),
        sa.Column('summoner_name', sa.String(), nullable=False),
        sa.Column('region', sa.String(), nullable=False),
        sa.ForeignKeyConstraint(['discord_id'], ['users.discord_id']),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('puuid'),
    )
    op.create_index('ix_linked_accounts_discord_id', 'linked_accounts', ['discord_id'])
    op.create_index('ix_linked_accounts_puuid', 'linked_accounts', ['puuid'], unique=True)


def downgrade():
    op.drop_index('ix_linked_accounts_puuid', 'linked_accounts')
    op.drop_index('ix_linked_accounts_discord_id', 'linked_accounts')
    op.drop_table('linked_accounts')
