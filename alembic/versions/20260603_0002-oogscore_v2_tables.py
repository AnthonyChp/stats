"""OogScore v2 tables: match_participants, baseline_cache, oogscores

Revision ID: 20260603_0002
Revises: 20260112_0001
Create Date: 2026-06-03
"""
from alembic import op
import sqlalchemy as sa

revision = '20260603_0002'
down_revision = '20260112_0001'
branch_labels = None
depends_on = None

def upgrade():
    op.create_table(
        'match_participants',
        sa.Column('id', sa.Integer(), autoincrement=True, nullable=False),
        sa.Column('match_id', sa.String(), nullable=False),
        sa.Column('puuid', sa.String(), nullable=False),
        sa.Column('is_linked_member', sa.Boolean(), default=False),
        sa.Column('role', sa.String()),
        sa.Column('champion', sa.String()),
        sa.Column('win', sa.Boolean()),
        sa.Column('kills', sa.Integer(), default=0),
        sa.Column('deaths', sa.Integer(), default=0),
        sa.Column('assists', sa.Integer(), default=0),
        sa.Column('total_damage_champ', sa.Integer(), default=0),
        sa.Column('total_damage_taken', sa.Integer(), default=0),
        sa.Column('gold_earned', sa.Integer(), default=0),
        sa.Column('cs_total', sa.Integer(), default=0),
        sa.Column('vision_score', sa.Integer(), default=0),
        sa.Column('heals_on_teammates', sa.Integer(), default=0),
        sa.Column('shields_on_teammates', sa.Integer(), default=0),
        sa.Column('time_ccing_others', sa.Integer(), default=0),
        sa.Column('penta_kills', sa.Integer(), default=0),
        sa.Column('dragon_kills', sa.Integer(), default=0),
        sa.Column('baron_kills', sa.Integer(), default=0),
        sa.Column('turret_kills', sa.Integer(), default=0),
        sa.Column('challenges_json', sa.Text()),
        sa.Column('duration_min', sa.Float()),
        sa.Column('is_scorable', sa.Boolean(), default=True),
        sa.ForeignKeyConstraint(['match_id'], ['matches.match_id']),
        sa.PrimaryKeyConstraint('id')
    )
    op.create_index('ix_match_participants_match_id', 'match_participants', ['match_id'])
    op.create_index('ix_match_participants_puuid', 'match_participants', ['puuid'])
    op.create_index('ix_match_participants_role', 'match_participants', ['role'])
    op.create_index('ix_match_participants_champion', 'match_participants', ['champion'])

    op.create_table(
        'baseline_cache',
        sa.Column('id', sa.Integer(), autoincrement=True, nullable=False),
        sa.Column('scope', sa.String(), nullable=True),
        sa.Column('distributions_json', sa.Text(), nullable=True),
        sa.Column('sample_size', sa.Integer(), nullable=True),
        sa.Column('computed_at', sa.DateTime(), nullable=True),
        sa.PrimaryKeyConstraint('id')
    )
    op.create_index('ix_baseline_cache_scope', 'baseline_cache', ['scope'])

    op.create_table(
        'oogscores',
        sa.Column('id', sa.Integer(), autoincrement=True, nullable=False),
        sa.Column('match_id', sa.String(), nullable=True),
        sa.Column('puuid', sa.String(), nullable=True),
        sa.Column('score', sa.Float(), nullable=True),
        sa.Column('grade', sa.String(), nullable=True),
        sa.Column('role', sa.String(), nullable=True),
        sa.Column('components_json', sa.Text(), nullable=True),
        sa.Column('baseline_source', sa.String(), nullable=True),
        sa.Column('sample_size_used', sa.Integer(), nullable=True),
        sa.Column('computed_at', sa.DateTime(), nullable=True),
        sa.PrimaryKeyConstraint('id')
    )
    op.create_index('ix_oogscores_match_id', 'oogscores', ['match_id'])
    op.create_index('ix_oogscores_puuid', 'oogscores', ['puuid'])

def downgrade():
    op.drop_index('ix_oogscores_puuid', 'oogscores')
    op.drop_index('ix_oogscores_match_id', 'oogscores')
    op.drop_table('oogscores')
    op.drop_index('ix_baseline_cache_scope', 'baseline_cache')
    op.drop_table('baseline_cache')
    op.drop_index('ix_match_participants_champion', 'match_participants')
    op.drop_index('ix_match_participants_role', 'match_participants')
    op.drop_index('ix_match_participants_puuid', 'match_participants')
    op.drop_index('ix_match_participants_match_id', 'match_participants')
    op.drop_table('match_participants')
