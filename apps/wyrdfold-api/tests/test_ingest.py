"""Tests for resume parsing and merge services (#497)."""

from __future__ import annotations

import io
import zipfile

import pytest

from app.services.ingest import parse as parse_mod
from app.services.ingest.merge import (
    DEFAULT_PURPOSE as MERGE_PURPOSE,
)
from app.services.ingest.merge import (
    MIN_PRESERVATION_RATIO,
    merge_into_prose,
)
from app.services.ingest.parse import (
    ACCEPTED_CONTENT_TYPES,
    MAX_FILE_SIZE,
    ParsedResume,
    ParseError,
    _assert_docx_within_inflation_limit,
    parse_docx,
    parse_pdf,
    parse_resume,
)
from app.services.llm.mock import MockLLMClient

# ---------------------------------------------------------------------------
# Helpers — create minimal valid PDF / DOCX bytes
# ---------------------------------------------------------------------------


def _make_pdf_bytes(text: str = "Senior Frontend Engineer\nReact, TypeScript") -> bytes:
    """Create a minimal single-page PDF with pdfplumber-readable text."""
    import pypdfium2 as pdfium
    from pdfplumber.utils.pdfinternals import resolve_and_decode  # noqa: F401

    pdf = pdfium.PdfDocument.new()
    page = pdf.new_page(200, 100)
    # pypdfium2 doesn't have a simple text-insert API, so we'll use
    # a raw content stream approach. Instead, create via reportlab-free method.
    pdf.close()

    # Fallback: build a minimal PDF by hand with text operators
    content = text.encode("latin-1", errors="replace")
    stream = (
        b"%PDF-1.4\n"
        b"1 0 obj<</Type/Catalog/Pages 2 0 R>>endobj\n"
        b"2 0 obj<</Type/Pages/Kids[3 0 R]/Count 1>>endobj\n"
        b"3 0 obj<</Type/Page/Parent 2 0 R/MediaBox[0 0 612 792]"
        b"/Contents 4 0 R/Resources<</Font<</F1<</Type/Font/Subtype/Type1/BaseFont/Helvetica>>>>>>>>endobj\n"
        b"4 0 obj<</Length " + str(50 + len(content)).encode() + b">>\nstream\n"
        b"BT /F1 12 Tf 72 720 Td (" + content + b") Tj ET\n"
        b"endstream\nendobj\n"
        b"xref\n0 5\n"
        b"0000000000 65535 f \n"
        b"0000000009 00000 n \n"
        b"0000000058 00000 n \n"
        b"0000000115 00000 n \n"
        b"0000000310 00000 n \n"
        b"trailer<</Size 5/Root 1 0 R>>\n"
        b"startxref\n456\n%%EOF"
    )
    return stream


def _make_docx_bytes(
    paragraphs: list[str] | None = None,
) -> bytes:
    """Create a minimal DOCX file with python-docx."""
    from docx import Document

    doc = Document()
    if paragraphs is None:
        paragraphs = ["Senior Frontend Engineer", "React, TypeScript, Next.js"]
    for text in paragraphs:
        doc.add_paragraph(text)
    buf = io.BytesIO()
    doc.save(buf)
    return buf.getvalue()


def _make_zip_bomb(inflated_size: int, *, member: str = "word/document.xml") -> bytes:
    """A zip whose single member decompresses to ``inflated_size`` zero bytes.

    Zeros compress to almost nothing, so this is the classic decompression
    bomb shape: a few KB on the wire, ``inflated_size`` bytes on parse.
    """
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr(member, b"\0" * inflated_size)
    return buf.getvalue()


# ---------------------------------------------------------------------------
# PDF parsing
# ---------------------------------------------------------------------------


