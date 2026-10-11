"""Тесты консенсуса таймингов: «сцену размечали разные люди — кому верить».

Это защита от главной ошибки стримера: взять тайминг одного случайного человека,
промахнуться на пару секунд и получить страйк от Twitch.
"""
from __future__ import annotations

import pytest

from streamblur.consensus import (
    Cluster,
    build_consensus,
    cluster_segments,
    describe_consensus,
)


def seg(start: float, end: float, source: str, reason: str | None = None) -> dict:
    return {"start": start, "end": end, "source": source, "reason": reason}


# --------------------------------------------------------------- кластеризация
def test_same_scene_from_different_people_becomes_one_cluster():
    clusters = cluster_segments(
        [
            seg(724, 871, "user1", "драка"),
            seg(731, 880, "user2", "бой"),
            seg(720, 860, "user3", None),
        ]
    )
    assert len(clusters) == 1
    assert clusters[0].votes == 3


def test_different_scenes_stay_apart():
    clusters = cluster_segments(
        [seg(100, 130, "a"), seg(3000, 3060, "b"), seg(105, 135, "c")]
    )
    assert len(clusters) == 2
    assert [c.votes for c in clusters] == [2, 1]


def test_cluster_bounds_use_median_not_outlier():
    """Один человек промахнулся на минуту — граница не должна уехать."""
    clusters = cluster_segments(
        [
            seg(600, 700, "a"),
            seg(602, 702, "b"),
            seg(604, 704, "c"),
            seg(540, 640, "промахнувшийся"),
        ],
        min_overlap=0.3,
    )
    assert len(clusters) == 1
    assert 595 <= clusters[0].start <= 610
    assert 695 <= clusters[0].end <= 710


def test_empty_input():
    assert cluster_segments([]) == []
    assert build_consensus([]) == []


def test_inverted_segments_ignored():
    assert cluster_segments([seg(100, 50, "a")]) == []


# ------------------------------------------------------------------ режимы
SAMPLE = [
    seg(600, 700, "user1", "драка"),
    seg(605, 705, "user2", "бой"),
    seg(3000, 3060, "user1", "ню"),   # отметил только один
]


def test_consensus_default_keeps_everything_with_one_vote():
    result = build_consensus(SAMPLE, min_votes=1)
    assert len(result) == 2


def test_consensus_with_two_votes_drops_single_reports():
    result = build_consensus(SAMPLE, min_votes=2)
    assert len(result) == 1
    assert result[0]["start"] == pytest.approx(602.5)
    assert result[0]["votes"] == 2
    assert result[0]["sources"] == ["user1", "user2"]


def test_union_mode_keeps_all_clusters():
    result = build_consensus(SAMPLE, mode="union", min_votes=5)
    assert len(result) == 2


def test_strict_mode_requires_all_sources():
    result = build_consensus(SAMPLE, mode="strict")
    # два независимых источника, сцену «драка» отметили оба → остаётся только она
    assert len(result) == 1
    assert result[0]["reason"] == "драка; бой"


def test_result_shape_is_provider_compatible():
    """Результат можно сразу класть в базу таймингов."""
    result = build_consensus(SAMPLE, min_votes=1)
    for item in result:
        assert {"start", "end", "kind", "reason", "confidence"} <= set(item)
        assert item["end"] > item["start"]
        assert 0.0 < item["confidence"] <= 1.0


def test_confidence_grows_with_votes():
    one = build_consensus([seg(10, 20, "a")], min_votes=1)[0]
    many = build_consensus(
        [seg(10, 20, "a"), seg(10, 20, "b"), seg(10, 20, "c")], min_votes=1
    )[0]
    assert many["confidence"] > one["confidence"]


def test_describe_consensus_summary():
    summary = describe_consensus(SAMPLE, min_votes=2)
    assert summary["sources"] == 2
    assert summary["raw_segments"] == 3
    assert summary["clusters"] == 2
    assert summary["kept"] == 1
    assert summary["dropped"] == 1
    assert summary["max_votes"] == 2


def test_cluster_add_is_idempotent_for_bounds():
    cluster = Cluster(start=100, end=200, sources=["a"], _starts=[100], _ends=[200])
    cluster.add(100, 200, "b", None)
    assert cluster.start == 100 and cluster.end == 200
    assert cluster.votes == 2


# ------------------------------------------------- интеграция с провайдером
def test_link_auto_provider_uses_consensus(monkeypatch):
    from streamblur.timings import get_provider

    page = """
    <script type="application/json">{"scenes":[{"start":600,"end":700,"reason":"драка"}]}</script>
    <div data-start="0:10:05" data-end="0:11:45">x</div>
    <div data-start="0:50:00" data-end="0:51:00">одиночная отметка</div>
    """
    provider = get_provider("link-auto", {
        "url": "https://example.com/movie/1",
        "consensus_mode": "consensus",
        "min_votes": 2,
    })
    monkeypatch.setattr(provider, "fetch_page", lambda url: page)
    result = provider.fetch({"title": "Фильм"})
    # две отметки одной сцены (json + data-атрибут) проходят порог, одиночная — нет
    assert len(result.segments) == 1
    assert "консенсуса: 1" in result.message
    assert "отброшено 1" in result.message


def test_link_auto_provider_union_mode_keeps_single_reports(monkeypatch):
    from streamblur.timings import get_provider

    page = """
    <script type="application/json">{"scenes":[{"start":600,"end":700}]}</script>
    <div data-start="0:50:00" data-end="0:51:00">одиночная отметка</div>
    """
    provider = get_provider("link-auto", {
        "url": "https://example.com/movie/1",
        "consensus_mode": "union",
    })
    monkeypatch.setattr(provider, "fetch_page", lambda url: page)
    result = provider.fetch({"title": "Фильм"})
    assert len(result.segments) == 2
