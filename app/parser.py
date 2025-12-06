import base64
from pathlib import Path
from typing import Optional

from pypdf import PdfReader


def extract_text_from_file(file_path: Path) -> Optional[str]:
    suffix = file_path.suffix.lower()
    if suffix == ".pdf":
        return _extract_pdf_text(file_path)
    if suffix in {".txt", ".csv"}:
        return file_path.read_text(errors="ignore")
    return None


def _extract_pdf_text(file_path: Path) -> str:
    reader = PdfReader(str(file_path))
    text_parts = []
    for page in reader.pages:
        text_parts.append(page.extract_text() or "")
    return "\n".join(text_parts).strip()


def encode_image_to_base64(file_path: Path) -> Optional[str]:
    suffix = file_path.suffix.lower()
    if suffix not in {".png", ".jpg", ".jpeg", ".webp", ".heic"}:
        return None
    return base64.b64encode(file_path.read_bytes()).decode("utf-8")
