import json
import logging
from typing import Any, Dict, Optional, Tuple

import requests
from requests import RequestException

from app.config import settings

logger = logging.getLogger(__name__)


def build_prompt(extracted_text: str) -> str:
    categories = ", ".join(settings.allowed_categories)
    return (
        "You are an expense parser. Read the provided receipt or statement text and "
        "output JSON only. The text may contain one document or multiple documents "
        "joined together with document markers. If image attachments are included in "
        "the request, inspect those images in the same order they were sent. "
        "Extract every expense you can find "
        "across all documents without duplicating entries. Use this JSON schema:\n"
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
        "Only include a date if it is explicitly present in the source. "
        "If the date is unclear or missing, leave it empty rather than guessing. "
        "Ensure any provided date is YYYY-MM-DD. "
        "Return JSON and nothing else.\n\n"
        f"Text:\n{extracted_text}"
    )


def generate_expenses_from_text(
    extracted_text: str,
    image_data_uris: Optional[list[str]] = None,
    enable_thinking: Optional[bool] = None,
) -> Tuple[str, Optional[Dict[str, Any]]]:
    prompt = build_prompt(extracted_text)

    content: list[Dict[str, Any]] = [{"type": "text", "text": prompt}]
    for image in image_data_uris or []:
        content.append(
            {
                "type": "image_url",
                "image_url": {"url": image},
            }
        )

    payload: Dict[str, Any] = {
        "model": settings.vllm_model,
        "stream": False,
        "messages": [
            {
                "role": "user",
                "content": content,
            }
        ],
    }
    if enable_thinking is not None:
        payload["chat_template_kwargs"] = {"enable_thinking": enable_thinking}

    endpoint = f"{settings.vllm_url.rstrip('/')}/v1/chat/completions"
    logger.info(
        "Calling vLLM endpoint=%s model=%s enable_thinking=%s",
        endpoint,
        settings.vllm_model,
        enable_thinking,
    )

    try:
        response = requests.post(endpoint, json=payload, timeout=7200)
        response.raise_for_status()
    except RequestException as exc:
        raise RuntimeError(
            "Unable to reach vLLM at "
            f"{settings.vllm_url}. Set VLLM_URL to the reachable vLLM host "
            "for this runtime environment."
        ) from exc

    body = response.json()
    raw = ""
    try:
        raw = body["choices"][0]["message"]["content"]
    except (KeyError, IndexError):
        logger.error("Unexpected vLLM response format: %s", body)

    parsed = None
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError:
        logger.warning("vLLM did not return valid JSON: %s", raw[:200])

    return raw, parsed
