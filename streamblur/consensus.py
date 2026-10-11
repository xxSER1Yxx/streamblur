"""Консенсус таймингов: когда сцену размечали несколько людей.

На сайтах с пользовательской разметкой один и тот же момент часто описывают
по-разному: кто-то поставил ``0:12:04``, кто-то ``0:12:11``, кто-то вообще
промахнулся. Если брать первое попавшееся — легко промазать мимо сцены и словить
бан. Поэтому здесь три режима:

``consensus`` (по умолчанию)
    Оставляем только те интервалы, за которые проголосовало не меньше
    ``min_votes`` источников. Голоса считаются по пересечению интервалов:
    ``0:12:04–0:14:31`` и ``0:12:11–0:14:40`` — это одна и та же сцена.
``union``
    Максимально осторожно: блюрим по объединению всех источников. Ложных
    срабатываний больше, зато пропустить сцену почти невозможно.
``strict``
    Только пересечение всех источников — минимум ложных блюров, максимум риска.

Для стрима по правилам Twitch безопаснее ``consensus`` с ``min_votes=1``
или ``union``: лучше лишний раз закрыться, чем получить страйк.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Iterable

Mode = str  # "consensus" | "union" | "strict"

DEFAULT_MIN_VOTES = 2
DEFAULT_MIN_OVERLAP = 0.5


@dataclass
class Cluster:
    """Группа интервалов, которые разные люди поставили на одну и ту же сцену."""

    start: float
    end: float
    votes: int = 1
    sources: list[str] = field(default_factory=list)
    reasons: list[str] = field(default_factory=list)
    _starts: list[float] = field(default_factory=list)
    _ends: list[float] = field(default_factory=list)

    def add(self, start: float, end: float, source: str, reason: str | None) -> None:
        """Добавить голос и пересчитать границы по медиане.

        Медиана устойчива к выбросам: если один человек поставил сцену на минуту
        раньше остальных, граница не уедет.
        """
        self.votes += 1
        self.sources.append(source)
        if reason:
            self.reasons.append(reason)
        self._starts.append(start)
        self._ends.append(end)
        self.start = _median(self._starts)
        self.end = _median(self._ends)
        if self.end <= self.start:
            self.end = max(self._ends)

    def as_segment(self, kind: str = "nsfw", language: str | None = None) -> dict[str, Any]:
        unique_sources = sorted(set(self.sources))
        reason = "; ".join(dict.fromkeys(r for r in self.reasons if r)) or None
        return {
            "start": self.start,
            "end": self.end,
            "kind": kind,
            "reason": reason,
            "language": language,
            "confidence": min(1.0, 0.5 + 0.15 * self.votes),
            "votes": self.votes,
            "sources": unique_sources,
        }


def _overlap_ratio(a: tuple[float, float], b: tuple[float, float]) -> float:
    """Доля пересечения относительно меньшего из интервалов."""
    left = max(a[0], b[0])
    right = min(a[1], b[1])
    if right <= left:
        return 0.0
    shorter = min(a[1] - a[0], b[1] - b[0])
    return (right - left) / shorter if shorter > 0 else 0.0


def cluster_segments(
    segments: Iterable[dict[str, Any]],
    min_overlap: float = DEFAULT_MIN_OVERLAP,
) -> list[Cluster]:
    """Сгруппировать интервалы, относящиеся к одной сцене.

    Два интервала попадают в одну группу, если пересекаются хотя бы на
    ``min_overlap`` от более короткого из них.
    """
    items = sorted(
        (s for s in segments if float(s["end"]) > float(s["start"])),
        key=lambda s: (float(s["start"]), float(s["end"])),
    )
    clusters: list[Cluster] = []
    for seg in items:
        start, end = float(seg["start"]), float(seg["end"])
        source = str(seg.get("source") or seg.get("provider") or seg.get("author") or "?")
        reason = seg.get("reason")
        target: Cluster | None = None
        best = 0.0
        for cluster in clusters:
            ratio = _overlap_ratio((start, end), (cluster.start, cluster.end))
            if ratio >= min_overlap and ratio > best:
                target, best = cluster, ratio
        if target is None:
            clusters.append(
                Cluster(start=start, end=end, votes=1, sources=[source],
                        reasons=[reason] if reason else [], _starts=[start], _ends=[end])
            )
            continue
        target.add(start, end, source, reason)
    clusters.sort(key=lambda c: c.start)
    return clusters


def _median(values: list[float]) -> float:
    ordered = sorted(values)
    middle = len(ordered) // 2
    if not ordered:
        return 0.0
    if len(ordered) % 2:
        return float(ordered[middle])
    return (ordered[middle - 1] + ordered[middle]) / 2


def build_consensus(
    segments: Iterable[dict[str, Any]],
    *,
    mode: Mode = "consensus",
    min_votes: int = DEFAULT_MIN_VOTES,
    min_overlap: float = DEFAULT_MIN_OVERLAP,
    language: str | None = None,
    kind: str = "nsfw",
) -> list[dict[str, Any]]:
    """Итоговые тайминги из нескольких источников.

    Возвращает список сегментов в том же формате, что и провайдеры, поэтому
    результат можно сразу класть в базу.
    """
    materialized = list(segments)
    if not materialized:
        return []
    clusters = cluster_segments(materialized, min_overlap=min_overlap)
    sources_total = len({
        str(s.get("source") or s.get("provider") or s.get("author") or "?") for s in materialized
    })

    if mode == "union":
        keep = clusters
    elif mode == "strict":
        keep = [c for c in clusters if c.votes >= max(2, sources_total)]
    else:
        threshold = max(1, int(min_votes))
        keep = [c for c in clusters if c.votes >= threshold]

    return [c.as_segment(kind=kind, language=language) for c in keep]


def describe_consensus(segments: Iterable[dict[str, Any]], **kwargs: Any) -> dict[str, Any]:
    """Короткая сводка для UI: сколько сцен, сколько голосов, что отброшено."""
    materialized = list(segments)
    clusters = cluster_segments(materialized, min_overlap=kwargs.get("min_overlap", DEFAULT_MIN_OVERLAP))
    kept = build_consensus(materialized, **kwargs)
    return {
        "sources": len({
            str(s.get("source") or s.get("provider") or s.get("author") or "?")
            for s in materialized
        }),
        "raw_segments": len(materialized),
        "clusters": len(clusters),
        "kept": len(kept),
        "dropped": len(clusters) - len(kept),
        "max_votes": max((c.votes for c in clusters), default=0),
    }
