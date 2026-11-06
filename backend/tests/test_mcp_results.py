import asyncio
import os
from base64 import b64decode
from dataclasses import replace
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
from redis.asyncio import Redis
from redis.exceptions import RedisError

from multisearch import mcp_results
from multisearch.errors import ApiError
from multisearch.identity import MachineIdentity
from multisearch.mcp_results import MCPResultStore


@pytest.fixture
def identity():
    return MachineIdentity(uuid4(), uuid4(), uuid4(), frozenset({"search:text", "fetch"}))


@pytest.fixture
async def redis(monkeypatch):
    url = os.environ.get("MULTISEARCH_TEST_MCP_REDIS_URL")
    if not url:
        pytest.skip("requires a disposable Redis instance in MULTISEARCH_TEST_MCP_REDIS_URL")
    prefix = f"multisearch:test:mcp:{uuid4().hex}"
    monkeypatch.setattr(mcp_results, "_PREFIX", prefix)
    async with Redis.from_url(url) as client:
        await client.ping()
        try:
            yield client
        finally:
            async for key in client.scan_iter(match=f"{prefix}:*"):
                await client.delete(key)


def _data_key(identity, result_id):
    return mcp_results._data_key(identity, mcp_results._scope_digest(identity), result_id[:32])


async def test_maximum_size_round_trips_with_isolation_and_exact_page_sizes(
    redis, identity, monkeypatch
):
    store = MCPResultStore(redis)
    payload = bytes(range(256)) * (4 * 1024 * 1024 // 256)
    reference = await store.put(identity, payload)
    assert reference.model_dump() == {
        "result_id": reference.result_id,
        "fetch_tool": "get_result_page",
        "encoding": "base64",
        "page_count": 933,
        "payload_bytes": 4 * 1024 * 1024,
        "expires_in_seconds": 300,
    }
    assert len(reference.result_id) == 64
    monkeypatch.setattr(mcp_results, "_KEY_RATE", 1_000)
    monkeypatch.setattr(mcp_results, "_TENANT_RATE", 1_000)
    pages = []
    for start in range(0, reference.page_count, 16):
        pages.extend(
            await asyncio.gather(
                *(
                    store.get(identity, reference.result_id, page)
                    for page in range(start, min(start + 16, reference.page_count))
                )
            )
        )
    for index, page in enumerate(pages):
        assert (page.result_id, page.page, page.page_count, page.next_page) == (
            reference.result_id,
            index,
            reference.page_count,
            index + 1 if index < len(pages) - 1 else None,
        )
        if page.next_page is not None:
            assert len(page.data_base64) == 6000
        else:
            assert 0 < len(page.data_base64) < 6000
    assert b"".join(b64decode(page.data_base64, validate=True) for page in pages) == payload

    for other in (
        replace(identity, key_id=uuid4()),
        replace(identity, tenant_id=uuid4()),
        replace(identity, scopes=frozenset({"search:text"})),
    ):
        for page in (0, 10**60):
            with pytest.raises(ApiError) as error:
                await store.get(other, reference.result_id, page)
            assert (error.value.status, error.value.code) == (404, "MCP_RESULT_NOT_FOUND")
    with pytest.raises(ApiError) as error:
        await store.get(identity, reference.result_id, reference.page_count)
    assert (error.value.status, error.value.code) == (422, "MCP_RESULT_PAGE_OUT_OF_RANGE")


async def test_duplicate_keeps_original_expiration_and_expired_slot_is_reusable(
    redis, identity, monkeypatch
):
    store = MCPResultStore(redis)
    monkeypatch.setattr(mcp_results, "_CAPACITY", 1)
    first, repeated = await asyncio.gather(
        store.put(identity, b"same"), store.put(identity, b"same")
    )
    assert first == repeated
    key = _data_key(identity, first.result_id)
    deadline = await redis.pexpiretime(key)
    assert 0 < await redis.pttl(key) <= 300_000
    assert (await store.put(identity, b"same")).result_id == first.result_id
    assert await redis.pexpiretime(key) == deadline
    with pytest.raises(ApiError) as error:
        await store.put(identity, b"other")
    assert (error.value.status, error.value.code) == (503, "MCP_RESULT_STORE_FULL")
    assert 0 < error.value.retry_after_ms <= 300_000

    now_s, now_us = await redis.time()
    past = now_s * 1000 + now_us // 1000 - 1
    await redis.pexpireat(key, past)
    await redis.zadd(f"{mcp_results._PREFIX}:capacity", {key: past})
    with pytest.raises(ApiError) as missing:
        await store.get(identity, first.result_id, 0)
    assert (missing.value.status, missing.value.code) == (404, "MCP_RESULT_NOT_FOUND")
    replacement = await store.put(identity, b"same")
    assert replacement.result_id != first.result_id
    with pytest.raises(ApiError) as stale:
        await store.get(identity, first.result_id, 0)
    assert (stale.value.status, stale.value.code) == (404, "MCP_RESULT_NOT_FOUND")
    page = await store.get(identity, replacement.result_id, 0)
    assert (page.page_count, page.next_page, b64decode(page.data_base64)) == (1, None, b"same")


async def test_concurrent_writes_enforce_global_entry_limit(redis, identity):
    stores = [MCPResultStore(redis) for _ in range(2)]
    tenants = [identity, replace(identity, tenant_id=uuid4()), replace(identity, tenant_id=uuid4())]
    attempts = []
    for start in range(0, 132, 12):
        attempts.extend(
            await asyncio.gather(
                *(
                    stores[n % 2].put(tenants[n % 3], str(n).encode())
                    for n in range(start, start + 12)
                ),
                return_exceptions=True,
            )
        )
    rejected = [result for result in attempts if isinstance(result, ApiError)]
    assert len(rejected) == 4
    assert all(error.code == "MCP_RESULT_STORE_FULL" for error in rejected)
    assert await redis.zcard(f"{mcp_results._PREFIX}:capacity") == 128


async def test_capacity_uses_encoded_bytes_and_is_shared_by_tenant(redis, identity, monkeypatch):
    store = MCPResultStore(redis)
    monkeypatch.setattr(mcp_results, "_GLOBAL_BYTES", 96)
    monkeypatch.setattr(mcp_results, "_TENANT_BYTES", 48)
    other_key = replace(identity, key_id=uuid4())
    other_tenant = replace(identity, tenant_id=uuid4())
    third_tenant = replace(identity, tenant_id=uuid4())
    await store.put(identity, b"a" * 12)
    with pytest.raises(ApiError, match="已满"):
        await store.put(other_key, b"b")
    await store.put(other_tenant, b"a" * 12)
    with pytest.raises(ApiError, match="已满"):
        await store.put(third_tenant, b"b")
    assert await redis.zcard(f"{mcp_results._PREFIX}:capacity") == 2


async def test_tenant_cannot_fill_shared_slots_with_small_payloads(redis, identity, monkeypatch):
    monkeypatch.setattr(mcp_results, "_TENANT_CAPACITY", 1)
    store = MCPResultStore(redis)
    await store.put(identity, b"one")
    with pytest.raises(ApiError) as error:
        await store.put(replace(identity, key_id=uuid4()), b"two")
    assert error.value.code == "MCP_RESULT_STORE_FULL"
    assert await store.put(replace(identity, tenant_id=uuid4()), b"three")


async def test_read_limits_count_misses_and_are_shared_by_key_and_tenant(redis, identity):
    store = MCPResultStore(redis)
    reference = await store.put(identity, b"result")
    missing_id = "0" * 64
    second_key = replace(identity, key_id=uuid4())
    for key in (identity, second_key):
        for _ in range(240):
            with pytest.raises(ApiError) as missing:
                await store.get(key, missing_id, 0)
            assert missing.value.code == "MCP_RESULT_NOT_FOUND"
        with pytest.raises(ApiError) as error:
            await store.get(key, reference.result_id, 0)
        assert (error.value.status, error.value.code) == (429, "MCP_RESULT_RATE_LIMITED")
        assert 0 < error.value.retry_after_ms <= 60_000

    for key in (
        replace(identity, key_id=uuid4()),
        replace(identity, scopes=frozenset({"fetch"})),
    ):
        with pytest.raises(ApiError) as error:
            await store.get(key, reference.result_id, 0)
        assert error.value.code == "MCP_RESULT_RATE_LIMITED"
    with pytest.raises(ApiError) as missing:
        await store.get(replace(identity, tenant_id=uuid4()), reference.result_id, 0)
    assert missing.value.code == "MCP_RESULT_NOT_FOUND"


@pytest.mark.parametrize("operation", ["put", "get"])
@pytest.mark.parametrize("failure", ["redis", "timeout"])
async def test_storage_faults_have_a_redacted_retryable_error(
    identity, monkeypatch, operation, failure
):
    async def fail(*args):
        if failure == "redis":
            raise RedisError("sensitive implementation detail")
        await asyncio.sleep(10)

    monkeypatch.setattr(mcp_results, "_REDIS_TIMEOUT", 0.005)
    redis = AsyncMock(spec=Redis)
    redis.eval.side_effect = fail
    store = MCPResultStore(redis)
    with pytest.raises(ApiError) as error:
        if operation == "put":
            await store.put(identity, b"sensitive payload")
        else:
            await store.get(identity, "a" * 64, 0)
    assert (error.value.status, error.value.code, error.value.retryable) == (
        503,
        "MCP_RESULT_STORE_UNAVAILABLE",
        True,
    )
    assert "sensitive" not in str(error.value)


async def test_overlarge_payload_and_malformed_result_handle_do_not_call_redis(identity):
    redis = AsyncMock(spec=Redis)
    store = MCPResultStore(redis)
    with pytest.raises(ApiError) as error:
        await store.put(identity, b"a" * (4 * 1024 * 1024 + 1))
    assert (error.value.status, error.value.code) == (413, "MCP_RESULT_TOO_LARGE")
    with pytest.raises(ApiError) as missing:
        await store.get(identity, "contains-private-input", 0)
    assert (missing.value.status, missing.value.code) == (404, "MCP_RESULT_NOT_FOUND")
    assert "contains-private-input" not in str(missing.value)
    redis.eval.assert_not_called()
