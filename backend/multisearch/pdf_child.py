"""仅处理通过标准输入提供的 PDF 文本层。"""

import codecs
import io
import json
import sys
from unicodedata import category, normalize

PDF_SIGNATURE = b"%PDF-"
PDF_MAX_BYTES = 1024 * 1024
PDF_MAX_PAGES = 20
PDF_MAX_PAGE_CHARS = 16_000
PDF_MAX_CHARS = 64_000
PDF_MAX_OUTPUT = 320 * 1024
PDF_WALL_SECONDS = 3.0
_CPU_SECONDS = 2
_ADDRESS_BYTES = 160 * 1024 * 1024
_STREAM_BYTES = 2 * 1024 * 1024
_PAINT_OPERATORS = frozenset(
    {b"Do", b"INLINE IMAGE", b"S", b"s", b"f", b"F", b"f*", b"B", b"B*", b"b", b"b*", b"sh"}
)


def _limits() -> None:
    if sys.platform == "linux":
        import resource

        resource.setrlimit(resource.RLIMIT_CPU, (_CPU_SECONDS, _CPU_SECONDS))
        resource.setrlimit(resource.RLIMIT_AS, (_ADDRESS_BYTES, _ADDRESS_BYTES))
        resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
        resource.setrlimit(resource.RLIMIT_FSIZE, (0, 0))


def _deny_external_operations(event: str, _arguments: tuple) -> None:
    # 依赖完成导入后才安装。解析阶段只需在内存中读写 PDF。
    if (
        event == "open"
        or event.startswith(("socket.", "subprocess.", "ctypes."))
        or event
        in {
            "os.exec",
            "os.fork",
            "os.forkpty",
            "os.posix_spawn",
            "os.system",
            "os.startfile",
            "os.chdir",
            "os.mkdir",
            "os.rmdir",
            "os.rename",
            "os.remove",
            "os.link",
            "os.symlink",
            "os.truncate",
            "os.listdir",
            "os.scandir",
        }
    ):
        raise PermissionError("PDF 文本解析只允许内存操作")


def _clean_page(text: str) -> str:
    text = normalize("NFC", text)
    return " ".join(
        "".join(
            char for char in text if char.isspace() or not category(char).startswith("C")
        ).split()
    )


def _inspect_page(page) -> tuple[str, bool]:
    visited = painted = False

    def inspect_operator(operator: bytes, *_args) -> None:
        nonlocal visited, painted
        visited = True
        painted |= operator in _PAINT_OPERATORS

    text = _clean_page(
        page.extract_text(extraction_mode="plain", visitor_operand_before=inspect_operator)
    )
    # 无资源字典的页面可能只有内联图像，文本提取器会跳过其内容流。
    if not visited and not text and "/Contents" in page:
        content = page.get_contents()
        if content is not None:
            painted = any(operator in _PAINT_OPERATORS for _, operator in content.operations)
    return text, painted


def _extract() -> dict:
    from pypdf import PdfReader, overwrite_configuration
    from pypdf.errors import LimitReachedError

    # 提前加载 PDF 字体映射常用的解码器，避免解析时打开模块文件。
    for name in (
        "utf-8",
        "utf-16",
        "utf-16-be",
        "utf-16-le",
        "latin-1",
        "cp1252",
        "mac-roman",
        "gb2312",
        "gbk",
        "gb18030",
        "big5",
        "cp950",
        "shift-jis",
        "cp932",
        "euc-jp",
        "euc-kr",
        "cp949",
    ):
        codecs.lookup(name)
    overwrite_configuration(
        disable_legacy_handling=True,
        jbig2dec_binary=None,
        maximum_declared_stream_length=PDF_MAX_BYTES,
        array_based_stream_maximum_output_length=_STREAM_BYTES,
        zlib_maximum_output_length=_STREAM_BYTES,
        lzw_maximum_output_length=_STREAM_BYTES,
        run_length_maximum_output_length=_STREAM_BYTES,
        brotli_maximum_output_length=_STREAM_BYTES,
        stream_decoding_work_maximum_length=4 * _STREAM_BYTES,
        stream_parsing_work_maximum_count=200_000,
        page_tree_maximum_entries=1024,
        page_tree_maximum_depth=32,
        page_tree_maximum_page_entries=32_768,
    )
    sys.addaudithook(_deny_external_operations)
    raw = sys.stdin.buffer.read(PDF_MAX_BYTES + 1)
    if len(raw) > PDF_MAX_BYTES or not raw.startswith(PDF_SIGNATURE):
        return {"status": "invalid"}

    try:
        reader = PdfReader(io.BytesIO(raw), strict=True)
        if reader.is_encrypted:
            return {"status": "encrypted"}
        page_count = len(reader.pages)
        truncated = page_count > PDF_MAX_PAGES
        remaining = PDF_MAX_CHARS
        pages = []
        has_visual_content = False
        for index in range(min(PDF_MAX_PAGES, page_count)):
            page_text, painted = _inspect_page(reader.pages[index])
            has_visual_content |= painted
            take = min(PDF_MAX_PAGE_CHARS, remaining)
            truncated |= len(page_text) > take
            pages.append(page_text[:take])
            remaining -= len(pages[-1])
            if remaining == 0:
                truncated |= index + 1 < page_count
                break
        return {
            "status": "ok",
            "pages": pages,
            "truncated": truncated,
            "has_visual_content": has_visual_content,
        }
    except (LimitReachedError, MemoryError):
        return {"status": "resource_limit"}
    except Exception:
        return {"status": "invalid"}


if __name__ == "__main__":
    _limits()
    payload = json.dumps(_extract(), ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    sys.stdout.buffer.write(
        payload if len(payload) <= PDF_MAX_OUTPUT else b'{"status":"resource_limit"}'
    )
