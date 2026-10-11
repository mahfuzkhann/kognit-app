import io
import logging
import time

from pypdf import PdfReader, apply_configuration
from pypdf.errors import LimitReachedError

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
# PDF_EXTRACTION_TIME_BUDGET_SECONDS: wall-clock budget. Checked between
# pages AND (see "P0 regression fix" below) inside a page while its content
# is being processed.
#
# ---------------------------------------------------------------------------
# P0 regression fix: decompression-bomb / parse-time bound.
#
# MEASURED ROOT CAUSE (pypdf 6.19.0, see the fix notes delivered with this
# change): inflating a 60 MB FlateDecode content stream takes ~0.2 s - that is
# NOT the cost. The cost is pypdf's PURE-PYTHON content-stream parser, which
# then walks the decoded bytes one at a time: ~18 s for 60 MB of whitespace on
# the reference Linux box (~59 s on the slower Windows machine that reported
# the failure) and ~1.3-2.2 s PER DECODED MEGABYTE for dense operator content
# (the realistic worst case). pypdf's own default output cap is 75 MB, far too
# generous for a time budget of ~30 s, and the old between-pages time check
# could only fire AFTER the single expensive page had finished.
#
# Two further facts shaped the fix:
#   * A page may invoke a form XObject (the `Do` operator) thousands of times
#     and pypdf re-parses the form's stream on EVERY invocation. Measured: a
#     1.9 KB PDF with a 0.7 MB form invoked 20 times took 23.5 s, linearly in
#     the number of invocations. A size cap alone therefore cannot bound the
#     time, so a deadline must also be enforced WHILE the page is processed.
#   * pypdf exposes both mechanisms we need as supported APIs:
#     `pypdf.apply_configuration(...)` (limits scoped to the current
#     thread/context via a ContextVar - nothing global is mutated, so
#     concurrent extractions and other pypdf users are unaffected) and the
#     `visitor_operand_before` / `visitor_text` callbacks of extract_text(),
#     which are invoked per operator / per piece of text, including inside
#     nested form XObjects.
#
# WHAT IS ENFORCED NOW
#   1. MAX_STREAM_DECODED_BYTES bounds the decoded size of any single stream
#      (and of array-based content streams) BEFORE it is allocated -
#      pypdf stops decompressing at the limit (LimitReachedError), so memory
#      and time are bounded up front. 3 MB is ~20-100x the decoded content
#      stream of an ordinary text page (tens of KB) while capping the measured
#      worst-case parse of ONE stream at ~7 s on the reference box.
#   2. A deadline checked at every operator and every text piece (including
#      inside forms): a page that keeps running past the budget is abandoned.
#   3. A per-page text cap (the document text budget, with a floor of
#      MIN_PAGE_TEXT_CAP_CHARS so an ordinary page is never affected), so a
#      page that multiplies text via repeated forms is abandoned too.
#
# HONEST LIMIT (not hidden): this is a COOPERATIVE deadline, not a hard kill.
# Python cannot interrupt the parse of one stream mid-way, so the worst-case
# overshoot is the time to parse ONE stream of at most MAX_STREAM_DECODED_BYTES
# (~7 s on the reference box; roughly 3x that on a 3x slower machine). A hard
# wall-clock guarantee would need extraction in a separate process that can be
# killed - the scale-phase option noted in P1-5; not justified yet because the
# measured overshoot is small and bounded, and a subprocess adds startup cost,
# Windows spawn/pickling cost and a new failure mode for a rarely-used upload.
# ---------------------------------------------------------------------------
DEFAULT_MAX_EXTRACTED_CHARS = 600_000
MAX_PDF_PAGES = 1000
PDF_EXTRACTION_TIME_BUDGET_SECONDS = 30.0

# Per-stream decoded-size cap (see the P0 note above for the measurements).
MAX_STREAM_DECODED_BYTES = 3_000_000

# The first page is ALWAYS given at least this long, even when the caller's
# time budget is tiny or zero (existing contract: a budget that is already
# used up still returns the first page's text, then stops). It only matters
# for the in-page deadline; ordinary pages take milliseconds.
MIN_IN_PAGE_SECONDS = 10.0

# Per-page text cap = max(document text budget, this floor). No real page
# approaches a million characters; the floor only keeps the guard from ever
# touching an ordinary page when a caller passes a small max_chars.
MIN_PAGE_TEXT_CAP_CHARS = 1_000_000

# The PDF spec lets the "%PDF-" header sit anywhere in the first 1024 bytes
# (some generators prepend junk), so check that window, not just byte 0.
_PDF_SIGNATURE = b"%PDF-"
_PDF_SIGNATURE_WINDOW = 1024


