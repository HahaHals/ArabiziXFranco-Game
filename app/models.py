"""Database tables (spec section 8)."""

import enum
from datetime import datetime

from sqlalchemy import (
    JSON,
    Boolean,
    DateTime,
    Enum,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    false,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db import Base


class SentenceSource(enum.StrEnum):
    generated = "generated"
    owner = "owner"


class SentenceStatus(enum.StrEnum):
    pending_review = "pending_review"
    active = "active"
    retired = "retired"
    reported = "reported"
    deleted = "deleted"


class ViewOutcome(enum.StrEnum):
    shown = "shown"
    answered = "answered"
    reported = "reported"


class FlagReview(enum.StrEnum):
    kept = "kept"
    excluded = "excluded"


class ReportReason(enum.StrEnum):
    misspelled = "misspelled"
    not_egyptian = "not_egyptian"
    inappropriate = "inappropriate"
    not_understood = "not_understood"


def _enum(cls: type[enum.Enum], name: str) -> Enum:
    # Stored as VARCHAR + CHECK rather than a native Postgres enum, so adding a value
    # later is a simple migration and SQLite behaves the same.
    return Enum(
        cls,
        name=name,
        native_enum=False,
        create_constraint=True,
        length=32,
        values_callable=lambda c: [m.value for m in c],
    )


def _created_at() -> Mapped[datetime]:
    return mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)


class Owner(Base):
    __tablename__ = "owners"

    id: Mapped[int] = mapped_column(primary_key=True)
    username: Mapped[str] = mapped_column(String(64), unique=True)
    password_hash: Mapped[str] = mapped_column(String(255))


class Player(Base):
    __tablename__ = "players"

    id: Mapped[str] = mapped_column(String(32), primary_key=True)  # e.g. "p_7f3a91c2"
    display_name: Mapped[str | None] = mapped_column(String(40))
    created_at: Mapped[datetime] = _created_at()


class Theme(Base):
    __tablename__ = "themes"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(100), unique=True)
    description: Mapped[str] = mapped_column(Text, default="")
    active: Mapped[bool] = mapped_column(Boolean, default=True)


class TargetWord(Base):
    __tablename__ = "target_words"

    id: Mapped[int] = mapped_column(primary_key=True)
    word: Mapped[str] = mapped_column(String(100), unique=True)
    active: Mapped[bool] = mapped_column(Boolean, default=True)


class GenerationBatch(Base):
    __tablename__ = "generation_batches"

    id: Mapped[int] = mapped_column(primary_key=True)
    # The owner's username, or "auto" when the pool threshold triggered it. Plain text
    # rather than a foreign key, so it stays readable if the owner account is deleted.
    requested_by: Mapped[str] = mapped_column(String(64))
    model: Mapped[str] = mapped_column(String(100))
    prompt_snapshot: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = _created_at()


class Sentence(Base):
    __tablename__ = "sentences"

    id: Mapped[int] = mapped_column(primary_key=True)
    text: Mapped[str] = mapped_column(Text)
    # Whitespace/punctuation-normalized text. Unique, so the database itself rejects
    # duplicates (4.2), deleted sentences included: bring those back with "restore".
    normalized_text: Mapped[str] = mapped_column(Text, unique=True, index=True)
    words: Mapped[list[str]] = mapped_column(JSON)
    english: Mapped[str] = mapped_column(Text, default="")
    source: Mapped[SentenceSource] = mapped_column(_enum(SentenceSource, "sentence_source"))
    theme_id: Mapped[int | None] = mapped_column(ForeignKey("themes.id", ondelete="SET NULL"))
    batch_id: Mapped[int | None] = mapped_column(
        ForeignKey("generation_batches.id", ondelete="SET NULL")
    )
    status: Mapped[SentenceStatus] = mapped_column(
        _enum(SentenceStatus, "sentence_status"), default=SentenceStatus.active, index=True
    )
    replaces_id: Mapped[int | None] = mapped_column(ForeignKey("sentences.id"))
    created_at: Mapped[datetime] = _created_at()

    theme: Mapped[Theme | None] = relationship()
    batch: Mapped[GenerationBatch | None] = relationship()
    replaces: Mapped["Sentence | None"] = relationship(remote_side=[id])


class Answer(Base):
    """One row per filled box. This table is the dataset."""

    __tablename__ = "answers"

    id: Mapped[int] = mapped_column(primary_key=True)
    sentence_id: Mapped[int] = mapped_column(ForeignKey("sentences.id"), index=True)
    player_id: Mapped[str] = mapped_column(
        ForeignKey("players.id", ondelete="CASCADE"), index=True
    )
    position_start: Mapped[int] = mapped_column(Integer)  # 1-based
    position_end: Mapped[int] = mapped_column(Integer)
    arabic: Mapped[str] = mapped_column(Text, index=True)
    franco_raw: Mapped[str] = mapped_column(Text)
    franco: Mapped[str] = mapped_column(Text)
    joined: Mapped[bool] = mapped_column(Boolean, default=False)
    flagged: Mapped[bool] = mapped_column(Boolean, default=False, index=True)
    flag_reason: Mapped[str | None] = mapped_column(Text)
    # An owner's decision on a flagged answer. Reviewed answers aren't re-flagged, and
    # excluded ones never go into exports.
    flag_review: Mapped[FlagReview | None] = mapped_column(_enum(FlagReview, "flag_review"))
    created_at: Mapped[datetime] = _created_at()

    sentence: Mapped[Sentence] = relationship()
    player: Mapped[Player] = relationship()


class SentenceView(Base):
    """Which sentences each player has seen, and how it ended. Powers rotation (4.6)."""

    __tablename__ = "sentence_views"

    player_id: Mapped[str] = mapped_column(
        ForeignKey("players.id", ondelete="CASCADE"), primary_key=True
    )
    sentence_id: Mapped[int] = mapped_column(ForeignKey("sentences.id"), primary_key=True)
    outcome: Mapped[ViewOutcome] = mapped_column(_enum(ViewOutcome, "view_outcome"))
    created_at: Mapped[datetime] = _created_at()

    sentence: Mapped[Sentence] = relationship()
    player: Mapped[Player] = relationship()

    __table_args__ = (Index("ix_sentence_views_sentence_outcome", "sentence_id", "outcome"),)


class Report(Base):
    __tablename__ = "reports"

    id: Mapped[int] = mapped_column(primary_key=True)
    sentence_id: Mapped[int] = mapped_column(ForeignKey("sentences.id"), index=True)
    player_id: Mapped[str] = mapped_column(ForeignKey("players.id", ondelete="CASCADE"))
    reason: Mapped[ReportReason] = mapped_column(_enum(ReportReason, "report_reason"))
    comment: Mapped[str | None] = mapped_column(Text)
    # Set when an owner dismisses, fixes or deletes the sentence, so old reports stop
    # counting toward the auto-pull threshold but stay as history.
    resolved: Mapped[bool] = mapped_column(Boolean, default=False, server_default=false())
    created_at: Mapped[datetime] = _created_at()

    sentence: Mapped[Sentence] = relationship()
    player: Mapped[Player] = relationship()

    # A player can report a given sentence only once (3.3).
    __table_args__ = (UniqueConstraint("sentence_id", "player_id"),)


class Setting(Base):
    """Owner-tunable key/value settings. Values are JSON-encoded; see app/settings_store.py."""

    __tablename__ = "settings"

    key: Mapped[str] = mapped_column(String(64), primary_key=True)
    value: Mapped[str] = mapped_column(Text)
