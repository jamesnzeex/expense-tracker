import json
import logging
import os
from typing import Any, Dict, Optional, Tuple
from urllib.parse import urlsplit, urlunsplit

import requests
from requests import ConnectionError as RequestsConnectionError
from requests import RequestException, Timeout

from app.config import _default_vllm_url, settings

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


def _configured_vllm_url() -> str:
    raw = os.getenv("VLLM_URL")
    if raw is None:
        return _default_vllm_url()

    configured = raw.strip()
    if not configured or configured.lower() in {"auto", "detect"}:
        return _default_vllm_url()

    return configured


def _swap_hostname(base_url: str, hostname: str) -> str:
    parsed = urlsplit(base_url.rstrip("/"))
    if not parsed.scheme or not parsed.hostname:
        return base_url.rstrip("/")

    netloc = hostname
    if parsed.port:
        netloc = f"{netloc}:{parsed.port}"

    if parsed.username:
        userinfo = parsed.username
        if parsed.password is not None:
            userinfo = f"{userinfo}:{parsed.password}"
        netloc = f"{userinfo}@{netloc}"

    return urlunsplit(
        (parsed.scheme, netloc, parsed.path, parsed.query, parsed.fragment)
    ).rstrip("/")


def _vllm_base_urls() -> list[str]:
    primary = _configured_vllm_url().rstrip("/")
    candidates = [primary]

    parsed = urlsplit(primary)
    hostname = parsed.hostname
    if hostname in {"localhost", "127.0.0.1", "::1"}:
        alternate = _swap_hostname(primary, "host.docker.internal")
        if alternate not in candidates:
            candidates.append(alternate)
    elif hostname == "host.docker.internal":
        alternate = _swap_hostname(primary, "localhost")
        if alternate not in candidates:
            candidates.append(alternate)

    return candidates


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

    last_exc: Optional[BaseException] = None
    endpoint = ""
    base_urls = _vllm_base_urls()
    for base_url in base_urls:
        endpoint = f"{base_url.rstrip('/')}/v1/chat/completions"
        logger.info(
            "Calling vLLM endpoint=%s model=%s enable_thinking=%s",
            endpoint,
            settings.vllm_model,
            enable_thinking,
        )

        try:
            response = requests.post(endpoint, json=payload, timeout=7200)
            response.raise_for_status()
            break
        except (RequestsConnectionError, Timeout) as exc:
            last_exc = exc
            logger.warning("vLLM unreachable at %s: %s", base_url, exc)
            continue
        except RequestException as exc:
            raise RuntimeError(
                f"vLLM responded with an HTTP error at {base_url}."
            ) from exc
    else:
        tried = ", ".join(base_urls)
        raise RuntimeError(
            "Unable to reach vLLM. Tried: "
            f"{tried}. Set VLLM_URL to the reachable vLLM host for this runtime "
            "environment."
        ) from last_exc

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
