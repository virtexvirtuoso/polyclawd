"""Tests for services.task_state — restart-proof wall-clock task gating."""

from services import task_state


def test_first_call_runs_and_persists(tmp_path):
    db = tmp_path / "sched_state.db"
    assert task_state.should_run("scan_a", 1800, now=1000.0, db_path=db) is True
    assert task_state.should_run("scan_a", 1800, now=1001.0, db_path=db) is False


def test_runs_again_after_interval(tmp_path):
    db = tmp_path / "sched_state.db"
    assert task_state.should_run("scan_b", 1800, now=1000.0, db_path=db)
    assert not task_state.should_run("scan_b", 1800, now=2799.0, db_path=db)
    assert task_state.should_run("scan_b", 1800, now=2800.0, db_path=db)


def test_state_survives_restart(tmp_path):
    db = tmp_path / "sched_state.db"
    assert task_state.should_run("scan_c", 3600, now=1000.0, db_path=db)
    assert not task_state.should_run("scan_c", 3600, now=1100.0, db_path=db)


def test_tasks_are_independent(tmp_path):
    db = tmp_path / "sched_state.db"
    assert task_state.should_run("x", 600, now=50.0, db_path=db)
    assert task_state.should_run("y", 600, now=50.0, db_path=db)


def test_seed_marks_ran_without_running(tmp_path):
    db = tmp_path / "sched_state.db"
    task_state.seed(["a", "b"], now=1000.0, db_path=db)
    assert task_state.should_run("a", 1800, now=1500.0, db_path=db) is False
    assert task_state.should_run("a", 1800, now=2800.0, db_path=db) is True
    # seeding never overwrites an existing row: last_run stays 2800 (from the
    # run above), so "a" is due again at 4600. Had seed overwritten it to
    # 9999, this would be False.
    task_state.seed(["a"], now=9999.0, db_path=db)
    assert task_state.should_run("a", 1800, now=2899.0, db_path=db) is False
    assert task_state.should_run("a", 1800, now=4600.0, db_path=db) is True


def test_should_run_safe_fails_closed(tmp_path, monkeypatch):
    def boom(*a, **kw):
        raise RuntimeError("db exploded")

    monkeypatch.setattr(task_state, "should_run", boom)
    assert task_state.should_run_safe("x", 600, db_path=tmp_path / "s.db") is False
