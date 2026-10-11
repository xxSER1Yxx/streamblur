"""Слой доступа к SQLite: сессии, репозиторий фильмов, сегментов и служебных ключей."""
from __future__ import annotations

import json
import threading
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator, Sequence

from sqlalchemy import create_engine, delete, event, func, or_, select
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker

from .db_models import Base, Film, KeyValue, Segment, SyncLog


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _sqlite_connect(dbapi_connection, _record) -> None:
    """Подготовка соединения.

    ВАЖНО: регистронезависимый поиск по кириллице нельзя делать через SQLite —
    ``lower()`` и ``LIKE`` там знают только ASCII. Поэтому название дублируется в
    ``title_norm`` уже приведённым к нижнему регистру средствами Python.
    """
    cursor = dbapi_connection.cursor()
    cursor.execute("PRAGMA foreign_keys = ON")
    cursor.close()


class Database:
    """Тонкая обёртка над SQLAlchemy. Потокобезопасна (своя сессия на вызов)."""

    def __init__(self, path: Path | str = ":memory:") -> None:
        self.path = str(path)
        if self.path != ":memory:":
            Path(self.path).parent.mkdir(parents=True, exist_ok=True)
            url = f"sqlite:///{self.path}"
            self._engine: Engine = create_engine(url, future=True)
        else:
            self._engine = create_engine("sqlite://", future=True)
        event.listen(self._engine, "connect", _sqlite_connect)
        self._migrate()
        self._session_factory = sessionmaker(bind=self._engine, expire_on_commit=False, future=True)
        self._lock = threading.RLock()

    # ------------------------------------------------------------- миграции
    def _migrate(self) -> None:
        """Добавить недостающие колонки старым базам и дозаполнить их."""
        Base.metadata.create_all(self._engine)
        with self._engine.begin() as conn:
            columns = {row[1] for row in conn.exec_driver_sql("PRAGMA table_info(films)")}
            if "title_norm" not in columns:
                conn.exec_driver_sql("ALTER TABLE films ADD COLUMN title_norm VARCHAR(512)")
            rows = conn.exec_driver_sql(
                "SELECT id, title FROM films WHERE title_norm IS NULL OR title_norm = ''"
            ).fetchall()
            for film_id, title in rows:
                conn.exec_driver_sql(
                    "UPDATE films SET title_norm = ? WHERE id = ?",
                    ((title or "").strip().lower(), film_id),
                )

    # ------------------------------------------------------------- поиск
    @staticmethod
    def _norm(term: str) -> str:
        """Нормализация строки для поиска: нижний регистр силами Python."""
        return " ".join((term or "").strip().lower().split())

    def _like(self, term: str) -> str:
        """Шаблон «содержит» — регистр не учитывается, кириллица включительно."""
        return f"%{self._norm(term)}%"

    def _same(self, term: str) -> str:
        """Точное совпадение нормализованного названия (для поиска дублей)."""
        return self._norm(term)

    # ------------------------------------------------------------- sessions
    @contextmanager
    def session(self) -> Iterator[Session]:
        session = self._session_factory()
        try:
            yield session
            session.commit()
        except Exception:
            session.rollback()
            raise
        finally:
            session.close()

    # ----------------------------------------------------------------- films
    def upsert_film(
        self,
        *,
        title: str,
        original_title: str | None = None,
        year: int | None = None,
        release_date: str | None = None,
        runtime_minutes: int | None = None,
        country: str | None = None,
        language: str | None = None,
        genres: Sequence[str] | None = None,
        studios: Sequence[str] | None = None,
        production_companies: Sequence[str] | None = None,
        overview: str | None = None,
        poster_url: str | None = None,
        tmdb_id: int | None = None,
        imdb_id: str | None = None,
        source: str | None = None,
        raw: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Найти фильм (по tmdb/imdb id → по названию+году) и обновить поля."""
        with self._lock, self.session() as s:
            film: Film | None = None
            if tmdb_id:
                film = s.scalar(select(Film).where(Film.tmdb_id == tmdb_id))
            if film is None and imdb_id:
                film = s.scalar(select(Film).where(Film.imdb_id == imdb_id))
            if film is None:
                exact = self._same(title)
                stmt = select(Film).where(Film.title_norm == exact)
                if year:
                    stmt = stmt.where(Film.year == year)
                film = s.scalars(stmt.limit(1)).first()
                if film is None and year:
                    # тот же тайтл без года — не плодим дубликаты
                    film = s.scalars(
                        select(Film).where(Film.title_norm == exact).limit(1)
                    ).first()

            if film is None:
                film = Film(title=title.strip())
                s.add(film)

            film.title = title.strip()
            film.title_norm = self._norm(title)
            film.original_title = original_title or film.original_title
            film.year = year or film.year
            film.release_date = release_date or film.release_date
            film.runtime_minutes = runtime_minutes or film.runtime_minutes
            film.country = country or film.country
            film.language = language or film.language
            film.genres = _dump_list(genres) if genres is not None else film.genres
            film.studios = _dump_list(studios) if studios is not None else film.studios
            film.production_companies = (
                _dump_list(production_companies)
                if production_companies is not None
                else film.production_companies
            )
            film.overview = overview or film.overview
            film.poster_url = poster_url or film.poster_url
            film.tmdb_id = tmdb_id or film.tmdb_id
            film.imdb_id = imdb_id or film.imdb_id
            film.source = source or film.source
            if raw is not None:
                film.raw = json.dumps(raw, ensure_ascii=False)
            film.updated_at = _now()
            s.flush()
            return film.as_dict()

    def get_film(self, film_id: int) -> dict[str, Any] | None:
        with self.session() as s:
            film = s.get(Film, film_id)
            return film.as_dict() if film else None

    def find_film(self, query: str, year: int | None = None) -> dict[str, Any] | None:
        q = (query or "").strip()
        if not q:
            return None
        pattern = self._like(q)
        with self.session() as s:
            stmt = select(Film).where(
                or_(Film.title_norm.like(pattern), Film.original_title.like(pattern))
            )
            if year:
                stmt = stmt.where(Film.year == year)
            film = s.scalars(stmt.order_by(Film.title).limit(1)).first()
            return film.as_dict() if film else None

    def list_films(self, query: str | None = None, limit: int = 200) -> list[dict[str, Any]]:
        with self.session() as s:
            stmt = select(Film)
            if query:
                pattern = self._like(query)
                stmt = stmt.where(
                    or_(Film.title_norm.like(pattern), Film.original_title.like(pattern))
                )
            stmt = stmt.order_by(Film.title).limit(limit)
            return [f.as_dict() for f in s.scalars(stmt)]

    def delete_film(self, film_id: int) -> bool:
        with self._lock, self.session() as s:
            film = s.get(Film, film_id)
            if film is None:
                return False
            s.delete(film)
            return True

    def count_films(self) -> int:
        with self.session() as s:
            return int(s.scalar(select(func.count()).select_from(Film)) or 0)

    # -------------------------------------------------------------- segments
    def replace_segments(
        self, film_id: int, segments: Sequence[dict[str, Any]], provider: str
    ) -> int:
        """Полностью заменить тайминги фильма от конкретного провайдера."""
        cleaned: list[dict[str, Any]] = []
        for seg in segments:
            start = float(seg["start"])
            end = float(seg["end"])
            if end <= start:
                continue
            cleaned.append(
                {
                    "start": start,
                    "end": end,
                    "kind": seg.get("kind", "nsfw"),
                    "reason": seg.get("reason"),
                    "language": seg.get("language"),
                    "confidence": float(seg.get("confidence", 1.0)),
                }
            )
        with self._lock, self.session() as s:
            s.execute(
                delete(Segment).where(Segment.film_id == film_id, Segment.provider == provider)
            )
            for item in cleaned:
                s.add(
                    Segment(
                        film_id=film_id,
                        provider=provider,
                        created_at=_now(),
                        **item,
                    )
                )
        return len(cleaned)

    def add_segment(self, film_id: int, seg: dict[str, Any]) -> dict[str, Any]:
        with self._lock, self.session() as s:
            row = Segment(
                film_id=film_id,
                start=float(seg["start"]),
                end=float(seg["end"]),
                kind=seg.get("kind", "nsfw"),
                reason=seg.get("reason"),
                provider=seg.get("provider", "manual"),
                language=seg.get("language"),
                confidence=float(seg.get("confidence", 1.0)),
                created_at=_now(),
            )
            s.add(row)
            s.flush()
            return row.as_dict()

    def delete_segment(self, segment_id: int) -> bool:
        with self._lock, self.session() as s:
            row = s.get(Segment, segment_id)
            if row is None:
                return False
            s.delete(row)
            return True

    def list_segments(self, film_id: int) -> list[dict[str, Any]]:
        with self.session() as s:
            stmt = (
                select(Segment)
                .where(Segment.film_id == film_id)
                .order_by(Segment.start)
            )
            return [r.as_dict() for r in s.scalars(stmt)]

    def count_segments(self, film_id: int) -> int:
        with self.session() as s:
            return int(
                s.scalar(
                    select(func.count()).select_from(Segment).where(Segment.film_id == film_id)
                )
                or 0
            )

    # --------------------------------------------------------------- sync log
    def log_sync(
        self,
        *,
        film_id: int | None,
        provider: str,
        kind: str,
        status: str,
        segments: int = 0,
        message: str | None = None,
    ) -> None:
        with self._lock, self.session() as s:
            s.add(
                SyncLog(
                    film_id=film_id,
                    provider=provider,
                    kind=kind,
                    status=status,
                    segments=segments,
                    message=message,
                    created_at=_now(),
                )
            )

    def recent_sync(self, limit: int = 30) -> list[dict[str, Any]]:
        with self.session() as s:
            stmt = select(SyncLog).order_by(SyncLog.id.desc()).limit(limit)
            return [r.as_dict() for r in s.scalars(stmt)]

    # --------------------------------------------------------------------- kv
    def kv_get(self, key: str, default: str | None = None) -> str | None:
        with self.session() as s:
            row = s.get(KeyValue, key)
            return row.value if row and row.value is not None else default

    def kv_set(self, key: str, value: str) -> None:
        with self._lock, self.session() as s:
            row = s.get(KeyValue, key)
            if row is None:
                s.add(KeyValue(key=key, value=value, updated_at=_now()))
            else:
                row.value = value
                row.updated_at = _now()

    def kv_delete(self, key: str) -> None:
        with self._lock, self.session() as s:
            s.execute(delete(KeyValue).where(KeyValue.key == key))


def _dump_list(values: Sequence[str] | None) -> str | None:
    if values is None:
        return None
    return json.dumps(list(values), ensure_ascii=False)
