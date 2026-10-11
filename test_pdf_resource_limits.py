"""
Regression tests for the PDF resource-exhaustion fix in backend/rag_engine.py
(bounded stream decoding + in-page deadline).

Run with: GEMINI_API_KEY=dummy python3 -m pytest test_pdf_resource_limits.py -v

BACKGROUND (measured, pypdf 6.19.0): the expensive part of a "decompression
bomb" content stream is NOT inflating it (~0.2 s for 60 MB) but pypdf's
pure-Python content-stream parser walking the decoded bytes (~18 s for 60 MB
of whitespace here, ~59 s on the slower Windows machine that reported the
failure; ~1.3-2.2 s per decoded MB for dense operators). A page can also
invoke a form XObject (`Do`) many times and pypdf re-parses the form every
time, so a size cap alone cannot bound the time. These tests pin both
protections:

  * decoded streams are capped BEFORE allocation (any filter, array contents)
  * a deadline / per-page text cap is enforced WHILE a page is processed,
    including inside nested forms

Timing-sensitive assertions use generous margins relative to the unbounded
behaviour (>= 5x), and the deterministic variants use an injected clock so
they do not depend on machine speed at all.
"""
import os
import sys
import threading
import time
import tracemalloc
import zlib

sys.path.insert(0, os.path.abspath(os.path.dirname(__file__)))

import pypdf
import pytest

from backend import rag_engine
from backend.rag_engine import PDFExtractionError, extract_text_from_pdf
from test_p1_hardening import build_pdf, make_pdf

# %d = content stream object number, %d = font object number, %s = extra resources
FONT_PAGE = (b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Contents %d 0 R "
             b"/Resources << /Font << /F1 %d 0 R >> %s >> >>")
FONT_OBJ = b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>"


def _flate(body: bytes) -> bytes:
    comp = zlib.compress(body, 9)
    return b"<< /Length %d /Filter /FlateDecode >>\nstream\n" % len(comp) + comp + b"\nendstream"


def _single_page_pdf(content_stream_obj: bytes) -> bytes:
    return build_pdf([
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        FONT_PAGE % (4, 5, b""),
        content_stream_obj,
        FONT_OBJ,
    ])


HEAD = b"BT /F1 12 Tf 72 720 Td "


def _bomb_pdf(decoded_bytes: int) -> bytes:
    return _single_page_pdf(_flate(HEAD + b"(x) Tj ET " + b" " * decoded_bytes))


# ---------------------------------------------------------------------------
# the reported regression
# ---------------------------------------------------------------------------

