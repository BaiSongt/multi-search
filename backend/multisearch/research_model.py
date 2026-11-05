import asyncio
import json
from dataclasses import dataclass, field
from typing import Protocol
from urllib.parse import urlsplit, urlunsplit

import httpx

_MAX_RESPONSE_BYTES = 256 * 1024
_MIN_OUTPUT_TOKENS = 32
_MAX_OUTPUT_TOKENS = 1024
_OLLAMA_RAW_FRAMING = 64


class ResearchModelError(Exception):
    """给后台任务返回固定错误码，避免泄漏模型端点、令牌或原始响应。"""


@dataclass(frozen=True)
class ModelPlan:
    input_token_limit: int
    output_token_limit: int
    payload: dict = field(repr=False)

    @property
    def reservation(self) -> int:
        return self.input_token_limit + self.output_token_limit


@dataclass(frozen=True)
class ModelGeneration:
    text: str = field(repr=False)
    input_tokens: int | None
    output_tokens: int | None


class ResearchModel(Protocol):
    async def prepare(
        self,
        prompt: str,
        available_tokens: int,
        deadline: float,
    ) -> ModelPlan | None: ...

    async def generate(self, plan: ModelPlan, deadline: float) -> ModelGeneration: ...


def _token_count(value: object) -> int | None:
    return value if type(value) is int and 0 <= value <= 2**31 - 1 else None


def _endpoint(base_url: str, suffix: str, *, openai: bool = False) -> str:
    try:
        parts = urlsplit(base_url)
        if (
            parts.scheme not in {"http", "https"}
            or not parts.hostname
            or parts.username is not None
            or parts.query
            or parts.fragment
            or any(char.isspace() or ord(char) < 32 or char == "\\" for char in base_url)
            or parts.port == 0
        ):
            raise ValueError
    except ValueError:
        raise ValueError("研究模型地址需要明确的 HTTP(S) 服务根路径") from None
    prefix = parts.path.rstrip("/")
    if openai and not prefix.endswith("/v1"):
        prefix += "/v1"
    return urlunsplit((parts.scheme, parts.netloc, prefix + suffix, "", ""))


class _HTTPModel:
    def __init__(
        self,
        client: httpx.AsyncClient,
        base_url: str,
        model: str,
        api_key: str = "",
    ):
        if not 1 <= len(model) <= 128 or any(ord(char) < 32 for char in model):
            raise ValueError("研究模型名称格式无效")
        self.client = client
        self.model = model
        self.base_url = base_url
        self.headers = {"Accept": "application/json", "Accept-Encoding": "identity"}
        if api_key:
            self.headers["Authorization"] = f"Bearer {api_key}"

    async def _post(self, url: str, payload: dict, deadline: float) -> dict:
        remaining = deadline - asyncio.get_running_loop().time()
        if remaining <= 0:
            raise ResearchModelError("TIME_BUDGET_EXCEEDED")
        try:
            async with (
                asyncio.timeout(remaining),
                self.client.stream(
                    "POST",
                    url,
                    json=payload,
                    headers=self.headers,
                    follow_redirects=False,
                    timeout=remaining,
                ) as response,
            ):
                if (
                    response.status_code != 200
                    or response.headers.get("content-encoding", "identity").lower() != "identity"
                ):
                    raise ResearchModelError("MODEL_UNAVAILABLE")
                body = bytearray()
                async for chunk in response.aiter_bytes():
                    if len(body) + len(chunk) > _MAX_RESPONSE_BYTES:
                        raise ResearchModelError("MODEL_RESPONSE_TOO_LARGE")
                    body.extend(chunk)
        except (TimeoutError, httpx.TimeoutException):
            raise ResearchModelError("TIME_BUDGET_EXCEEDED") from None
        except (httpx.HTTPError, ValueError):
            raise ResearchModelError("MODEL_UNAVAILABLE") from None
        try:
            data = json.loads(body)
        except (ValueError, RecursionError):
            raise ResearchModelError("MODEL_RESPONSE_INVALID") from None
        if not isinstance(data, dict):
            raise ResearchModelError("MODEL_RESPONSE_INVALID")
        return data


