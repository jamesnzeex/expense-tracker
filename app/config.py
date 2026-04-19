import os
from dataclasses import dataclass, field
from pathlib import Path


def _running_in_docker() -> bool:
    return Path("/.dockerenv").exists()


def _default_vllm_url() -> str:
    if _running_in_docker():
        return "http://host.docker.internal:8000"
    return "http://localhost:8000"


@dataclass
class Settings:
    telegram_token: str = os.getenv("TELEGRAM_BOT_TOKEN")
    vllm_url: str = os.getenv("VLLM_URL", _default_vllm_url())
    vllm_model: str = os.getenv("VLLM_MODEL", "Qwen/Qwen3.6-35B-A3B-FP8")
    date_lookback_months: int = int(os.getenv("DATE_LOOKBACK_MONTHS", "6"))
    database_url: str = os.getenv("DATABASE_URL", "sqlite:///./expense_tracker.db")
    storage_dir: Path = Path(os.getenv("STORAGE_DIR", "./uploads"))
    document_batch_wait_seconds: float = float(
        os.getenv("DOCUMENT_BATCH_WAIT_SECONDS", "5.0")
    )
    global_registration_password: str = os.getenv("GLOBAL_REGISTRATION_PASSWORD", "J&S")
    allowed_categories: list[str] = field(
        default_factory=lambda: [
            c.strip()
            for c in os.getenv("ALLOWED_CATEGORIES", "").split(",")
            if c.strip()
        ]
        or [
            "Food",
            "Transport",
            "Groceries",
            "Shopping",
            "Bills",
            "Utilities",
            "Entertainment",
            "Travel",
            "Health",
            "Insurance",
            "Other",
        ]
    )


settings = Settings()
settings.storage_dir.mkdir(parents=True, exist_ok=True)