class TestDecodedStreamCap:
    def test_the_reported_60mb_bomb_is_refused_fast_with_the_safe_message(self):
        data = _bomb_pdf(60_000_000)
        assert len(data) < 200_000
        t0 = time.monotonic()
        with pytest.raises(PDFExtractionError) as e:
            extract_text_from_pdf(data)
        elapsed = time.monotonic() - t0
        assert elapsed < 2.0, f"took {elapsed:.2f}s - the stream must be refused before it is parsed"
        assert str(e.value) == rag_engine._SAFE_TOO_COMPLEX_MESSAGE

    def test_memory_is_bounded_the_60mb_stream_is_never_allocated(self):
        data = _bomb_pdf(60_000_000)
        tracemalloc.start()
        try:
            with pytest.raises(PDFExtractionError):
                extract_text_from_pdf(data)
            _, peak = tracemalloc.get_traced_memory()
        finally:
            tracemalloc.stop()
        # decoding the full stream would need >= 60 MB; the cap is 3 MB
        assert peak < 20_000_000, f"peak traced allocation {peak / 1e6:.1f} MB"

    def test_error_text_never_leaks_parser_internals(self):
        with pytest.raises(PDFExtractionError) as e:
            extract_text_from_pdf(_bomb_pdf(60_000_000))
        msg = str(e.value).lower()
        for leak in ("pypdf", "limit reached", "decompress", "bytes remaining", "traceback"):
            assert leak not in msg

    def test_stream_just_under_the_cap_still_extracts(self):
        cap = rag_engine.MAX_STREAM_DECODED_BYTES
        data = _bomb_pdf(cap - 200)           # whitespace padding: cheap to parse, still a huge legit-size stream
        text = extract_text_from_pdf(data)
        assert text.strip() == "x"

    def test_stream_just_over_the_cap_is_refused(self):
        cap = rag_engine.MAX_STREAM_DECODED_BYTES
        with pytest.raises(PDFExtractionError) as e:
            extract_text_from_pdf(_bomb_pdf(cap + 10))
        assert str(e.value) == rag_engine._SAFE_TOO_COMPLEX_MESSAGE

    def test_cap_is_well_above_what_an_ordinary_text_page_needs(self):
        # An ordinary dense text page: ~3000 characters in ~100 text-show operators (~6-10 KB decoded).
        page = HEAD + b"".join(b"(Some ordinary study text about force and motion on this line.) Tj 0 -14 Td " for _ in range(100)) + b"ET"
        assert len(page) * 100 < rag_engine.MAX_STREAM_DECODED_BYTES        # cap is >100x such a page
        text = extract_text_from_pdf(_single_page_pdf(_flate(page)))
        assert text.count("Some ordinary study text") == 100

    def test_pypdf_configuration_is_scoped_and_restored(self):
        default = pypdf.Configuration().zlib_maximum_output_length   # pypdf's pristine default, not 'whatever is current'
        with pytest.raises(PDFExtractionError):
            extract_text_from_pdf(_bomb_pdf(60_000_000))
        assert pypdf.get_configuration().zlib_maximum_output_length == default
        extract_text_from_pdf(make_pdf(["hello world"]))
        assert pypdf.get_configuration().zlib_maximum_output_length == default

    def test_configuration_is_restored_even_when_extraction_fails_unexpectedly(self):
        default = pypdf.Configuration().zlib_maximum_output_length   # pypdf's pristine default, not 'whatever is current'
        with pytest.raises(PDFExtractionError):
            extract_text_from_pdf(b"%PDF-1.4\n garbage garbage")
        assert pypdf.get_configuration().zlib_maximum_output_length == default

    def test_other_filters_are_capped_too_runlength(self):
        # RunLengthDecode: [257-n, byte] repeats `byte` n times (n<=128): 2 bytes -> 128 bytes.
        body = bytes([257 - 128, 0x20]) * 40_000 + bytes([128])          # ~5.1 MB decoded, ~80 KB encoded
        stream = (b"<< /Length %d /Filter /RunLengthDecode >>\nstream\n" % len(body)) + body + b"\nendstream"
        data = _single_page_pdf(stream)
        assert len(data) < 200_000
        t0 = time.monotonic()
        with pytest.raises(PDFExtractionError):
            extract_text_from_pdf(data)
        assert time.monotonic() - t0 < 5

    def test_content_stream_arrays_are_capped_in_total(self):
        piece = _flate(b" " * 1_200_000)                                  # 1.2 MB each, under the per-stream cap
        pieces = [piece, piece, piece, piece]                              # 4.8 MB total, over the 3 MB cap
        objs = [b"<< /Type /Catalog /Pages 2 0 R >>", b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
                b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Contents [4 0 R 5 0 R 6 0 R 7 0 R] "
                b"/Resources << /Font << /F1 8 0 R >> >> >>"] + pieces + [FONT_OBJ]
        data = build_pdf(objs)
        t0 = time.monotonic()
        with pytest.raises(PDFExtractionError):
            extract_text_from_pdf(data)
        assert time.monotonic() - t0 < 5


# ---------------------------------------------------------------------------
# partial results must never look complete
# ---------------------------------------------------------------------------

