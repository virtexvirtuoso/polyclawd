"""Credit-cache dir resolution must SELF-CREATE the prod path when the deploy
tree exists, instead of silently falling back to a dev path (the 2026-06-25
footgun: the prod cache dir was missing, so all credit/usage tracking silently
went to ~/Desktop/polyclawd/cache on the VPS).

Run: venv/bin/python -m pytest tests/test_rate_limiter_cache_dir.py -v --noconftest
"""
from odds.rate_limiter import _resolve_cache_dir


def test_uses_prod_when_it_exists(tmp_path):
    prod = tmp_path / "prod"; prod.mkdir()
    dev = tmp_path / "dev"
    assert _resolve_cache_dir(prod, dev) == prod
    assert not dev.exists()


def test_self_creates_prod_when_deploy_tree_exists(tmp_path):
    # prod dir missing but its PARENT (the deploy tree) exists -> create prod,
    # do NOT fall back. This is the durability fix.
    tree = tmp_path / "polyclawd"; tree.mkdir()
    prod = tree / "cache"            # parent exists, prod does not
    dev = tmp_path / "dev"
    got = _resolve_cache_dir(prod, dev)
    assert got == prod and prod.is_dir()
    assert not dev.exists()          # must NOT have fallen back


def test_falls_back_only_when_prod_tree_absent(tmp_path):
    # /var/www/... entirely absent (a real dev machine) -> use dev path.
    prod = tmp_path / "missing_tree" / "cache"   # parent does not exist
    dev = tmp_path / "dev"
    got = _resolve_cache_dir(prod, dev)
    assert got == dev and dev.is_dir()