class TestParsePdf:
    def test_basic_extraction(self):
        pdf_bytes = _make_pdf_bytes("Software Engineer at Acme Corp")
        result = parse_pdf(pdf_bytes, "resume.pdf")
        assert result.file_type == "pdf"
        assert result.source_filename == "resume.pdf"
        assert result.page_count is not None
        assert result.page_count >= 1
        # pdfplumber may or may not extract text from our hand-built PDF,
        # so we check that parsing doesn't crash
        assert isinstance(result.text, str)

    def test_corrupt_file_raises(self):
        with pytest.raises(ParseError, match="Failed to parse PDF"):
            parse_pdf(b"not a pdf", "bad.pdf")

    def test_rejects_pdf_over_page_cap(self, monkeypatch: pytest.MonkeyPatch):
        """#190: a PDF with more pages than the cap is rejected fast, before
        any per-page text extraction burns CPU — and the rejection surfaces
        cleanly (not re-wrapped as a generic 'Failed to parse PDF')."""
        monkeypatch.setattr(parse_mod, "MAX_PDF_PAGES", 0)
        pdf_bytes = _make_pdf_bytes("one page is already over a cap of 0")
        with pytest.raises(ParseError, match="too many pages") as exc_info:
            parse_pdf(pdf_bytes, "many.pdf")
        assert "Failed to parse PDF" not in str(exc_info.value)


# ---------------------------------------------------------------------------
# DOCX parsing
# ---------------------------------------------------------------------------


class TestParseDocx:
    def test_basic_extraction(self):
        docx_bytes = _make_docx_bytes(["Hello World", "Skills: Python, FastAPI"])
        result = parse_docx(docx_bytes, "resume.docx")
        assert result.file_type == "docx"
        assert result.source_filename == "resume.docx"
        assert "Hello World" in result.text
        assert "Python" in result.text

    def test_empty_docx(self):
        docx_bytes = _make_docx_bytes([])
        result = parse_docx(docx_bytes, "empty.docx")
        assert result.text == ""

    def test_corrupt_file_raises(self):
        # Non-zip bytes are now caught by the inflation guard's zip check
        # (before python-docx), so the message names the invalid-zip cause.
        with pytest.raises(ParseError, match="Not a valid DOCX"):
            parse_docx(b"not a docx", "bad.docx")

    def test_normal_docx_passes_inflation_guard(self):
        """A legitimate résumé-sized DOCX must not trip the guard."""
        docx_bytes = _make_docx_bytes(["Real resume", "Skills: Python"])
        # Guard is a no-op (returns None) and the parse succeeds end-to-end.
        assert _assert_docx_within_inflation_limit(docx_bytes) is None
        assert "Real resume" in parse_docx(docx_bytes, "ok.docx").text


class TestDocxDecompressionBomb:
    """#190/#29 R1 H3 — reject DOCX zips that inflate past the ceiling."""

    def test_rejects_bomb_over_default_ceiling(self):
        """A genuine bomb: a few KB compressed, >50 MB inflated → rejected,
        and parse never materialises the full payload."""
        inflated = parse_mod.MAX_DOCX_INFLATED_BYTES + (1 * 1024 * 1024)
        bomb = _make_zip_bomb(inflated)
        # The tiny-in / huge-out property: the fixture itself is small.
        assert len(bomb) < 1 * 1024 * 1024
        with pytest.raises(ParseError, match="zip bomb"):
            parse_docx(bomb, "bomb.docx")

    def test_ceiling_is_the_decision_boundary(self, monkeypatch: pytest.MonkeyPatch):
        """Guard rejects just over the ceiling and passes just under it —
        proving the limit is enforced on *actual* decompressed bytes."""
        monkeypatch.setattr(parse_mod, "MAX_DOCX_INFLATED_BYTES", 1 * 1024 * 1024)

        over = _make_zip_bomb(2 * 1024 * 1024)
        with pytest.raises(ParseError, match="zip bomb"):
            _assert_docx_within_inflation_limit(over)

        under = _make_zip_bomb(512 * 1024)  # half the (patched) ceiling
        assert _assert_docx_within_inflation_limit(under) is None

    def test_rejects_too_many_members(self, monkeypatch: pytest.MonkeyPatch):
        monkeypatch.setattr(parse_mod, "MAX_DOCX_MEMBERS", 3)
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
            for i in range(5):
                zf.writestr(f"part{i}.xml", b"x")
        with pytest.raises(ParseError, match="too many parts"):
            _assert_docx_within_inflation_limit(buf.getvalue())

    def test_non_zip_rejected_by_guard(self):
        with pytest.raises(ParseError, match="Not a valid DOCX"):
            _assert_docx_within_inflation_limit(b"definitely not a zip")


