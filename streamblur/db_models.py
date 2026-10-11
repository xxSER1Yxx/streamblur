"""ORM-модели SQLite-хранилища."""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from sqlalchemy import (
    Boolean,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class Base(DeclarativeBase):
    pass


class Film(Base):
    """Запись о фильме во встроенной базе."""

    __tablename__ = "films"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    title: Mapped[str] = mapped_column(String(512), index=True)
    title_norm: Mapped[str | None] = mapped_column(String(512), index=True)
    """Название в нижнем регистре.

    Нужно потому, что SQLite ``lower()`` и ``LIKE`` умеют только ASCII —
    кириллический поиск «ночной» → «Ночной патруль» без этой колонки не работает.
    """
    original_title: Mapped[str | None] = mapped_column(String(512))
    year: Mapped[int | None] = mapped_column(Integer, index=True)
    release_date: Mapped[str | None] = mapped_column(String(32))
    runtime_minutes: Mapped[int | None] = mapped_column(Integer)
    country: Mapped[str | None] = mapped_column(String(256))
    language: Mapped[str | None] = mapped_column(String(64))
    genres: Mapped[str | None] = mapped_column(Text)          # JSON-список
    studios: Mapped[str | None] = mapped_column(Text)         # JSON-список студий
    production_companies: Mapped[str | None] = mapped_column(Text)  # JSON-список компаний
    overview: Mapped[str | None] = mapped_column(Text)
    poster_url: Mapped[str | None] = mapped_column(String(1024))
    tmdb_id: Mapped[int | None] = mapped_column(Integer, index=True)
    imdb_id: Mapped[str | None] = mapped_column(String(32), index=True)
    source: Mapped[str | None] = mapped_column(String(64))    # откуда прилетели данные
    raw: Mapped[str | None] = mapped_column(Text)             # JSON исходного ответа
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, onupdate=utcnow)

    segments: Mapped[list["Segment"]] = relationship(
        back_populates="film", cascade="all, delete-orphan", lazy="selectin"
    )

    # ------------------------------------------------------------- helpers
    def as_dict(self) -> dict[str, Any]:
        import json

        def _list(value: str | None) -> list[str]:
            if not value:
                return []
            try:
                parsed = json.loads(value)
            except (json.JSONDecodeError, TypeError):
                return []
            return parsed if isinstance(parsed, list) else []

        return {
            "id": self.id,
            "title": self.title,
            "original_title": self.original_title,
            "year": self.year,
            "release_date": self.release_date,
            "runtime_minutes": self.runtime_minutes,
            "country": self.country,
            "language": self.language,
            "genres": _list(self.genres),
            "studios": _list(self.studios),
            "production_companies": _list(self.production_companies),
            "overview": self.overview,
            "poster_url": self.poster_url,
            "tmdb_id": self.tmdb_id,
            "imdb_id": self.imdb_id,
            "source": self.source,
            "segments_count": len(self.segments or []),
            "updated_at": self.updated_at.isoformat() if self.updated_at else None,
        }


class Segment(Base):
    """Опасный интервал фильма: [start, end] в секундах от начала фильма."""

    __tablename__ = "segments"
    __table_args__ = (
        UniqueConstraint("film_id", "provider", "start", "end", name="uq_segment"),
        Index("ix_segments_film", "film_id"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    film_id: Mapped[int] = mapped_column(ForeignKey("films.id", ondelete="CASCADE"))
    start: Mapped[float] = mapped_column(Float)
    end: Mapped[float] = mapped_column(Float)
    kind: Mapped[str] = mapped_column(String(32), default="nsfw")
    reason: Mapped[str | None] = mapped_column(String(256))
    provider: Mapped[str] = mapped_column(String(64), default="local-file")
    language: Mapped[str | None] = mapped_column(String(32))
    confidence: Mapped[float] = mapped_column(Float, default=1.0)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)

    film: Mapped[Film] = relationship(back_populates="segments")

    @property
    def duration(self) -> float:
        return max(0.0, float(self.end) - float(self.start))

    def as_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "film_id": self.film_id,
            "start": float(self.start),
            "end": float(self.end),
            "kind": self.kind,
            "reason": self.reason,
            "provider": self.provider,
            "language": self.language,
            "confidence": float(self.confidence),
        }


class SyncLog(Base):
    """История подтягивания таймингов/метаданных — видно в UI."""

    __tablename__ = "sync_log"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    film_id: Mapped[int | None] = mapped_column(ForeignKey("films.id", ondelete="SET NULL"))
    provider: Mapped[str] = mapped_column(String(64))
    kind: Mapped[str] = mapped_column(String(32))    # timings | metadata
    status: Mapped[str] = mapped_column(String(16))  # ok | error | skip
    segments: Mapped[int] = mapped_column(Integer, default=0)
    message: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)

    def as_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "film_id": self.film_id,
            "provider": self.provider,
            "kind": self.kind,
            "status": self.status,
            "segments": self.segments,
            "message": self.message,
            "created_at": self.created_at.isoformat() if self.created_at else None,
        }


class KeyValue(Base):
    """Служебное хранилище: последнее обновление, активный фильм и т.п."""

    __tablename__ = "kv"

    key: Mapped[str] = mapped_column(String(128), primary_key=True)
    value: Mapped[str | None] = mapped_column(Text)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, onupdate=utcnow)
