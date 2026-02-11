"""Unit tests for StorageService."""
import pytest
import asyncio
from pathlib import Path
from api.services.storage import StorageService


@pytest.fixture
def storage(tmp_path):
    """Create a StorageService with temp directory."""
    return StorageService(tmp_path)


@pytest.mark.asyncio
async def test_load_nonexistent_returns_default(storage):
    """Non-existent files should return the default value."""
    result = await storage.load("missing.json", {"default": True})
    assert result == {"default": True}


@pytest.mark.asyncio
async def test_load_nonexistent_returns_empty_dict_when_no_default(storage):
    """Non-existent files with no default should return empty dict."""
    result = await storage.load("missing.json")
    assert result == {}


@pytest.mark.asyncio
async def test_save_and_load_roundtrip(storage):
    """Data should be preserved through save and load cycle."""
    await storage.save("test.json", {"key": "value", "number": 42})
    result = await storage.load("test.json")
    assert result == {"key": "value", "number": 42}


@pytest.mark.asyncio
async def test_save_creates_file(storage):
    """Save should create the file on disk."""
    await storage.save("new_file.json", {"data": 123})
    assert (storage.base_dir / "new_file.json").exists()


@pytest.mark.asyncio
async def test_append_adds_item_with_timestamp(storage):
    """Append should add item with timestamp."""
    await storage.append_to_list("items.json", {"id": 1})
    data = await storage.load("items.json")
    assert len(data) == 1
    assert data[0]["id"] == 1
    assert "timestamp" in data[0]


@pytest.mark.asyncio
async def test_append_respects_max_items(storage):
    """Append should truncate when max_items is exceeded."""
    for i in range(15):
        await storage.append_to_list("items.json", {"id": i}, max_items=10)
    data = await storage.load("items.json")
    assert len(data) == 10
    # First 5 should be trimmed (items 0-4)
    assert data[0]["id"] == 5


@pytest.mark.asyncio
async def test_corrupted_json_returns_default(storage):
    """Corrupted JSON should return default without raising."""
    path = storage.base_dir / "corrupted.json"
    path.write_text("not valid json {{{")
    result = await storage.load("corrupted.json", {"fallback": True})
    assert result == {"fallback": True}


@pytest.mark.asyncio
async def test_path_traversal_blocked_double_dot(storage):
    """Path traversal with .. should be blocked."""
    with pytest.raises(ValueError, match="Invalid filename"):
        await storage.load("../etc/passwd")


@pytest.mark.asyncio
async def test_path_traversal_blocked_absolute(storage):
    """Path traversal with / should be blocked."""
    with pytest.raises(ValueError, match="Invalid filename"):
        await storage.load("/etc/passwd")


@pytest.mark.asyncio
async def test_concurrent_writes_are_safe(storage):
    """Verify asyncio.Lock prevents race conditions."""
    async def writer(n):
        for i in range(20):
            await storage.append_to_list("concurrent.json", {"writer": n, "i": i}, max_items=1000)

    await asyncio.gather(*[writer(n) for n in range(5)])
    data = await storage.load("concurrent.json")
    assert len(data) == 100  # 5 writers x 20 each


def test_load_sync_works(storage):
    """Synchronous load should work for startup contexts."""
    # First save a file
    (storage.base_dir / "sync_test.json").write_text('{"sync": true}')
    result = storage.load_sync("sync_test.json")
    assert result == {"sync": True}


def test_load_sync_returns_default_for_missing(storage):
    """Synchronous load should return default for missing files."""
    result = storage.load_sync("missing.json", {"default": True})
    assert result == {"default": True}