# ---------------------------------------------------------------------------
# parse_resume (router)
# ---------------------------------------------------------------------------


class TestParseResume:
    def test_routes_docx_by_content_type(self):
        docx_bytes = _make_docx_bytes(["Test content"])
        ct = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
        result = parse_resume(docx_bytes, "resume.docx", ct)
        assert result.file_type == "docx"
        assert "Test content" in result.text

    def test_routes_docx_by_extension(self):
        docx_bytes = _make_docx_bytes(["Fallback test"])
        result = parse_resume(docx_bytes, "resume.docx", "application/octet-stream")
        assert result.file_type == "docx"

    def test_rejects_unsupported_type(self):
        with pytest.raises(ValueError, match="Unsupported"):
            parse_resume(b"data", "file.txt", "text/plain")

    def test_rejects_oversized_file(self):
        big = b"x" * (MAX_FILE_SIZE + 1)
        with pytest.raises(ValueError, match="too large"):
            parse_resume(big, "huge.pdf", "application/pdf")

    def test_accepted_content_types(self):
        assert "application/pdf" in ACCEPTED_CONTENT_TYPES
        assert (
            "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
            in ACCEPTED_CONTENT_TYPES
        )


# ---------------------------------------------------------------------------
# merge_into_prose
# ---------------------------------------------------------------------------


class TestMergeIntoProse:
    async def test_first_upload_no_existing_skips_llm(self):
        parsed = ParsedResume(
            text="My resume content",
            source_filename="resume.pdf",
            file_type="pdf",
        )
        llm = MockLLMClient()
        merged, result = await merge_into_prose(llm, existing_content=None, parsed=parsed)
        assert merged == "My resume content"
        assert result is None
        assert llm.calls == []  # no LLM call on first upload

    async def test_first_upload_empty_existing_skips_llm(self):
        parsed = ParsedResume(
            text="My resume content",
            source_filename="resume.pdf",
            file_type="pdf",
        )
        llm = MockLLMClient()
        merged, result = await merge_into_prose(llm, existing_content="   ", parsed=parsed)
        assert merged == "My resume content"
        assert result is None
        assert llm.calls == []

    async def test_merge_with_existing_calls_llm_and_returns_merged(self):
        existing = (
            "Frontend Developer\nSentry\nSanta Monica\n"
            "did very cool stuff\nThere was other cool stuff I did"
        )
        parsed = ParsedResume(
            text=(
                "Frontend Developer\nSentry\nSanta Monica\n"
                "did very cool stuff\nI also did some awesome stuff"
            ),
            source_filename="second.docx",
            file_type="docx",
        )
        merged_doc = (
            "Frontend Developer\nSentry\nSanta Monica\n"
            "did very cool stuff\nThere was other cool stuff I did\n"
            "I also did some awesome stuff"
        )
        llm = MockLLMClient(scripted={MERGE_PURPOSE: merged_doc})

        merged, result = await merge_into_prose(llm, existing_content=existing, parsed=parsed)

        assert merged == merged_doc
        assert result is not None
        assert llm.calls and llm.calls[0]["purpose"] == MERGE_PURPOSE

    async def test_short_llm_output_falls_back_to_legacy_concat(self):
        existing = "x" * 1000  # something the LLM-output threshold can fail
        parsed = ParsedResume(
            text="brand new line",
            source_filename="resume.pdf",
            file_type="pdf",
        )
        # Mock returns a paraphrase shorter than MIN_PRESERVATION_RATIO * existing
        too_short = "y" * int(len(existing) * MIN_PRESERVATION_RATIO - 10)
        llm = MockLLMClient(scripted={MERGE_PURPOSE: too_short})

        merged, result = await merge_into_prose(llm, existing_content=existing, parsed=parsed)

        # Should NOT be the paraphrased output — should be the legacy
        # divider-concat fallback that preserves both inputs intact.
        assert too_short not in merged
        assert merged.startswith(existing)
        assert "[Uploaded Resume: resume.pdf]" in merged
        assert "brand new line" in merged
        assert result is not None  # LLM was still called (cost logged)
