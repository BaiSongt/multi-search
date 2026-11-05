"""对 PDF 子进程施加并发、总时限和协议输出限制。"""

import asyncio
import json
import math
import os
import sys
from dataclasses import dataclass
from importlib.metadata import version
from pathlib import Path

from .evidence_contracts import STATIC_EXTRACTION_VERSION
from .pdf_child import (
    PDF_MAX_BYTES,
    PDF_MAX_CHARS,
    PDF_MAX_OUTPUT,
    PDF_MAX_PAGE_CHARS,
    PDF_MAX_PAGES,
    PDF_SIGNATURE,
    PDF_WALL_SECONDS,
)
from .safe_fetch import FetchError

PDF_EXTRACTION_VERSION = f"{version('pypdf')}+normalizer.{STATIC_EXTRACTION_VERSION}"
_CHILD = Path(__file__).with_name("pdf_child.py")


@dataclass(frozen=True)
class ParsedPDF:
    pages: tuple[str, ...]
    truncated: bool
    has_visual_content: bool


class OCRPending(FetchError):
    """已检查 PDF 绘制页面，需要部署方的 OCR 能力。"""

    def __init__(self) -> None:
        super().__init__(422, "OCR_PENDING", "已检查页面未提取到文本层，待 OCR")


def _resource_limit() -> FetchError:
    return FetchError(422, "FETCH_PDF_LIMIT", "PDF 超过解析资源上限")


def _invalid() -> FetchError:
    return FetchError(422, "FETCH_PDF_INVALID", "无法提取该 PDF 的文本层")


def _timeout() -> FetchError:
    return FetchError(504, "TIME_BUDGET_EXCEEDED", "PDF 解析时限已耗尽", retryable=True)


async def _write_input(writer: asyncio.StreamWriter, body: bytes) -> None:
    try:
        writer.write(body)
        await writer.drain()
    except (BrokenPipeError, ConnectionResetError):
        pass
    finally:
        writer.close()


async def _collect(process: asyncio.subprocess.Process, body: bytes) -> bytes:
    assert process.stdin is not None and process.stdout is not None
    writer = asyncio.create_task(_write_input(process.stdin, body))
    try:
        output = bytearray()
        while chunk := await process.stdout.read(16_384):
            if len(output) + len(chunk) > PDF_MAX_OUTPUT:
                raise _resource_limit()
            output.extend(chunk)
        await process.wait()
        return bytes(output)
    finally:
        writer.cancel()
        await asyncio.gather(writer, return_exceptions=True)


async def _stop(process: asyncio.subprocess.Process) -> None:
    if process.returncode is None:
        try:
            process.kill()
        except ProcessLookupError:
            pass
    # 子进程停止后清理已缓冲的管道数据。
    await process.communicate()


def _decode(raw: bytes) -> ParsedPDF:
    try:
        payload = json.loads(raw)
    except (ValueError, RecursionError):
        raise _invalid() from None
    if not isinstance(payload, dict):
        raise _invalid()
    match payload.get("status"):
        case "encrypted":
            raise FetchError(422, "FETCH_PDF_ENCRYPTED", "加密 PDF 需要单独授权的提取流程")
        case "resource_limit":
            raise _resource_limit()
        case "ok":
            pages = payload.get("pages")
            truncated = payload.get("truncated")
            has_visual_content = payload.get("has_visual_content")
        case _:
            raise _invalid()
    if (
        not isinstance(pages, list)
        or len(pages) > PDF_MAX_PAGES
        or not all(isinstance(page, str) and len(page) <= PDF_MAX_PAGE_CHARS for page in pages)
        or sum(map(len, pages)) > PDF_MAX_CHARS
        or not isinstance(truncated, bool)
        or not isinstance(has_visual_content, bool)
    ):
        raise _invalid()
    return ParsedPDF(tuple(pages), truncated, has_visual_content)


class PdfParser:
    def __init__(self) -> None:
        self._slots = asyncio.Semaphore(2)

    async def parse(self, body: bytes, *, timeout_s: float) -> ParsedPDF:
        if not math.isfinite(timeout_s) or timeout_s <= 0:
            raise _timeout()
        if len(body) > PDF_MAX_BYTES:
            raise FetchError(413, "FETCH_BODY_TOO_LARGE", "PDF 响应超过抓取上限")
        if not body.startswith(PDF_SIGNATURE):
            raise _invalid()
        # 仅保留 Windows 创建 Python 进程所需的系统目录变量。
        env = {key: os.environ[key] for key in ("SYSTEMROOT",) if key in os.environ}
        try:
            async with asyncio.timeout(min(timeout_s, PDF_WALL_SECONDS)), self._slots:
                process = await asyncio.create_subprocess_exec(
                    sys.executable,
                    "-I",
                    "-B",
                    str(_CHILD),
                    stdin=asyncio.subprocess.PIPE,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.DEVNULL,
                    env=env,
                )
                try:
                    output = await _collect(process, body)
                finally:
                    await _stop(process)
                if process.returncode != 0:
                    raise _resource_limit()
        except TimeoutError:
            raise _timeout() from None
        except OSError:
            raise FetchError(
                503, "FETCH_PDF_UNAVAILABLE", "PDF 解析进程暂不可用", retryable=True
            ) from None
        return _decode(output)
