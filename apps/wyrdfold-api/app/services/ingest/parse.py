"""Resume file parsing: PDF (pdfplumber) and DOCX (python-docx).

Pure functions — no DB, no side effects. Callers pass file bytes in,
get structured text out.
"""

from __future__ import annotations

import io
import logging
import zipfile
from typing import Literal

from pydantic import BaseModel

logger = logging.getLogger(__name__)

MAX_FILE_SIZE = 10 * 1024 * 1024  # 10 MB compressed upload body

# --- Decompression-bomb ceilings (#190/#29 R1 H3) --------------------------
# The 10 MB cap above only bounds the *compressed* upload. A crafted DOCX (zip)
# or PDF can inflate enormously on parse, exhausting memory / burning CPU on the
# worker thread (the request path is authed + rate-limited, but the thread that
# `asyncio.wait_for` abandons on timeout keeps running, so a fast reject matters
# more than the wall-clock timeout). These bound the *inflated* work.
MAX_DOCX_INFLATED_BYTES = 50 * 1024 * 1024  # 50 MB total decompressed
MAX_DOCX_MEMBERS = 2000  # a real .docx has ~10-40 parts; this is generous
_INFLATE_CHUNK = 1024 * 1024  # 1 MB - read members incrementally, never whole
MAX_PDF_PAGES = 500  # a resume is 1-5 pp; caps text-extraction CPU on a bomb

ACCEPTED_CONTENT_TYPES: dict[str, Literal["pdf", "docx"]] = {
    "application/pdf": "pdf",
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document": "docx",
}


class ParsedResume(BaseModel):
    text: str
    source_filename: str
    file_type: Literal["pdf", "docx"]
    page_count: int | None = None
    warnings: list[str] = []


class ParseError(Exception):
    """Raised when a file cannot be parsed."""


def parse_pdf(file_bytes: bytes, filename: str) -> ParsedResume:
    """Extract text from a PDF file using pdfplumber."""
    import pdfplumber

    warnings: list[str] = []
    pages_text: list[str] = []

    try:
        with pdfplumber.open(io.BytesIO(file_bytes)) as pdf:
            page_count = len(pdf.pages)
            # Cap pages before extracting: a bomb PDF with thousands of pages
            # would otherwise burn CPU page-by-page. Reject fast with a 422
            # (the router's parse timeout can't hard-kill the worker thread).
            if page_count > MAX_PDF_PAGES:
                raise ParseError(f"PDF has too many pages: {page_count} (max {MAX_PDF_PAGES})")
            for i, page in enumerate(pdf.pages):
                text = page.extract_text() or ""
                if not text.strip():
                    warnings.append(f"empty_page:{i + 1}")
                else:
                    pages_text.append(text)
    except ParseError:
        raise  # the page-cap rejection — surface it as-is, don't re-wrap
    except Exception as exc:
        raise ParseError(f"Failed to parse PDF: {exc}") from exc

    full_text = "\n\n".join(pages_text)
    return ParsedResume(
        text=full_text,
        source_filename=filename,
        file_type="pdf",
        page_count=page_count,
        warnings=warnings,
    )


def _assert_docx_within_inflation_limit(file_bytes: bytes) -> None:
    """Reject a DOCX that inflates past ``MAX_DOCX_INFLATED_BYTES`` on parse.

    A .docx is a zip; a tiny compressed file can decompress to gigabytes
    (a "zip bomb"). We decompress every member incrementally, counting the
    *actual* inflated bytes and aborting the moment the running total crosses
    the ceiling — so at most ``ceiling + _INFLATE_CHUNK`` bytes are ever
    materialised, regardless of how large the bomb claims to be. The
    central-directory size is never trusted (a crafted header can lie), so we
    always decompress rather than reading ``ZipInfo.file_size``.

    Runs before python-docx opens the same bytes; if this passes, the total
    the parser then reads is bounded by the ceiling too.
    """
    try:
        zf = zipfile.ZipFile(io.BytesIO(file_bytes))
    except zipfile.BadZipFile as exc:
        raise ParseError(f"Not a valid DOCX (zip) file: {exc}") from exc

    with zf:
        infos = zf.infolist()
        if len(infos) > MAX_DOCX_MEMBERS:
            raise ParseError(
                f"DOCX has too many parts: {len(infos)} (max {MAX_DOCX_MEMBERS}) "
                "— possible zip bomb"
            )
        total = 0
        for info in infos:
            try:
                with zf.open(info) as member:
                    while chunk := member.read(_INFLATE_CHUNK):
                        total += len(chunk)
                        if total > MAX_DOCX_INFLATED_BYTES:
                            raise ParseError(
                                "DOCX decompresses beyond the "
                                f"{MAX_DOCX_INFLATED_BYTES} byte limit "
                                "— possible zip bomb"
                            )
            except (zipfile.BadZipFile, OSError, EOFError) as exc:
                raise ParseError(f"Corrupt DOCX member {info.filename!r}: {exc}") from exc


def parse_docx(file_bytes: bytes, filename: str) -> ParsedResume:
    """Extract text from a DOCX file using python-docx."""
    from docx import Document

    warnings: list[str] = []

    _assert_docx_within_inflation_limit(file_bytes)

    try:
        doc = Document(io.BytesIO(file_bytes))
    except Exception as exc:
        raise ParseError(f"Failed to parse DOCX: {exc}") from exc

    paragraphs = [p.text for p in doc.paragraphs if p.text.strip()]
    full_text = "\n".join(paragraphs)

    return ParsedResume(
        text=full_text,
        source_filename=filename,
        file_type="docx",
        warnings=warnings,
    )


def parse_resume(
    file_bytes: bytes,
    filename: str,
    content_type: str,
) -> ParsedResume:
    """Route to the correct parser based on content type.

    Raises ParseError on parsing failure, ValueError on unsupported type
    or oversized file.
    """
    if len(file_bytes) > MAX_FILE_SIZE:
        raise ValueError(f"File too large: {len(file_bytes)} bytes (max {MAX_FILE_SIZE})")

    file_type = ACCEPTED_CONTENT_TYPES.get(content_type)

    # Fallback: check file extension if content type is generic
    if file_type is None:
        lower = filename.lower()
        if lower.endswith(".pdf"):
            file_type = "pdf"
        elif lower.endswith(".docx"):
            file_type = "docx"

    if file_type is None:
        raise ValueError(f"Unsupported file type: {content_type}")

    if file_type == "pdf":
        return parse_pdf(file_bytes, filename)
    return parse_docx(file_bytes, filename)