def has_pdf_signature(file_bytes: bytes) -> bool:
    """True if the content actually starts like a PDF (never trusts the
    browser-supplied MIME type or the filename extension)."""
    return _PDF_SIGNATURE in (file_bytes[:_PDF_SIGNATURE_WINDOW] if file_bytes else b"")


class _ExtractionLimit(Exception):
    """Internal control-flow signal raised from inside pypdf's per-operator /
    per-text callbacks to abandon the page being processed. Never escapes
    this module (extract_text_from_pdf converts it into a stop reason)."""

    def __init__(self, reason: str):
        self.reason = reason
        super().__init__(reason)


def _make_page_limit_visitors(hard_deadline: float, page_char_limit: int):
    """Callbacks for PageObject.extract_text that enforce the in-page deadline
    and the per-page text cap. Fresh state per page. pypdf calls them for
    every operator / every piece of text, including those inside nested form
    XObjects, which is what makes a repeated-form amplification stoppable."""
    produced = {"chars": 0}

    def before_operand(operator, operands, cm_matrix, tm_matrix):
        if time.monotonic() >= hard_deadline:
            raise _ExtractionLimit("time budget exceeded")

    def on_text(text, cm_matrix, tm_matrix, font_dict, font_size):
        produced["chars"] += len(text)
        if produced["chars"] > page_char_limit:
            raise _ExtractionLimit("page text budget exceeded")

    return before_operand, on_text


_SAFE_TOO_COMPLEX_MESSAGE = "This PDF has a page that is too large or complex to process safely."


def _extract_with_limits(file_bytes: bytes, max_chars: int, max_pages: int, time_budget_seconds: float) -> str:
    pdf_file = io.BytesIO(file_bytes)
    reader = PdfReader(pdf_file, strict=False)

    if reader.is_encrypted:
        raise PDFExtractionError("This PDF is password-protected and cannot be read.")

    total_pages = len(reader.pages)
    pages_to_read = min(total_pages, max_pages)
    started = time.monotonic()
    deadline = started + time_budget_seconds
    # In-page deadline: the same budget, but never shorter than
    # MIN_IN_PAGE_SECONDS, so an already-used-up budget still lets the first
    # (ordinary, millisecond-scale) page finish - the long-standing contract -
    # while a runaway page is still cut off.
    hard_deadline = started + max(time_budget_seconds, MIN_IN_PAGE_SECONDS)

    parts = []
    total_chars = 0
    stop_reason = None

    for page_index in range(pages_to_read):
        page = reader.pages[page_index]
        before_operand, on_text = _make_page_limit_visitors(hard_deadline, max(max_chars, MIN_PAGE_TEXT_CAP_CHARS))
        try:
            page_text = (page.extract_text(
                visitor_operand_before=before_operand, visitor_text=on_text
            ) or "") + "\n"
        except _ExtractionLimit as limit:
            # The page was abandoned mid-way; its partial text is DISCARDED
            # (never returned as if it were the whole page) and extraction
            # stops here, keeping only the complete pages before it - the same
            # "keep the beginning" semantics as the page / text / time caps.
            stop_reason = limit.reason
            break
        except LimitReachedError:
            # pypdf refused to decode a stream larger than
            # MAX_STREAM_DECODED_BYTES (stopped BEFORE allocating it). Same
            # handling: stop at this page, keep the complete pages before it.
            stop_reason = "page content exceeds the size limit"
            break
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
        if stop_reason in ("page content exceeds the size limit", "page text budget exceeded"):
            raise PDFExtractionError(_SAFE_TOO_COMPLEX_MESSAGE)
        raise PDFExtractionError("No readable text was found in this PDF (it may be a scanned image).")

    return text


def extract_text_from_pdf(
    file_bytes: bytes,
    max_chars: int = DEFAULT_MAX_EXTRACTED_CHARS,
    max_pages: int = MAX_PDF_PAGES,
    time_budget_seconds: float = PDF_EXTRACTION_TIME_BUDGET_SECONDS,
) -> str:
    try:
        if not has_pdf_signature(file_bytes):
            raise PDFExtractionError("This file does not look like a valid PDF.")

        # Limits are scoped to THIS call (ContextVar-based, thread-safe): the
        # decoded size of any stream is capped before it is allocated, for
        # every filter that can carry a content stream. Nothing global is
        # modified, and the previous configuration is restored on exit.
        with apply_configuration(
            zlib_maximum_output_length=MAX_STREAM_DECODED_BYTES,
            array_based_stream_maximum_output_length=MAX_STREAM_DECODED_BYTES,
            lzw_maximum_output_length=MAX_STREAM_DECODED_BYTES,
            run_length_maximum_output_length=MAX_STREAM_DECODED_BYTES,
        ):
            return _extract_with_limits(file_bytes, max_chars, max_pages, time_budget_seconds)
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