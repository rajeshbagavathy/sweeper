from __future__ import annotations

import pytest

from src.web import portfolio_sweep_favorites as fav


def test_list_favorites_empty_when_no_file(tmp_path, monkeypatch):
    monkeypatch.setattr(fav, "FAVORITES_PATH", tmp_path / "favorites.json")
    assert fav.list_favorites() == []


def test_save_and_list_favorite_roundtrip(tmp_path, monkeypatch):
    monkeypatch.setattr(fav, "FAVORITES_PATH", tmp_path / "favorites.json")
    params = {"threshold": 0.3, "top_n": 300, "min_lots": 3, "max_lots": 7}
    result = {"reward_risk_ratio": 5.13, "overall_profit": 2500000}

    fav.save_favorite("best combo", params, result)

    listed = fav.list_favorites()
    assert len(listed) == 1
    assert listed[0]["name"] == "best combo"
    assert listed[0]["params"] == params
    assert listed[0]["last_result"] == result
    assert listed[0]["created_at"]
    assert listed[0]["updated_at"]


def test_save_favorite_rejects_empty_name(tmp_path, monkeypatch):
    monkeypatch.setattr(fav, "FAVORITES_PATH", tmp_path / "favorites.json")
    with pytest.raises(ValueError):
        fav.save_favorite("   ", {"threshold": 0.3})


def test_save_favorite_overwrites_same_name_keeping_created_at(tmp_path, monkeypatch):
    monkeypatch.setattr(fav, "FAVORITES_PATH", tmp_path / "favorites.json")
    fav.save_favorite("x", {"threshold": 0.25})
    first_created = fav.list_favorites()[0]["created_at"]

    fav.save_favorite("x", {"threshold": 0.3})
    listed = fav.list_favorites()

    assert len(listed) == 1
    assert listed[0]["params"] == {"threshold": 0.3}
    assert listed[0]["created_at"] == first_created  # unchanged across the overwrite


def test_delete_favorite_removes_it(tmp_path, monkeypatch):
    monkeypatch.setattr(fav, "FAVORITES_PATH", tmp_path / "favorites.json")
    fav.save_favorite("x", {"threshold": 0.25})
    fav.delete_favorite("x")
    assert fav.list_favorites() == []


def test_delete_favorite_is_a_noop_when_missing(tmp_path, monkeypatch):
    monkeypatch.setattr(fav, "FAVORITES_PATH", tmp_path / "favorites.json")
    fav.delete_favorite("does-not-exist")  # must not raise
    assert fav.list_favorites() == []


def test_list_favorites_newest_created_first(tmp_path, monkeypatch):
    monkeypatch.setattr(fav, "FAVORITES_PATH", tmp_path / "favorites.json")
    fav.save_favorite("older", {"threshold": 0.25})
    # Force a distinguishable created_at ordering without depending on real timing.
    all_fav = fav._load_all()
    all_fav["older"]["created_at"] = "2020-01-01T00:00:00+00:00"
    fav._save_all(all_fav)
    fav.save_favorite("newer", {"threshold": 0.3})

    names = [f["name"] for f in fav.list_favorites()]
    assert names == ["newer", "older"]
