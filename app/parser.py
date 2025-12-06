import re
import base64
from pathlib import Path
from typing import Optional

from pypdf import PdfReader


def extract_text_from_file(file_path: Path) -> Optional[str]:
    suffix = file_path.suffix.lower()
    if suffix == ".pdf":
        raw = _extract_pdf_text(file_path)
        cleaned = clean_pypdf_text(raw)
        return cleaned
    if suffix in {".txt", ".csv"}:
        return file_path.read_text(errors="ignore")
    return None


def _extract_pdf_text(file_path: Path) -> str:
    reader = PdfReader(str(file_path))
    text_parts = []
    for page in reader.pages:
        text_parts.append(page.extract_text() or "")
    return "\n".join(text_parts).strip()


def _normalize_whitespace(text: str) -> str:
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def _remove_vertical_text_blocks(text: str, min_run=6) -> str:
    lines = text.splitlines()
    out = []
    run = []

    def flush_run():
        nonlocal run
        if len([x for x in run if x.strip()]) >= min_run:
            run = []
            return
        out.extend(run)
        run = []

    for ln in lines:
        if len(ln.strip()) <= 1:
            run.append(ln)
        else:
            flush_run()
            out.append(ln)
    flush_run()
    return "\n".join(out)


def clean_pypdf_text(raw: str) -> str:
    t = raw
    t = _remove_vertical_text_blocks(t, min_run=3)
    t = _normalize_whitespace(t)
    return t


def encode_image_to_base64(file_path: Path) -> Optional[str]:
    suffix = file_path.suffix.lower()
    if suffix not in {".png", ".jpg", ".jpeg", ".webp", ".heic"}:
        return None
    return base64.b64encode(file_path.read_bytes()).decode("utf-8")
