"""requested_by as text, unique normalized_text

Revision ID: 07d70fb075ee
Revises: 54f447146432
Create Date: 2026-10-22 17:20:41.188238

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '07d70fb075ee'
down_revision: Union[str, Sequence[str], None] = '54f447146432'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    # requested_by: owner id (NULL = automatic) -> owner username or "auto".
    # Drop the FK first, widen to text, convert the values, then make it required.
    with op.batch_alter_table('generation_batches', schema=None) as batch_op:
        batch_op.drop_constraint(batch_op.f('fk_generation_batches_requested_by_owners'), type_='foreignkey')
        batch_op.alter_column('requested_by',
               existing_type=sa.INTEGER(),
               type_=sa.String(length=64),
               existing_nullable=True)
    op.execute(
        "UPDATE generation_batches SET requested_by = COALESCE("
        "(SELECT username FROM owners WHERE CAST(owners.id AS VARCHAR(64)) = generation_batches.requested_by), "
        "'auto')"
    )
    with op.batch_alter_table('generation_batches', schema=None) as batch_op:
        batch_op.alter_column('requested_by', existing_type=sa.String(length=64), nullable=False)

    # Fails if duplicate normalized_text rows already exist; remove them first.
    with op.batch_alter_table('sentences', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_sentences_normalized_text'))
        batch_op.create_index(batch_op.f('ix_sentences_normalized_text'), ['normalized_text'], unique=True)


def downgrade() -> None:
    """Downgrade schema."""
    with op.batch_alter_table('sentences', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_sentences_normalized_text'))
        batch_op.create_index(batch_op.f('ix_sentences_normalized_text'), ['normalized_text'], unique=False)

    # Usernames map back to owner ids; "auto" and deleted owners become NULL.
    with op.batch_alter_table('generation_batches', schema=None) as batch_op:
        batch_op.alter_column('requested_by', existing_type=sa.String(length=64), nullable=True)
    op.execute(
        "UPDATE generation_batches SET requested_by = "
        "(SELECT CAST(owners.id AS VARCHAR(64)) FROM owners WHERE owners.username = generation_batches.requested_by)"
    )
    with op.batch_alter_table('generation_batches', schema=None) as batch_op:
        batch_op.alter_column('requested_by',
               existing_type=sa.String(length=64),
               type_=sa.INTEGER(),
               existing_nullable=True,
               postgresql_using='requested_by::integer')
        batch_op.create_foreign_key(batch_op.f('fk_generation_batches_requested_by_owners'), 'owners', ['requested_by'], ['id'], ondelete='SET NULL')
