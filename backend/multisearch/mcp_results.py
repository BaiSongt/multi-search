import asyncio
import re
from base64 import b64encode
from hashlib import sha256
from math import ceil
from typing import Annotated, Literal
from uuid import uuid4

from pydantic import BaseModel, Field, StringConstraints
from redis.asyncio import Redis
from redis.exceptions import RedisError

from .errors import ApiError
from .identity import MachineIdentity

_PREFIX = "multisearch:mcp:results:v1"
_MAX_PAYLOAD_BYTES = 4 * 1024 * 1024
_PAGE_SIZE = 6_000
_MAX_PAGE_COUNT = ceil(ceil(_MAX_PAYLOAD_BYTES / 3) * 4 / _PAGE_SIZE)
_TTL_MS = 300_000
_CAPACITY = 128
_TENANT_CAPACITY = 64
_GLOBAL_BYTES = 24 * 1024 * 1024
_TENANT_BYTES = 8 * 1024 * 1024
_RATE_WINDOW_MS = 60_000
_TENANT_RATE = 480
_KEY_RATE = 240
_REDIS_TIMEOUT = 0.35
_RESULT_ID = r"^[0-9a-f]{64}$"
_NONCE_SIZE = 32

# The reservation and data key share a server-clock expiration. Reserve first so
# a failed script can never leave an uncounted live payload.
_PUT = """
local ttl = redis.call('PTTL', KEYS[2])
if ttl >= 0 then return {0, ttl, redis.call('GETRANGE', KEYS[2], 0, tonumber(ARGV[8]) - 1)} end
if ttl == -1 then return redis.error_reply('MCP result has no expiration') end
local time = redis.call('TIME')
local now = time[1] * 1000 + math.floor(time[2] / 1000)
redis.call('ZREMRANGEBYSCORE', KEYS[1], '-inf', now)
local entry_limit = tonumber(ARGV[1])
local entries = redis.call('ZRANGE', KEYS[1], 0, entry_limit - 1, 'WITHSCORES')
if #entries >= entry_limit * 2 then
    return {1, math.max(1, tonumber(entries[2]) - now)}
end
local total, tenant_total = #ARGV[3], #ARGV[3]
local tenant_expiry, tenant_entries = 0, 0
for index = 1, #entries, 2 do
    local key = entries[index]
    local bytes = redis.call('STRLEN', key)
    total = total + bytes
    if string.sub(key, 1, #ARGV[4]) == ARGV[4] then
        tenant_total = tenant_total + bytes
        tenant_entries = tenant_entries + 1
        if tenant_expiry == 0 then tenant_expiry = tonumber(entries[index + 1]) end
    end
end
local retry_at = 0
if total > tonumber(ARGV[5]) then retry_at = tonumber(entries[2]) end
if tenant_total > tonumber(ARGV[6]) or tenant_entries >= tonumber(ARGV[7]) then
    retry_at = math.max(retry_at, tenant_expiry)
end
if retry_at > 0 then
    return {1, math.max(1, retry_at - now)}
end
local deadline = now + tonumber(ARGV[2])
redis.call('ZADD', KEYS[1], deadline, KEYS[2])
local written = redis.pcall('SET', KEYS[2], ARGV[3], 'PXAT', deadline)
if written.err then
    redis.call('ZREM', KEYS[1], KEYS[2])
    return written
end
return {0, tonumber(ARGV[2]), string.sub(ARGV[3], 1, tonumber(ARGV[8]))}
"""

# Check and charge both limits atomically, including requests for unknown IDs.
# Only the requested slice leaves Redis, and existence is checked before bounds.
_GET = """
local wait = 0
local counts = {}
for index = 1, 2 do
    counts[index] = tonumber(redis.call('GET', KEYS[index]) or '0')
    if counts[index] >= tonumber(ARGV[index]) then
        wait = math.max(wait, redis.call('PTTL', KEYS[index]), 1)
    end
end
if wait > 0 then return {wait} end
for index = 1, 2 do
    if counts[index] == 0 then
        redis.call('SET', KEYS[index], 1, 'PX', ARGV[3])
    else
        redis.call('INCR', KEYS[index])
    end
end
local nonce_size = #ARGV[6]
if redis.call('GETRANGE', KEYS[3], 0, nonce_size - 1) ~= ARGV[6] then return {-1} end
local size = redis.call('STRLEN', KEYS[3]) - nonce_size
local page_size = tonumber(ARGV[4])
local page_count = math.max(1, math.ceil(size / page_size))
local page = tonumber(ARGV[5])
if page >= page_count then return {-2} end
local start = nonce_size + page * page_size
return {0, page_count, redis.call('GETRANGE', KEYS[3], start, start + page_size - 1)}
"""

ResultID = Annotated[str, StringConstraints(pattern=_RESULT_ID)]