class TestPartialExtractionIsSafe:
    def _two_page_pdf(self, second_page_stream: bytes) -> bytes:
        return build_pdf([
            b"<< /Type /Catalog /Pages 2 0 R >>",
            b"<< /Type /Pages /Kids [3 0 R 4 0 R] /Count 2 >>",
            FONT_PAGE % (5, 7, b""),
            FONT_PAGE % (6, 7, b""),
            _flate(HEAD + b"(FIRST PAGE TEXT) Tj ET"),
            second_page_stream,
            FONT_OBJ,
        ])

    def test_oversized_second_page_stops_extraction_keeping_only_complete_earlier_pages(self):
        # objects: 5 = page-1 content, 6 = page-2 content (second_page_stream)
        data = self._two_page_pdf(_flate(HEAD + b"(SECOND PAGE TEXT) Tj ET " + b" " * 5_000_000))
        text = extract_text_from_pdf(data)
        assert "FIRST PAGE TEXT" in text
        assert "SECOND PAGE TEXT" not in text, "a refused page's text must not be returned as part of the document"

    def test_the_stop_is_logged_with_counts_only_never_content(self, caplog):
        data = self._two_page_pdf(_flate(HEAD + b"(SECRET STUDENT NOTES) Tj ET " + b" " * 5_000_000))
        with caplog.at_level("WARNING", logger="kognit.rag_engine"):
            extract_text_from_pdf(data)
        joined = " ".join(r.getMessage() for r in caplog.records)
        assert "extraction stopped early" in joined and "size limit" in joined
        assert "SECRET STUDENT NOTES" not in joined and "FIRST PAGE TEXT" not in joined

    def test_when_nothing_complete_was_read_the_error_is_raised_not_an_empty_success(self):
        with pytest.raises(PDFExtractionError):
            extract_text_from_pdf(_bomb_pdf(10_000_000))


# ---------------------------------------------------------------------------
# the form-XObject amplification a size cap alone cannot stop
# ---------------------------------------------------------------------------

def _amplification_pdf(form_text_ops: int, invocations: int) -> bytes:
    form_body = HEAD + b"(x) Tj " * form_text_ops + b"ET"
    form = zlib.compress(form_body, 9)
    page_stream = _flate(b"".join(b"/X1 Do " for _ in range(invocations)))
    return build_pdf([
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        FONT_PAGE % (4, 5, b"/XObject << /X1 6 0 R >>"),
        page_stream,
        FONT_OBJ,
        b"<< /Type /XObject /Subtype /Form /BBox [0 0 612 792] /Resources << /Font << /F1 5 0 R >> >> "
        b"/Length %d /Filter /FlateDecode >>\nstream\n" % len(form) + form + b"\nendstream",
    ])


class _FakeClock:
    """Replaces the `time` module INSIDE rag_engine only. Every reading
    advances by `step` seconds, so deadlines trip after a fixed number of
    operators regardless of how fast the machine is."""

    def __init__(self, step):
        self._now = 1000.0
        self._step = step

    def monotonic(self):
        self._now += self._step
        return self._now


