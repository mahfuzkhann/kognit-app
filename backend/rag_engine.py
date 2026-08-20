import io
import logging

import PyPDF2

logger = logging.getLogger("kognit.rag_engine")


class PDFExtractionError(Exception):
    """
    Raised when a PDF cannot be read or contains no extractable text.
    Messages on this exception are shown directly to the end user, so they
    must never contain raw parser/library exception text - only the safe,
    hand-written strings below.
    """
    pass


def extract_text_from_pdf(file_bytes: bytes) -> str:
    try:
        pdf_file = io.BytesIO(file_bytes)
        reader = PyPDF2.PdfReader(pdf_file)

        if reader.is_encrypted:
            raise PDFExtractionError("This PDF is password-protected and cannot be read.")

        text = ""
        for page in reader.pages:
            text += (page.extract_text() or "") + "\n"

        if not text.strip():
            raise PDFExtractionError("No readable text was found in this PDF (it may be a scanned image).")

        return text
    except PDFExtractionError:
        raise
    except Exception:
        # Covers malformed/corrupted files that PyPDF2 itself chokes on.
        # Log the real parser exception server-side only - never put str(e)
        # into the PDFExtractionError message, since that message is shown
        # directly to the end user.
        logger.exception("PDF extraction failed (unexpected parser error)")
        raise PDFExtractionError(
            "Could not read this PDF. It may be corrupted, password-protected, "
            "or in an unsupported format."
        )