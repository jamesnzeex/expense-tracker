import json
import logging
from typing import Any, Dict, Optional, Tuple

import requests

from app.config import settings

logger = logging.getLogger(__name__)


def build_prompt(extracted_text: str) -> str:
    categories = ", ".join(settings.allowed_categories)
    return (
        "You are an expense parser. Read the provided receipt or statement text and "
        "output JSON only. Use this JSON schema:\n"
        "{\n"
        '  "expenses": [\n'
        "    {\n"
        '      "amount": 12.34,\n'
        '      "category": "Groceries",\n'
        '      "merchant": "Store name",\n'
        '      "description": "optional short note",\n'
        '      "date": "2024-01-31"\n'
        "    }\n"
        "  ]\n"
        "}\n"
        f"Allowed categories (choose the best fit from this list only): {categories}. "
        "Ensure date is YYYY-MM-DD. "
        "If data is missing, make the best guess from text instead of leaving nulls. "
        "Return JSON and nothing else.\n\n"
        f"Text:\n{extracted_text}"
    )


def generate_expenses_from_text(
    extracted_text: str, image_b64: Optional[str] = None
) -> Tuple[str, Optional[Dict[str, Any]]]:
    prompt = build_prompt(extracted_text)

    content: list[Dict[str, Any]] = [{"type": "text", "text": prompt}]
    if image_b64:
        content.append(
            {
                "type": "image_url",
                "image_url": {"url": f"data:image/jpeg;base64,{image_b64}"},
            }
        )

    payload: Dict[str, Any] = {
        "model": settings.ollama_model,
        "stream": False,
        "messages": [
            {
                "role": "user",
                "content": content,
            }
        ],
    }

    response = requests.post(
        f"{settings.ollama_url}/v1/chat/completions", json=payload, timeout=120
    )
    response.raise_for_status()
    body = response.json()
    raw = ""
    try:
        raw = body["choices"][0]["message"]["content"]
    except (KeyError, IndexError):
        logger.error("Unexpected Ollama response format: %s", body)

    parsed = None
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError:
        logger.warning("Ollama did not return valid JSON: %s", raw[:200])

    return raw, parsed
