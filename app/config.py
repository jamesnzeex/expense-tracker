import os
from dataclasses import dataclass
from pathlib import Path


@dataclass
class Settings:
    telegram_token: str = os.getenv("TELEGRAM_BOT_TOKEN", "your-telegram-bot-token")
    ollama_url: str = os.getenv("OLLAMA_URL", "http://localhost:11434")
    ollama_model: str = os.getenv("OLLAMA_MODEL", "qwen3-vl:8b-instruct")
    database_url: str = os.getenv("DATABASE_URL", "sqlite:///./expense_tracker.db")
    storage_dir: Path = Path(os.getenv("STORAGE_DIR", "./uploads"))
    global_registration_password: str = os.getenv(
        "GLOBAL_REGISTRATION_PASSWORD", "your-password"
    )
    allowed_categories: list[str] = [
        c.strip() for c in os.getenv("ALLOWED_CATEGORIES", "").split(",") if c.strip()
    ] or [
        "Food",
        "Transport",
        "Groceries",
        "Shopping",
        "Bills",
        "Utilities",
        "Entertainment",
        "Travel",
        "Health",
        "Other",
    ]


settings = Settings()
settings.storage_dir.mkdir(parents=True, exist_ok=True)
