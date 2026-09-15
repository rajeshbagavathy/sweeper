from __future__ import annotations

import threading
from pathlib import Path

import src.web.state as state_mod
from src.runner import AccountConfig
from src.web.models import SweepUIConfig


class _FakeResults:
    metrics: dict = {}


class _FakeSelectors:
    results = _FakeResults()


def _run_state_run(monkeypatch, tmp_path, cfg, env: dict[str, str]):
    monkeypatch.setattr(state_mod, "load_selectors", lambda path: _FakeSelectors())
    for key, value in env.items():
        monkeypatch.setenv(key, value)

    recorded: dict = {}

    def fake_run_sweep_multiprocess(combos, selectors, csv_path, log_path, fieldnames, **kwargs):
        recorded.update(kwargs)
        return {"ok": 0, "error": 0, "skipped": 0}

    monkeypatch.setattr(state_mod, "run_sweep_multiprocess", fake_run_sweep_multiprocess)

    rs = state_mod.RunState()
    rs._run(cfg, combos=[{"a": 1}], csv_path=tmp_path / "out.csv", fieldnames=["combo_id"], stop_event=threading.Event(), existing_statuses={})
    return recorded


def test_single_account_unaffected_when_account2_disabled(tmp_path, monkeypatch):
    """parallelism_account2=0 (the default) must behave exactly like before this
    feature existed - no `accounts` list at all, single email/password used."""
    cfg = SweepUIConfig(parallelism=3, parallelism_account2=0)
    recorded = _run_state_run(
        monkeypatch, tmp_path, cfg,
        env={"ALGOTEST_EMAIL": "one@x.com", "ALGOTEST_PASSWORD": "pw1"},
    )
    assert recorded["accounts"] is None
    assert recorded["email"] == "one@x.com"
    assert recorded["password"] == "pw1"
    assert recorded["parallelism"] == 3


def test_second_account_builds_correct_account_list(tmp_path, monkeypatch):
    cfg = SweepUIConfig(parallelism=4, parallelism_account2=2)
    recorded = _run_state_run(
        monkeypatch, tmp_path, cfg,
        env={
            "ALGOTEST_EMAIL": "one@x.com", "ALGOTEST_PASSWORD": "pw1",
            "ALGOTEST_EMAIL_2": "two@x.com", "ALGOTEST_PASSWORD_2": "pw2",
        },
    )
    accounts = recorded["accounts"]
    assert accounts is not None
    assert len(accounts) == 6  # 4 + 2, one AccountConfig per worker slot

    from src import browser

    assert accounts[:4] == [AccountConfig("one@x.com", "pw1", browser.PROFILE_DIR)] * 4
    assert accounts[4:] == [AccountConfig("two@x.com", "pw2", browser.PROFILE_DIR_2)] * 2


def test_account2_alone_still_triggers_multiprocess_path(tmp_path, monkeypatch):
    """Even with parallelism=1 (today's "sequential" value), enabling account 2
    workers must still go through run_sweep_multiprocess - there's no sequential
    mode that can split across two accounts."""
    cfg = SweepUIConfig(parallelism=1, parallelism_account2=3)
    recorded = _run_state_run(
        monkeypatch, tmp_path, cfg,
        env={
            "ALGOTEST_EMAIL": "one@x.com", "ALGOTEST_PASSWORD": "pw1",
            "ALGOTEST_EMAIL_2": "two@x.com", "ALGOTEST_PASSWORD_2": "pw2",
        },
    )
    accounts = recorded["accounts"]
    assert len(accounts) == 1 + 3


def test_third_account_builds_correct_account_list(tmp_path, monkeypatch):
    """A 3rd account slots in the same way as the 2nd - its own env vars, its own
    profile dir, appended after accounts 1 and 2, with no combo ever assigned to
    more than one account (guaranteed by run_sweep_multiprocess's single shared
    queue, exercised separately in test_runner.py)."""
    cfg = SweepUIConfig(parallelism=2, parallelism_account2=2, parallelism_account3=3)
    recorded = _run_state_run(
        monkeypatch, tmp_path, cfg,
        env={
            "ALGOTEST_EMAIL": "one@x.com", "ALGOTEST_PASSWORD": "pw1",
            "ALGOTEST_EMAIL_2": "two@x.com", "ALGOTEST_PASSWORD_2": "pw2",
            "ALGOTEST_EMAIL_3": "three@x.com", "ALGOTEST_PASSWORD_3": "pw3",
        },
    )
    accounts = recorded["accounts"]
    assert accounts is not None
    assert len(accounts) == 7  # 2 + 2 + 3

    from src import browser

    assert accounts[:2] == [AccountConfig("one@x.com", "pw1", browser.PROFILE_DIR)] * 2
    assert accounts[2:4] == [AccountConfig("two@x.com", "pw2", browser.PROFILE_DIR_2)] * 2
    assert accounts[4:] == [AccountConfig("three@x.com", "pw3", browser.PROFILE_DIR_3)] * 3


def test_third_account_alone_still_triggers_multiprocess_path(tmp_path, monkeypatch):
    """Account 3 enabled on its own (account 2 disabled) must still build a
    2-account list and go through run_sweep_multiprocess."""
    cfg = SweepUIConfig(parallelism=1, parallelism_account2=0, parallelism_account3=2)
    recorded = _run_state_run(
        monkeypatch, tmp_path, cfg,
        env={
            "ALGOTEST_EMAIL": "one@x.com", "ALGOTEST_PASSWORD": "pw1",
            "ALGOTEST_EMAIL_3": "three@x.com", "ALGOTEST_PASSWORD_3": "pw3",
        },
    )
    accounts = recorded["accounts"]
    assert len(accounts) == 1 + 2