class OpenAIResearchModel(_HTTPModel):
    """兼容 Responses 及其输入计数端点的服务可提供精确的预检预算。"""

    def __init__(
        self,
        client: httpx.AsyncClient,
        base_url: str,
        model: str,
        api_key: str = "",
    ):
        super().__init__(client, base_url, model, api_key)
        self.responses_url = _endpoint(base_url, "/responses", openai=True)
        self.count_url = self.responses_url + "/input_tokens"

    async def prepare(
        self,
        prompt: str,
        available_tokens: int,
        deadline: float,
    ) -> ModelPlan | None:
        if available_tokens <= _MIN_OUTPUT_TOKENS:
            return None
        base = {"model": self.model, "input": prompt}
        count = await self._post(self.count_url, base, deadline)
        input_tokens = _token_count(count.get("input_tokens"))
        if input_tokens is None or input_tokens == 0:
            raise ResearchModelError("MODEL_USAGE_UNAVAILABLE")
        max_output = min(_MAX_OUTPUT_TOKENS, available_tokens - input_tokens)
        if max_output < _MIN_OUTPUT_TOKENS:
            return None
        return ModelPlan(
            input_tokens,
            max_output,
            {**base, "max_output_tokens": max_output, "store": False, "stream": False},
        )

    async def generate(self, plan: ModelPlan, deadline: float) -> ModelGeneration:
        data = await self._post(self.responses_url, plan.payload, deadline)
        usage = data.get("usage")
        usage = usage if isinstance(usage, dict) else {}
        output = data.get("output")
        parts = []
        if isinstance(output, list):
            for item in output:
                if not isinstance(item, dict) or item.get("type") != "message":
                    continue
                items = item.get("content")
                if not isinstance(items, list):
                    continue
                for content in items:
                    if (
                        isinstance(content, dict)
                        and content.get("type") == "output_text"
                        and isinstance(content.get("text"), str)
                    ):
                        parts.append(content["text"])
        return ModelGeneration(
            "\n".join(parts),
            _token_count(usage.get("input_tokens")),
            _token_count(usage.get("output_tokens")),
        )


class OllamaResearchModel(_HTTPModel):
    """原始提示词禁止模型模板扩写，按 UTF-8 字节数加固定余量预留。"""

    def __init__(
        self,
        client: httpx.AsyncClient,
        base_url: str,
        model: str,
        api_key: str = "",
    ):
        super().__init__(client, base_url, model, api_key)
        self.generate_url = _endpoint(base_url, "/api/generate")

    async def prepare(
        self,
        prompt: str,
        available_tokens: int,
        deadline: float,
    ) -> ModelPlan | None:
        input_limit = len(prompt.encode("utf-8")) + _OLLAMA_RAW_FRAMING
        max_output = min(_MAX_OUTPUT_TOKENS, available_tokens - input_limit)
        if max_output < _MIN_OUTPUT_TOKENS:
            return None
        return ModelPlan(
            input_limit,
            max_output,
            {
                "model": self.model,
                "prompt": prompt,
                "raw": True,
                "stream": False,
                "format": "json",
                "options": {"num_ctx": input_limit + max_output, "num_predict": max_output},
            },
        )

    async def generate(self, plan: ModelPlan, deadline: float) -> ModelGeneration:
        data = await self._post(self.generate_url, plan.payload, deadline)
        output = data.get("response")
        return ModelGeneration(
            output if data.get("done") is True and isinstance(output, str) else "",
            _token_count(data.get("prompt_eval_count")),
            _token_count(data.get("eval_count")),
        )


def create_research_model(
    client: httpx.AsyncClient,
    *,
    backend: str = "",
    base_url: str = "",
    model: str = "",
    api_key: str = "",
) -> ResearchModel | None:
    if not backend:
        if base_url or model or api_key:
            raise ValueError("提供研究模型参数时需要指定后端")
        return None
    if backend == "openai":
        return OpenAIResearchModel(client, base_url, model, api_key)
    if backend == "ollama":
        return OllamaResearchModel(client, base_url, model, api_key)
    raise ValueError("研究模型后端需要为 openai 或 ollama")