class TestInPageDeadline:
    def test_repeated_form_invocations_are_cut_off_deterministically(self, monkeypatch):
        # 5000 invocations (pypdf's own default cap) of a 14 KB form: ~125 s of parsing unbounded.
        data = _amplification_pdf(form_text_ops=2000, invocations=5000)
        monkeypatch.setattr(rag_engine, "MIN_IN_PAGE_SECONDS", 0.0)
        monkeypatch.setattr(rag_engine, "time", _FakeClock(step=0.001))
        t0 = time.monotonic()                                    # REAL clock (rag_engine's is faked)
        with pytest.raises(PDFExtractionError) as e:
            extract_text_from_pdf(data, time_budget_seconds=0.05)
        assert "took too long" in str(e.value)
        assert time.monotonic() - t0 < 10, "the page must be abandoned after ~50 operators, not after 5000 form parses"

    def test_real_clock_a_runaway_page_is_stopped_near_the_budget(self, monkeypatch):
        # Unbounded: 60 invocations x ~0.7 s = ~40 s. With a 1 s budget it must stop within a few seconds.
        data = _amplification_pdf(form_text_ops=50_000, invocations=60)
        monkeypatch.setattr(rag_engine, "MIN_IN_PAGE_SECONDS", 0.0)
        t0 = time.monotonic()
        with pytest.raises(PDFExtractionError) as e:
            extract_text_from_pdf(data, time_budget_seconds=1.0)
        elapsed = time.monotonic() - t0
        assert "took too long" in str(e.value)
        assert elapsed < 10, f"runaway page ran {elapsed:.1f}s against a 1 s budget"

    def test_per_page_text_cap_stops_text_multiplication(self, monkeypatch):
        # each form call emits ~2000 chars; 600 calls = 1.2 M chars > the (floored) per-page cap
        data = _amplification_pdf(form_text_ops=2000, invocations=600)
        monkeypatch.setattr(rag_engine, "MIN_PAGE_TEXT_CAP_CHARS", 50_000)
        with pytest.raises(PDFExtractionError) as e:
            extract_text_from_pdf(data, max_chars=50_000)
        assert str(e.value) == rag_engine._SAFE_TOO_COMPLEX_MESSAGE

    def test_a_used_up_budget_still_returns_the_first_ordinary_page(self):
        # the long-standing contract (also pinned in test_p1_hardening): the first page always completes
        text = extract_text_from_pdf(make_pdf(["PAGE001 body", "PAGE002 body"]), time_budget_seconds=0)
        assert "PAGE001" in text and "PAGE002" not in text

    def test_in_page_deadline_is_never_shorter_than_the_floor(self):
        # with the default floor an ordinary page is unaffected by any tiny budget
        text = extract_text_from_pdf(make_pdf(["PAGE001 body"]), time_budget_seconds=1e-9)
        assert "PAGE001" in text


# ---------------------------------------------------------------------------
# normal documents must be unaffected
# ---------------------------------------------------------------------------

class TestNormalDocumentsUnchanged:
    def test_output_is_identical_to_plain_pypdf_extraction(self):
        pages = [f"Chapter {i}: Newton's laws describe force, mass and acceleration. F = ma." for i in range(1, 31)]
        data = make_pdf(pages)
        import io
        reader = pypdf.PdfReader(io.BytesIO(data), strict=False)
        expected = "".join((p.extract_text() or "") + "\n" for p in reader.pages)
        assert extract_text_from_pdf(data) == expected

    def test_form_xobject_text_is_still_extracted(self):
        text = extract_text_from_pdf(_amplification_pdf(form_text_ops=3, invocations=2))
        assert text.count("x") >= 6

    def test_all_existing_signatures_and_defaults_are_unchanged(self):
        import inspect
        sig = inspect.signature(extract_text_from_pdf)
        assert list(sig.parameters) == ["file_bytes", "max_chars", "max_pages", "time_budget_seconds"]
        assert rag_engine.DEFAULT_MAX_EXTRACTED_CHARS == 600_000
        assert rag_engine.MAX_PDF_PAGES == 1000
        assert rag_engine.PDF_EXTRACTION_TIME_BUDGET_SECONDS == 30.0


# ---------------------------------------------------------------------------
# concurrency: scoped configuration must not leak between threads
# ---------------------------------------------------------------------------

class TestThreadIsolation:
    def test_concurrent_extractions_do_not_interfere_and_leave_the_default_config_alone(self):
        default = pypdf.Configuration().zlib_maximum_output_length   # pypdf's pristine default, not 'whatever is current'
        bomb = _bomb_pdf(60_000_000)
        valid = make_pdf(["Valid study text about motion."])
        results, errors = [], []

        def worker(kind):
            try:
                if kind == "bomb":
                    try:
                        extract_text_from_pdf(bomb)
                        results.append(("bomb", "NO-ERROR"))
                    except PDFExtractionError:
                        results.append(("bomb", "refused"))
                else:
                    results.append(("valid", "Valid study text" in extract_text_from_pdf(valid)))
            except Exception as exc:                       # pragma: no cover - would fail the assertions below
                errors.append(repr(exc))

        threads = [threading.Thread(target=worker, args=(k,)) for k in ("bomb", "valid") * 4]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=60)
        assert not errors, errors
        assert sorted(results) == sorted([("bomb", "refused")] * 4 + [("valid", True)] * 4)
        assert pypdf.get_configuration().zlib_maximum_output_length == default