class MCPResultReference(BaseModel):
    result_id: ResultID
    fetch_tool: Literal["get_result_page"] = "get_result_page"
    encoding: Literal["base64"] = "base64"
    page_count: int = Field(ge=1, le=_MAX_PAGE_COUNT)
    payload_bytes: int = Field(ge=0, le=_MAX_PAYLOAD_BYTES)
    expires_in_seconds: int = Field(ge=0, le=_TTL_MS // 1_000)


class MCPResultPage(BaseModel):
    result_id: ResultID
    page: int = Field(ge=0, lt=_MAX_PAGE_COUNT)
    page_count: int = Field(ge=1, le=_MAX_PAGE_COUNT)
    next_page: int | None = Field(ge=1, lt=_MAX_PAGE_COUNT)
    data_base64: str = Field(max_length=_PAGE_SIZE)


def _tenant_data_prefix(identity: MachineIdentity) -> str:
    return f"{_PREFIX}:data:{identity.tenant_id.hex}:"


def _scope_digest(identity: MachineIdentity) -> bytes:
    return sha256("\0".join(sorted(identity.scopes)).encode()).digest()


def _data_key(identity: MachineIdentity, scope_digest: bytes, payload_id: str) -> str:
    return f"{_tenant_data_prefix(identity)}{identity.key_id.hex}:{scope_digest.hex()}:{payload_id}"


def _not_found() -> ApiError:
    return ApiError(404, "MCP_RESULT_NOT_FOUND", "MCP 结果不存在或已过期")


def _page_out_of_range() -> ApiError:
    return ApiError(422, "MCP_RESULT_PAGE_OUT_OF_RANGE", "MCP 结果页码超出范围")


class MCPResultStore:
    def __init__(self, redis: Redis):
        self.redis = redis

    async def _eval(self, script: str, key_count: int, *args: str | int | bytes):
        try:
            async with asyncio.timeout(_REDIS_TIMEOUT):
                return await self.redis.eval(script, key_count, *args)
        except (RedisError, TimeoutError):
            raise ApiError(
                503, "MCP_RESULT_STORE_UNAVAILABLE", "MCP 临时结果存储不可用", retryable=True
            ) from None

    async def put(self, identity: MachineIdentity, payload: bytes) -> MCPResultReference:
        if len(payload) > _MAX_PAYLOAD_BYTES:
            raise ApiError(
                413,
                "MCP_RESULT_TOO_LARGE",
                "MCP 结果超过临时存储上限，请减少结果数或通过 REST 获取任务全文",
            )
        # Hash the owner namespace with the payload for stable retries across workers.
        scopes = _scope_digest(identity)
        digest = sha256(identity.tenant_id.bytes + identity.key_id.bytes + scopes)
        digest.update(payload)
        payload_id = digest.hexdigest()[:32]
        encoded = b64encode(payload)
        nonce = uuid4().hex.encode("ascii")
        full, ttl, *stored = await self._eval(
            _PUT,
            2,
            f"{_PREFIX}:capacity",
            _data_key(identity, scopes, payload_id),
            _CAPACITY,
            _TTL_MS,
            nonce + encoded,
            _tenant_data_prefix(identity),
            _GLOBAL_BYTES,
            _TENANT_BYTES,
            _TENANT_CAPACITY,
            _NONCE_SIZE,
        )
        if full:
            raise ApiError(
                503,
                "MCP_RESULT_STORE_FULL",
                "MCP 临时结果存储已满",
                retryable=True,
                retry_after_ms=ttl,
            )
        stored_nonce = stored[0].decode("ascii") if isinstance(stored[0], bytes) else stored[0]
        return MCPResultReference(
            result_id=payload_id + stored_nonce,
            page_count=max(1, ceil(len(encoded) / _PAGE_SIZE)),
            payload_bytes=len(payload),
            expires_in_seconds=ceil(ttl / 1_000),
        )

    async def get(self, identity: MachineIdentity, result_id: str, page: int) -> MCPResultPage:
        if not re.fullmatch(_RESULT_ID, result_id):
            raise _not_found()
        if type(page) is not int or page < 0:
            raise _page_out_of_range()
        tenant = identity.tenant_id.hex
        status, *result = await self._eval(
            _GET,
            3,
            f"{_PREFIX}:rate:tenant:{tenant}",
            f"{_PREFIX}:rate:key:{tenant}:{identity.key_id.hex}",
            _data_key(identity, _scope_digest(identity), result_id[:32]),
            _TENANT_RATE,
            _KEY_RATE,
            _RATE_WINDOW_MS,
            _PAGE_SIZE,
            min(page, _MAX_PAGE_COUNT),
            result_id[32:],
        )
        if status > 0:
            raise ApiError(
                429,
                "MCP_RESULT_RATE_LIMITED",
                "MCP 结果分页读取频率已达上限",
                retryable=True,
                retry_after_ms=status,
            )
        if status == -1:
            raise _not_found()
        if status == -2:
            raise _page_out_of_range()
        page_count, data = result
        return MCPResultPage(
            result_id=result_id,
            page=page,
            page_count=page_count,
            next_page=page + 1 if page + 1 < page_count else None,
            data_base64=data,
        )
