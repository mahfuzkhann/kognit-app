import io
import logging
import time

from pypdf import PdfReader

logger = logging.getLogger("kognit.rag_engine")


class PDFExtractionError(Exception):
    """
    Raised when a PDF cannot be read or contains no extractable text.
    Messages on this exception are shown directly to the end user, so they
    must never contain raw parser/library exception text - only the safe,
    hand-written strings below.
    """
    pass


# ---------------------------------------------------------------------------
# P1-5: bounded PDF extraction.
#
# PROBLEM: extraction used to be completely unbounded - the only protection
# was the 15MB upload cap in backend/main.py. A 15MB PDF can legitimately
# declare a very large number of pages, or decompress to far more text than
# Kognit can ever use, so a small upload could burn arbitrary CPU/memory.
# All three limits below are about the EXTRACTION work, not the upload size.
#
# DEFAULT_MAX_EXTRACTED_CHARS: backend/main.py passes its own value
# (derived from ai_engine.MAX_PDF_CONTEXT_CHARS, the amount of document text
# that is ever actually sent to the model). This default only applies to a
# caller that does not pass one. Text beyond the budget is never used by the
# model today, so stopping early changes no answer - it only avoids storing
# and producing text nobody reads. Extraction keeps the BEGINNING of the
# document, which is exactly what ai_engine keeps when it truncates.
#
# MAX_PDF_PAGES: hard cap on pages that are examined. Pages beyond it are
# ignored (the document is treated as truncated, same as hitting the text
# budget) rather than rejecting the file - a large textbook must keep
# working, it just contributes its first pages. This bounds the work for a
# PDF whose page tree declares a huge number of tiny/empty pages.
#
# PDF_EXTRACTION_TIME_BUDGET_SECONDS: wall-clock budget checked BETWEEN
# pages. Known limit (documented, not hidden): Python cannot interrupt a
# single page whose content stream makes the parser spin; the pypdf
# upgrade (P1-8) is what closes the known infinite-loop CVEs, and running
# extraction in a separate process with a hard kill would be the
# scale-phase fix (out of scope here).
# ---------------------------------------------------------------------------
DEFAULT_MAX_EXTRACTED_CHARS = 600_000
MAX_PDF_PAGES = 1000
PDF_EXTRACTION_TIME_BUDGET_SECONDS = 30.0

# The PDF spec lets the "%PDF-" header sit anywhere in the first 1024 bytes
# (some generators prepend junk), so check that window, not just byte 0.
_PDF_SIGNATURE = b"%PDF-"
_PDF_SIGNATURE_WINDOW = 1024


def has_pdf_signature(file_bytes: bytes) -> bool:
    """True if the content actually starts like a PDF (never trusts the
    browser-supplied MIME type or the filename extension)."""
    return _PDF_SIGNATURE in (file_bytes[:_PDF_SIGNATURE_WINDOW] if file_bytes else b"")


def extract_text_from_pdf(
    file_bytes: bytes,
    max_chars: int = DEFAULT_MAX_EXTRACTED_CHARS,
    max_pages: int = MAX_PDF_PAGES,
    time_budget_seconds: float = PDF_EXTRACTION_TIME_BUDGET_SECONDS,
) -> str:
    try:
        if not has_pdf_signature(file_bytes):
            raise PDFExtractionError("This file does not look like a valid PDF.")

        pdf_file = io.BytesIO(file_bytes)
        reader = PdfReader(pdf_file, strict=False)

        if reader.is_encrypted:
            raise PDFExtractionError("This PDF is password-protected and cannot be read.")

        total_pages = len(reader.pages)
        pages_to_read = min(total_pages, max_pages)
        deadline = time.monotonic() + time_budget_seconds

        parts = []
        total_chars = 0
        stop_reason = None

        for page_index in range(pages_to_read):
            page_text = (reader.pages[page_index].extract_text() or "") + "\n"
            parts.append(page_text)
            total_chars += len(page_text)

            if total_chars >= max_chars:
                stop_reason = "text budget reached"
                break
            # >= (not >): the budget counts as used up the moment the clock reaches the
            # deadline. With a strict > a zero/tiny budget never trips on platforms whose
            # monotonic clock is coarse (Windows before Python 3.13 ticks every ~15ms).
            if time.monotonic() >= deadline:
                stop_reason = "time budget exceeded"
                break

        if stop_reason is None and total_pages > pages_to_read:
            stop_reason = "page cap reached"

        text = "".join(parts)
        if len(text) > max_chars:
            text = text[:max_chars]

        if stop_reason:
            # Counts only - never document content (student data).
            logger.warning(
                "extract_text_from_pdf: extraction stopped early (%s) total_pages=%d "
                "pages_read=%d chars_kept=%d max_chars=%d max_pages=%d",
                stop_reason, total_pages, len(parts), len(text), max_chars, max_pages
            )

        if not text.strip():
            if stop_reason == "time budget exceeded":
                raise PDFExtractionError("This PDF took too long to process. Try a smaller or simpler file.")
            raise PDFExtractionError("No readable text was found in this PDF (it may be a scanned image).")

        return text
    except PDFExtractionError:
        raise
    except Exception:
        # Covers malformed/corrupted files that the parser itself chokes on.
        # Log the real parser exception server-side only - never put str(e)
        # into the PDFExtractionError message, since that message is shown
        # directly to the end user.
        logger.exception("PDF extraction failed (unexpected parser error)")
        raise PDFExtractionError(
            "Could not read this PDF. It may be corrupted, password-protected, "
            "or in an unsupported format."
        )