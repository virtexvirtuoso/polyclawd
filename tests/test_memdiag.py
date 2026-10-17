"""Tests for the self-removing scheduler memory diagnostic (services/memdiag.py)."""
import time
import tracemalloc

import pytest

from services import memdiag


@pytest.fixture
def deadline_file(tmp_path, monkeypatch):
    p = tmp_path / "memdiag.deadline"
    monkeypatch.setattr(memdiag, "DEADLINE_FILE", p)
    yield p


@pytest.fixture
def clean_tracemalloc():
    yield
    try:
        tracemalloc.stop()
    except Exception:
        pass


class TestArmAndStart:
    def test_arms_when_no_deadline_file(self, deadline_file, clean_tracemalloc):
        import logging
        assert not deadline_file.exists()
        assert memdiag.arm_and_start(logging.getLogger("test")) is True
        assert deadline_file.exists()
        dl = float(deadline_file.read_text().strip())
        assert dl > time.time()
        assert dl <= time.time() + memdiag.WINDOW_S + 5
        assert tracemalloc.is_tracing()

    def test_inert_after_deadline(self, deadline_file, clean_tracemalloc):
        import logging
        deadline_file.write_text(str(time.time() - 10))
        assert memdiag.arm_and_start(logging.getLogger("test")) is False
        assert not tracemalloc.is_tracing()

    def test_resumes_within_window(self, deadline_file, clean_tracemalloc):
        import logging
        deadline_file.write_text(str(time.time() + 3600))
        assert memdiag.arm_and_start(logging.getLogger("test")) is True
        assert tracemalloc.is_tracing()

    def test_unwritable_deadline_disables_probe(self, tmp_path, monkeypatch, clean_tracemalloc):
        import logging
        bad = tmp_path / "no_such_dir" / "memdiag.deadline"
        monkeypatch.setattr(memdiag, "DEADLINE_FILE", bad)
        assert memdiag.arm_and_start(logging.getLogger("test")) is False
        assert not tracemalloc.is_tracing()


class TestHelpers:
    def test_anon_bytes_returns_int_or_none(self):
        v = memdiag._anon_bytes()
        assert v is None or (isinstance(v, int) and v >= 0)

    def test_constants_are_bounded(self):
        assert memdiag.N_FRAMES == 25
        assert memdiag.TOP_N == 25
        assert memdiag.DIFF_EVERY_S == 6 * 3600
        assert memdiag.WINDOW_S == 8 * 3600