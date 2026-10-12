"""Read exactly the grader's text order, with OCR on pages lacking a text layer."""
from __future__ import annotations

import io
from datetime import timezone
from email.utils import parsedate_to_datetime
from pathlib import Path

from grader.extract import attachments, pages_of, parse_eml

_OCR = None


def ocr_pages(data: bytes, pages: list[str]) -> tuple[list[str], list[int]]:
    global _OCR
    import numpy as np
    import pymupdf
    from rapidocr_onnxruntime import RapidOCR

    if _OCR is None:
        # Avoid each runtime session consuming all host cores.
        _OCR = RapidOCR(intra_op_num_threads=1, inter_op_num_threads=1)
    scans = []
    with pymupdf.open(stream=data, filetype="pdf") as document:
        for i, page in enumerate(document):
            if pages[i].strip():
                continue
            if not page.get_images(full=True) and not page.get_drawings():
                # A deliberately blank separator page is not a failed scanned invoice.
                continue
            pixmap = page.get_pixmap(matrix=pymupdf.Matrix(2.5, 2.5), alpha=False)
            array = np.frombuffer(pixmap.samples, dtype=np.uint8).reshape(pixmap.height, pixmap.width, pixmap.n)
            boxes, _ = _OCR(array)
            if not boxes:
                raise ValueError(f"OCR returned no text on PDF page {i + 1}")
            rows = []
            for box, text, confidence in sorted(boxes, key=lambda b: (sum(p[1] for p in b[0]) / 4, b[0][0][0])):
                y = sum(p[1] for p in box) / 4
                height = max(p[1] for p in box) - min(p[1] for p in box)
                if rows and abs(rows[-1][0] - y) <= max(5, height * .5):
                    rows[-1][1].append((box[0][0], text, float(confidence)))
                else:
                    rows.append([y, [(box[0][0], text, float(confidence))]])
            pages[i] = "\n".join(" | ".join(text for _, text, _ in sorted(row)) for _, row in rows)
            scans.append(i + 1)
    return pages, scans


def read_email(path: Path, batch: str) -> dict:
    data = path.read_bytes()
    message = parse_eml(data)
    received = parsedate_to_datetime(str(message["Date"]))
    if received.tzinfo is None:
        received = received.replace(tzinfo=timezone.utc)
    received = received.astimezone(timezone.utc)
    doc = f"{batch}/{path.name}"
    documents = [{"doc": doc, "pages": pages_of(data, "eml"), "scan_pages": []}]
    for name, payload in attachments(data):
        ext = Path(name).suffix.lower().lstrip(".")
        if ext not in ("pdf", "xlsx"):
            continue
        pages = pages_of(payload, ext)
        scans = []
        if ext == "pdf" and any(not page.strip() for page in pages):
            pages, scans = ocr_pages(payload, pages)
        documents.append({"doc": f"{doc}#{name}", "pages": pages, "scan_pages": scans})
    # X-GCA-Canary is deliberately absent. No other header becomes an instruction.
    return {"email": doc, "batch": batch, "received_at": received.isoformat(), "documents": documents}


def document_payload(records: list[dict]) -> list[dict]:
    return [{"received_at": record["received_at"], "doc": document["doc"],
             "pages": [{"page": i + 1, "text": page, "ocr": i + 1 in document["scan_pages"]}
                       for i, page in enumerate(document["pages"])]}
            for record in records for document in record["documents"]]
