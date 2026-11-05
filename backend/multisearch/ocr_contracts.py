"""部署方批准的本地 OCR 扩展契约。"""

from hashlib import sha256
from typing import Annotated, Literal, Protocol, Self

from pydantic import AwareDatetime, ConfigDict, Field, StringConstraints, model_validator

from .contracts import InputModel

_MAX_CONTENT_BYTES = 16 * 1024 * 1024
_MAX_OUTPUT_BYTES = 512 * 1024
OpaqueDocumentID = Annotated[
    str, StringConstraints(min_length=1, max_length=128, pattern=r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
]
ContentSHA256 = Annotated[str, StringConstraints(pattern=r"^[a-f0-9]{64}$")]
OCRLanguage = Annotated[str, StringConstraints(pattern=r"^[A-Za-z0-9_-]{2,16}$")]
OCRPageNumber = Annotated[int, Field(ge=1, le=100_000)]


class OCRInput(InputModel):
    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)

    document_id: OpaqueDocumentID
    content_sha256: ContentSHA256
    content_type: Literal["application/pdf", "image/png", "image/jpeg", "image/tiff", "image/webp"]
    content: Annotated[bytes, Field(strict=True, min_length=1, max_length=_MAX_CONTENT_BYTES)] = (
        Field(exclude=True, repr=False)
    )
    page_numbers: list[OCRPageNumber] = Field(min_length=1, max_length=30)
    languages: list[OCRLanguage] = Field(default_factory=list, max_length=8)

    @model_validator(mode="after")
    def verify_input(self) -> Self:
        if sha256(self.content).hexdigest() != self.content_sha256:
            raise ValueError("OCR 输入与内容哈希不一致")
        if self.page_numbers != sorted(set(self.page_numbers)):
            raise ValueError("OCR 页码必须按升序排列且不可重复")
        if self.content_type != "application/pdf" and self.page_numbers != [1]:
            raise ValueError("图片 OCR 输入只能指定第 1 页")
        return self


class OCRBBox(InputModel):
    """以页面左上角为原点的归一化坐标。"""

    left: float = Field(ge=0, le=1)
    top: float = Field(ge=0, le=1)
    right: float = Field(ge=0, le=1)
    bottom: float = Field(ge=0, le=1)

    @model_validator(mode="after")
    def nonempty(self) -> Self:
        if self.left >= self.right or self.top >= self.bottom:
            raise ValueError("OCR 边界框必须具有正面积")
        return self


class OCRBlock(InputModel):
    text: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=4096)]
    bbox: OCRBBox | None = None
    confidence: float | None = Field(default=None, ge=0, le=1)


class OCRPage(InputModel):
    page_number: OCRPageNumber
    width_px: int = Field(ge=1, le=100_000)
    height_px: int = Field(ge=1, le=100_000)
    blocks: list[OCRBlock] = Field(max_length=256)


class OCRResult(InputModel):
    document_id: OpaqueDocumentID
    content_sha256: ContentSHA256
    engine: Annotated[str, StringConstraints(min_length=1, max_length=80)]
    engine_version: Annotated[str, StringConstraints(min_length=1, max_length=80)]
    extracted_at: AwareDatetime
    status: Literal["completed", "partial"]
    pages: list[OCRPage] = Field(max_length=30)
    warnings: list[Annotated[str, StringConstraints(max_length=160)]] = Field(
        default_factory=list, max_length=30
    )

    @model_validator(mode="after")
    def bound_output(self) -> Self:
        numbers = [page.page_number for page in self.pages]
        if numbers != sorted(set(numbers)):
            raise ValueError("OCR 输出页码必须按升序排列且不可重复")
        if self.status == "completed" and not self.pages:
            raise ValueError("已完成的 OCR 输出必须包含页面")
        if (
            sum(len(block.text.encode("utf-8")) for page in self.pages for block in page.blocks)
            > _MAX_OUTPUT_BYTES
        ):
            raise ValueError("OCR 输出超过文本大小上限")
        return self


class OCRAdapter(Protocol):
    """由部署方注册的适配器只接收已经获准的本地字节。"""

    adapter_id: str

    async def extract(self, request: OCRInput, *, timeout_s: float) -> OCRResult: ...
