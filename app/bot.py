import asyncio
import json
import mimetypes
import logging
import re
import time
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, date
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Dict, List, Optional, Tuple
from zoneinfo import ZoneInfo

from telegram import Update
from telegram.ext import (
    Application,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

from calendar import monthrange

from sqlalchemy import func

from app.config import settings
from app.database import SessionLocal, init_db
from app.datamodel import (
    Expense,
    PendingDateReviewItem,
    PendingDateReviewSession,
    User,
)
from app.vllm_client import generate_expenses_from_text
from app.parser import encode_image_to_base64, extract_text_from_file

logger = logging.getLogger(__name__)
SG_TZ = ZoneInfo("Asia/Singapore")
ALLOWED_CATEGORIES = settings.allowed_categories
ALLOWED_CATEGORIES_DISPLAY = ", ".join(ALLOWED_CATEGORIES)


@contextmanager
def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


@dataclass
class SessionState:
    user_id: int
    logged_in_at: datetime
    enable_thinking: Optional[bool] = None


@dataclass
class QueuedDocument:
    chat_id: int
    user_id: int
    source_name: str
    storage_path: Path
    mime_type: Optional[str] = None


@dataclass
class PendingDocumentBatch:
    chat_id: int
    user_id: int
    documents: List[QueuedDocument]
    timer_task: Optional[asyncio.Task] = None


session_cache: Dict[int, SessionState] = {}
pending_document_batches: Dict[int, PendingDocumentBatch] = {}
document_batch_lock: Optional[asyncio.Lock] = None


def _get_document_batch_lock() -> asyncio.Lock:
    global document_batch_lock
    if document_batch_lock is None:
        document_batch_lock = asyncio.Lock()
    return document_batch_lock


async def start(update: Update, _: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(
        "*Welcome to the expense tracker bot*.\n"
        "Commands:\n"
        "/register <password> - create an account\n"
        "/login <password> - login\n"
        "/thinking [on|off|default] - set thinking mode for your next LLM requests\n"
        "/me - check status\n"
        "/listexpense [count] - list your last N expenses (default 5, max 500)\n"
        "/addexpense - enter manual expenses (comma-separated lines)\n"
        "/editexpense - edit expenses via comma-separated lines (id first)\n"
        "/deleteexpense <expense_id> - delete a single expense by id\n"
        "/deletelast <count> - delete your last N expenses (max 100)\n"
        "/deletemonth <month> <year> - delete expenses in a given month\n"
        "/deleteall - delete all your expenses\n"
        "/summary [month] [year] - totals for given month (defaults to current)\n"
        "/summaryall - totals for every month in the database\n"
        f"Allowed categories: {ALLOWED_CATEGORIES_DISPLAY}\n\n"
        "Examples:\n"
        "/addexpense (format: amount,date,category[,merchant][,description])\n"
        "12.50,2024-06-01,Food,McDonalds,Lunch\n"
        "8,2024-06-02,Transport,Bus\n"
        "/editexpense (format: id,amount[,date][,category][,merchant][,description])\n"
        "1,15.20,2025-12-01,Food,McDonalds\n"
        "/deleteexpense (format: id)\n"
        "42\n"
        "/deletemonth 5 2024\n"
        "/summary 5 2024\n\n"
        "Send a credit card statement or receipt (PDF/image). Multiple uploads "
        f"within {settings.document_batch_wait_seconds:g}s will be batched together.\n"
        "If the bot asks you to review dates, reply with edits like:\n"
        "edit 1 2025-03-10, use 2, skip 3"
    )


async def register(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    password = " ".join(context.args).strip()
    if not password:
        await update.message.reply_text("Usage: /register <password>")
        return
    if not settings.global_registration_password:
        await update.message.reply_text(
            "Registration password not configured. Set GLOBAL_REGISTRATION_PASSWORD on the server."
        )
        return

    if password != settings.global_registration_password:
        await update.message.reply_text("Invalid registration password.")
        return

    telegram_user = update.effective_user
    with get_db() as db:
        existing = db.query(User).filter(User.telegram_id == telegram_user.id).first()
        if existing:
            await update.message.reply_text(
                "You are already registered. Use /login <password>."
            )
            return

        user = User(
            telegram_id=telegram_user.id,
            username=telegram_user.username,
        )
        db.add(user)
        db.commit()
        session_cache[telegram_user.id] = SessionState(
            user_id=user.id, logged_in_at=datetime.now(SG_TZ)
        )
        await update.message.reply_text(
            "Registered and logged in. Send a receipt/statement here or use /addexpense to add manually."
        )


async def login(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    password = " ".join(context.args).strip()
    if not password:
        await update.message.reply_text("Usage: /login <password>")
        return
    if not settings.global_registration_password:
        await update.message.reply_text(
            "Login unavailable. Set GLOBAL_REGISTRATION_PASSWORD on the server."
        )
        return

    telegram_user = update.effective_user
    with get_db() as db:
        user = db.query(User).filter(User.telegram_id == telegram_user.id).first()
        if not user:
            await update.message.reply_text(
                "No account found. Register with /register <password>."
            )
            return
        if password != settings.global_registration_password:
            await update.message.reply_text("Incorrect password.")
            return

        session_cache[telegram_user.id] = SessionState(
            user_id=user.id, logged_in_at=datetime.now(SG_TZ)
        )
        await update.message.reply_text(
            "Logged in. Send a receipt/statement here or use /addexpense to add manually."
        )


def _get_authenticated_user(telegram_id: int) -> Optional[SessionState]:
    return session_cache.get(telegram_id)


def _parse_thinking_mode(raw: str) -> Optional[bool]:
    value = raw.strip().lower()
    if value in {"on", "true", "1", "enable", "enabled"}:
        return True
    if value in {"off", "false", "0", "disable", "disabled"}:
        return False
    if value in {"default", "auto", "unset"}:
        return None
    raise ValueError("Expected on, off, or default.")


def _thinking_mode_label(value: Optional[bool]) -> str:
    if value is True:
        return "on"
    if value is False:
        return "off"
    return "default"


def _subtract_months(anchor: date, months: int) -> date:
    if months <= 0:
        return anchor
    year = anchor.year
    month = anchor.month - months
    while month <= 0:
        year -= 1
        month += 12
    day = min(anchor.day, monthrange(year, month)[1])
    return date(year, month, day)


def _validate_llm_date(raw_date: Optional[str]) -> Tuple[Optional[date], Optional[str]]:
    if raw_date is None:
        return None, "missing date"

    text = str(raw_date).strip()
    if not text:
        return None, "missing date"

    try:
        parsed = _parse_date(text)
    except Exception:  # noqa: BLE001
        parsed = None

    if parsed is None:
        return None, "invalid date format"

    today = datetime.now(SG_TZ).date()
    earliest = _subtract_months(today, settings.date_lookback_months)
    if parsed > today:
        return None, "future date"
    if parsed < earliest:
        return None, f"older than {settings.date_lookback_months} month(s)"
    return parsed, None


def _review_item_label(item_no: int) -> str:
    return f"{item_no}"


def _format_review_item_text(item: PendingDateReviewItem) -> str:
    llm_date = item.llm_date_raw or "missing"
    merchant = item.merchant or "Unspecified merchant"
    category = item.category or "Uncategorized"
    description = f" | {item.description}" if item.description else ""
    return (
        f"{item.item_no}. {merchant} | {item.amount:.2f} SGD | {category}{description} | "
        f"llm date: {llm_date} | {item.reason}"
    )


def _chunk_lines(lines: List[str], max_chars: int = 3500) -> List[str]:
    chunks = []
    current = []
    current_len = 0
    for line in lines:
        line_len = len(line) + 1
        if current and current_len + line_len > max_chars:
            chunks.append("\n".join(current))
            current = [line]
            current_len = len(line)
            continue
        current.append(line)
        current_len += line_len
    if current:
        chunks.append("\n".join(current))
    return chunks


def _split_review_actions(raw_text: str) -> List[str]:
    return [part.strip() for part in re.split(r"[;,\n]+", raw_text) if part.strip()]


def _parse_review_action(action: str) -> Tuple[Optional[str], Optional[int], Optional[str], Optional[str]]:
    lowered = action.strip().lower()
    if lowered == "cancel":
        return "cancel", None, None, None
    if lowered == "skip all":
        return "skip_all", None, None, None
    if lowered == "use all":
        return "use_all", None, None, None

    match = re.match(
        r"^(edit|use|skip)\s+(\d+)(?:\s+([0-9]{4}-[0-9]{2}-[0-9]{2}))?$",
        action.strip(),
        re.IGNORECASE,
    )
    if not match:
        return None, None, None, "Expected edit <n> <date>, use <n>, skip <n>, use all, or skip all."
    verb = match.group(1).lower()
    item_no = int(match.group(2))
    value = match.group(3)
    if verb == "edit" and not value:
        return None, None, None, "Edit requires a date: edit <n> <YYYY-MM-DD>."
    return verb, item_no, value, None


def _get_open_date_review_session(
    db,
    user_id: int,
) -> Optional[PendingDateReviewSession]:
    return (
        db.query(PendingDateReviewSession)
        .filter(
            PendingDateReviewSession.user_id == user_id,
            PendingDateReviewSession.status == "open",
        )
        .order_by(PendingDateReviewSession.created_at.desc())
        .first()
    )


def _get_pending_review_items(
    db,
    session_id: int,
) -> List[PendingDateReviewItem]:
    return (
        db.query(PendingDateReviewItem)
        .filter(
            PendingDateReviewItem.session_id == session_id,
            PendingDateReviewItem.status == "pending",
        )
        .order_by(PendingDateReviewItem.item_no.asc())
        .all()
    )


def _create_or_append_date_review_items(
    db,
    *,
    user_id: int,
    chat_id: int,
    review_payloads: List[dict],
) -> PendingDateReviewSession:
    session = _get_open_date_review_session(db, user_id)
    if session is None:
        session = PendingDateReviewSession(
            user_id=user_id,
            chat_id=chat_id,
            status="open",
        )
        db.add(session)
        db.flush()
    else:
        session.chat_id = chat_id
        session.updated_at = datetime.now(SG_TZ)

    current_max_item_no = (
        db.query(func.max(PendingDateReviewItem.item_no))
        .filter(PendingDateReviewItem.session_id == session.id)
        .scalar()
        or 0
    )

    for offset, payload in enumerate(review_payloads, start=1):
        raw_date = payload.get("llm_date_raw")
        item = PendingDateReviewItem(
            session_id=session.id,
            item_no=current_max_item_no + offset,
            source_name=payload.get("source_name"),
            amount=float(payload["amount"]),
            category=payload.get("category"),
            merchant=payload.get("merchant"),
            description=payload.get("description"),
            llm_date_raw=raw_date,
            llm_date_parsed=payload.get("llm_date_parsed"),
            reason=payload.get("reason"),
            raw_payload=payload["raw_payload"],
            status="pending",
        )
        db.add(item)

    db.flush()
    return session


def _render_review_prompt(session: PendingDateReviewSession, items: List[PendingDateReviewItem]) -> str:
    lines = [
        "Some extracted expenses need date review.",
        "Reply with any of these forms, separated by commas or semicolons:",
        "edit 1 2025-03-10, use 2, skip 3",
        "use all",
        "skip all",
        "",
        "Pending items:",
    ]
    for item in items:
        lines.append(_format_review_item_text(item))
    lines.append("")
    lines.append(
        f"Window: dates must be on or before today and within the last {settings.date_lookback_months} month(s) unless you explicitly use or edit them."
    )
    lines.append(f"Review batch id: {session.id}")
    return "\n".join(lines)


def _user_expense_stats(user_id: int) -> Tuple[int, Optional[datetime]]:
    with get_db() as db:
        total = (
            db.query(func.count(Expense.id)).filter(Expense.user_id == user_id).scalar()
            or 0
        )
        last = (
            db.query(Expense.created_at)
            .filter(Expense.user_id == user_id)
            .order_by(Expense.created_at.desc())
            .limit(1)
            .scalar()
        )
    return total, last


async def me(update: Update, _: ContextTypes.DEFAULT_TYPE) -> None:
    telegram_user = update.effective_user
    state = _get_authenticated_user(telegram_user.id)
    if not state:
        await update.message.reply_text("Not logged in. Use /login <password>.")
        return
    total, last = _user_expense_stats(state.user_id)
    last_display = "n/a"
    if last:
        last_dt = last
        if last.tzinfo is None:
            last_dt = last.replace(tzinfo=SG_TZ)
        last_display = last_dt.astimezone(SG_TZ).isoformat(timespec="seconds")
    await update.message.reply_text(
        f"Logged in as {telegram_user.username or telegram_user.id}. "
        f"Session started at {state.logged_in_at.isoformat()}.\n"
        f"Total expenses: {total}. Last update: {last_display}.\n"
        f"Thinking mode: {_thinking_mode_label(state.enable_thinking)}."
    )


async def thinking(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    telegram_user = update.effective_user
    state = _get_authenticated_user(telegram_user.id)
    if not state:
        await update.message.reply_text("Please /login first.")
        return

    if not context.args:
        await update.message.reply_text(
            "Usage: /thinking on|off|default\n"
            f"Current setting: {_thinking_mode_label(state.enable_thinking)}"
        )
        return

    try:
        state.enable_thinking = _parse_thinking_mode(context.args[0])
    except ValueError:
        await update.message.reply_text("Usage: /thinking on|off|default")
        return

    await update.message.reply_text(
        "Thinking mode set to "
        f"{_thinking_mode_label(state.enable_thinking)} for your next LLM requests."
    )


def _is_batchable_document(storage_path: Path) -> bool:
    return storage_path.suffix.lower() in {
        ".pdf",
        ".txt",
        ".csv",
        ".png",
        ".jpg",
        ".jpeg",
        ".webp",
        ".heic",
    }


def _guess_image_mime_type(source_name: str, explicit_mime_type: Optional[str]) -> str:
    if explicit_mime_type and explicit_mime_type.startswith("image/"):
        return explicit_mime_type
    guessed_mime_type, _ = mimetypes.guess_type(source_name)
    if guessed_mime_type and guessed_mime_type.startswith("image/"):
        return guessed_mime_type
    return "image/jpeg"


def _normalize_upload_name(file_name: str, mime_type: Optional[str]) -> str:
    if Path(file_name).suffix:
        return file_name
    guessed_extension = mimetypes.guess_extension(mime_type or "") or ""
    return f"{file_name}{guessed_extension}"


def _build_image_data_uri(document: QueuedDocument) -> Optional[str]:
    image_b64 = encode_image_to_base64(document.storage_path)
    if not image_b64:
        return None
    mime_type = _guess_image_mime_type(document.source_name, document.mime_type)
    return f"data:{mime_type};base64,{image_b64}"


def _build_document_payload(
    documents: List[QueuedDocument],
) -> Tuple[str, List[str], List[str]]:
    sections = []
    image_data_uris: List[str] = []
    skipped: List[str] = []
    for index, document in enumerate(documents, start=1):
        extracted_text = extract_text_from_file(document.storage_path) or ""
        image_data_uri = _build_image_data_uri(document)
        if image_data_uri:
            image_data_uris.append(image_data_uri)
        if not extracted_text:
            if image_data_uri:
                sections.append(
                    "\n".join(
                        [
                            f"--- BEGIN IMAGE {index}: {document.source_name} ---",
                            "This attachment is provided as an image.",
                            f"--- END IMAGE {index}: {document.source_name} ---",
                        ]
                    )
                )
                continue
            skipped.append(document.source_name)
            continue
        sections.append(
            "\n".join(
                [
                    f"--- BEGIN DOCUMENT {index}: {document.source_name} ---",
                    extracted_text,
                    f"--- END DOCUMENT {index}: {document.source_name} ---",
                ]
            )
        )
    return "\n\n".join(sections), image_data_uris, skipped


def _format_batch_wait_seconds() -> str:
    seconds = settings.document_batch_wait_seconds
    if seconds.is_integer():
        return str(int(seconds))
    return f"{seconds:g}"


async def _add_document_to_batch(
    application: Application,
    telegram_user_id: int,
    queued_document: QueuedDocument,
) -> int:
    async with _get_document_batch_lock():
        batch = pending_document_batches.get(telegram_user_id)
        if batch is None:
            batch = PendingDocumentBatch(
                chat_id=queued_document.chat_id,
                user_id=queued_document.user_id,
                documents=[],
            )
            pending_document_batches[telegram_user_id] = batch

        batch.documents.append(queued_document)

        existing_task = batch.timer_task
        if existing_task is not None and not existing_task.done():
            existing_task.cancel()

        batch.timer_task = application.create_task(
            _flush_document_batch_after_delay(application, telegram_user_id)
        )
        return len(batch.documents)


async def _download_and_stage_document(
    application: Application,
    telegram_user_id: int,
    queued_document: QueuedDocument,
    telegram_file,
) -> None:
    try:
        queued_document.storage_path.parent.mkdir(parents=True, exist_ok=True)
        await telegram_file.download_to_drive(
            custom_path=str(queued_document.storage_path)
        )
    except Exception as exc:  # noqa: BLE001
        logger.exception(
            "Failed to download %s for telegram_user_id=%s",
            queued_document.source_name,
            telegram_user_id,
        )
        await application.bot.send_message(
            chat_id=queued_document.chat_id,
            text=f"Failed to download {queued_document.source_name}: {exc}",
        )
        return

    if _is_batchable_document(queued_document.storage_path):
        batch_size = await _add_document_to_batch(
            application, telegram_user_id, queued_document
        )
        if batch_size == 1:
            status = (
                f"Downloaded {queued_document.source_name}. Waiting "
                f"{_format_batch_wait_seconds()}s for more files before parsing."
            )
        else:
            status = (
                f"Downloaded {queued_document.source_name}. Added to batch "
                f"({batch_size} file(s) total). Parsing starts "
                f"{_format_batch_wait_seconds()}s after the last file."
            )
        await application.bot.send_message(
            chat_id=queued_document.chat_id,
            text=status,
        )
        return

    await application.bot.send_message(
        chat_id=queued_document.chat_id,
        text=f"Downloaded {queued_document.source_name}. Parsing now...",
    )
    await _process_document_batch(
        application,
        telegram_user_id,
        PendingDocumentBatch(
            chat_id=queued_document.chat_id,
            user_id=queued_document.user_id,
            documents=[queued_document],
        ),
    )


async def _flush_document_batch_after_delay(
    application: Application, telegram_user_id: int
) -> None:
    try:
        await asyncio.sleep(settings.document_batch_wait_seconds)
    except asyncio.CancelledError:
        return

    async with _get_document_batch_lock():
        batch = pending_document_batches.get(telegram_user_id)
        if batch is None or batch.timer_task is not asyncio.current_task():
            return
        pending_document_batches.pop(telegram_user_id, None)

    await application.bot.send_message(
        chat_id=batch.chat_id,
        text=f"Parsing batch of {len(batch.documents)} file(s) now...",
    )
    await _process_document_batch(application, telegram_user_id, batch)


async def _process_document_batch(
    application: Application, telegram_user_id: int, batch: PendingDocumentBatch
) -> None:
    combined_text, image_data_uris, skipped = _build_document_payload(batch.documents)
    source_name = ", ".join(document.source_name for document in batch.documents)

    if skipped:
        logger.warning(
            "Skipped unsupported attachments in batch for chat_id=%s: %s",
            batch.chat_id,
            ", ".join(skipped),
        )

    prompt_text = combined_text or "Inspect the attached images in order and extract expenses."
    if image_data_uris:
        prompt_text = (
            "The request includes image attachments in the same order as the payload.\n\n"
            f"{prompt_text}"
        )

    if not combined_text and not image_data_uris:
        names = ", ".join(document.source_name for document in batch.documents)
        await application.bot.send_message(
            chat_id=batch.chat_id,
            text=(
                f"Unable to extract usable text from: {names}. "
                "Provide PDF, text, or image."
            ),
        )
        return

    try:
        state = _get_authenticated_user(telegram_user_id)
        raw_llm, parsed = await asyncio.get_running_loop().run_in_executor(
            None,
            generate_expenses_from_text,
            prompt_text,
            image_data_uris,
            state.enable_thinking if state else None,
        )
    except Exception as exc:  # noqa: BLE001
        names = ", ".join(document.source_name for document in batch.documents)
        logger.exception("Failed to call vLLM for batch: %s", names)
        await application.bot.send_message(
            chat_id=batch.chat_id,
            text=f"vLLM request failed for batch: {exc}",
        )
        return

    if not parsed or "expenses" not in parsed:
        snippet = raw_llm[:500]
        await application.bot.send_message(
            chat_id=batch.chat_id,
            text=(
                "Could not parse JSON for the current batch. "
                f"Raw response preview:\n{snippet}"
            ),
        )
        return

    saved, review_count, skipped_count, review_session = _process_llm_expenses(
        application,
        user_id=batch.user_id,
        chat_id=batch.chat_id,
        source_name=source_name,
        expenses=parsed["expenses"],
    )
    if skipped_count:
        await application.bot.send_message(
            chat_id=batch.chat_id,
            text=f"Skipped {skipped_count} expense(s) with missing or invalid amount.",
        )
    if saved and not review_count:
        await application.bot.send_message(
            chat_id=batch.chat_id,
            text=(
                f"Saved {saved} expense(s) from {len(batch.documents)} file(s). "
                f"Use /listexpense {saved} to view recent entries."
            ),
        )
    elif review_count:
        with get_db() as db:
            pending_items = _get_pending_review_items(db, review_session.id)
        review_message = _render_review_prompt(review_session, pending_items)
        for chunk in _chunk_lines(review_message.splitlines()):
            await application.bot.send_message(chat_id=batch.chat_id, text=chunk)
        if saved:
            await application.bot.send_message(
                chat_id=batch.chat_id,
                text=f"Saved {saved} expense(s) and queued {review_count} for date review.",
            )
    elif saved:
        await application.bot.send_message(
            chat_id=batch.chat_id,
            text=(
                f"Saved {saved} expense(s) from {len(batch.documents)} file(s). "
                f"Use /listexpense {saved} to view recent entries."
            ),
        )
    else:
        await application.bot.send_message(
            chat_id=batch.chat_id,
            text=(
                f"No expenses were saved from the {len(batch.documents)}-file batch. "
                "Please try again or adjust the file."
            ),
        )


async def handle_document(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    telegram_user = update.effective_user
    session_state = _get_authenticated_user(telegram_user.id)
    if not session_state:
        await update.message.reply_text("Please /login before sending receipts.")
        return

    message = update.message
    if not message:
        return

    file_name = "upload"
    telegram_file = None
    mime_type = None
    if message.document:
        telegram_file = await message.document.get_file()
        file_name = message.document.file_name or file_name
        mime_type = message.document.mime_type
    elif message.photo:
        telegram_file = await message.photo[-1].get_file()
        file_name = f"{time.time_ns()}.jpg"
        mime_type = "image/jpeg"
    else:
        await message.reply_text("Unsupported attachment type.")
        return
    file_name = _normalize_upload_name(file_name, mime_type)

    chat = update.effective_chat
    if not chat:
        return

    storage_path = (
        settings.storage_dir / f"{telegram_user.id}_{time.time_ns()}_{file_name}"
    )
    queued_document = QueuedDocument(
        chat_id=chat.id,
        user_id=session_state.user_id,
        source_name=file_name,
        storage_path=storage_path,
        mime_type=mime_type,
    )
    context.application.create_task(
        _download_and_stage_document(
            context.application,
            telegram_user.id,
            queued_document,
            telegram_file,
        )
    )
    await message.reply_text(f"Received {file_name}. Downloading now...")


def _build_llm_expense_payload(
    expense: dict,
) -> Tuple[Optional[dict], Optional[str]]:
    amount = expense.get("amount")
    if amount is None:
        return None, "missing amount"
    try:
        amount_value = float(amount)
    except (TypeError, ValueError):
        return None, "invalid amount"

    raw_category = (expense.get("category") or "").strip()
    category = _normalize_category(raw_category)
    if category is None:
        category = next(
            (c for c in ALLOWED_CATEGORIES if c.lower() == "other"),
            ALLOWED_CATEGORIES[0],
        )

    merchant = (expense.get("merchant") or "")[:128] or None
    description = expense.get("description") or None
    raw_date = (expense.get("date") or "").strip() or None
    llm_date_parsed, reason = _validate_llm_date(raw_date)
    payload = {
        "amount": amount_value,
        "category": category,
        "merchant": merchant,
        "description": description,
        "llm_date_raw": raw_date,
        "llm_date_parsed": llm_date_parsed,
        "raw_payload": json.dumps(expense, ensure_ascii=False),
    }
    return payload, reason


def _parse_pending_review_actions(raw_text: str) -> Tuple[List[dict], List[str]]:
    actions: List[dict] = []
    errors: List[str] = []
    for part in _split_review_actions(raw_text):
        verb, item_no, value, error = _parse_review_action(part)
        if error:
            errors.append(f"{part} -> {error}")
            continue
        if verb == "cancel":
            return [], []
        actions.append({"verb": verb, "item_no": item_no, "value": value})
    return actions, errors


def _apply_pending_review_actions(
    db,
    session: PendingDateReviewSession,
    actions: List[dict],
) -> Tuple[int, int, List[str], bool]:
    pending_items = {
        item.item_no: item for item in _get_pending_review_items(db, session.id)
    }
    if not pending_items:
        session.status = "closed"
        session.updated_at = datetime.now(SG_TZ)
        return 0, 0, ["No pending review items remain."], True

    saved = 0
    skipped = 0
    errors: List[str] = []
    resolved_any = False

    def _save_item(item: PendingDateReviewItem, resolved_date: date) -> None:
        nonlocal saved, resolved_any
        db.add(
            Expense(
                user_id=session.user_id,
                amount=item.amount,
                category=item.category,
                merchant=item.merchant,
                description=item.description,
                expense_date=resolved_date,
                raw_payload=item.raw_payload,
            )
        )
        item.status = "approved"
        item.resolved_date = resolved_date
        item.updated_at = datetime.now(SG_TZ)
        saved += 1
        resolved_any = True

    def _skip_item(item: PendingDateReviewItem) -> None:
        nonlocal skipped, resolved_any
        item.status = "skipped"
        item.resolved_date = None
        item.updated_at = datetime.now(SG_TZ)
        skipped += 1
        resolved_any = True

    for action in actions:
        verb = action["verb"]
        item_no = action["item_no"]
        item = pending_items.get(item_no)
        if item is None:
            errors.append(f"Item {item_no} is not pending in the current review batch.")
            continue
        if item.status != "pending":
            errors.append(f"Item {item_no} was already resolved.")
            continue

        if verb == "skip":
            _skip_item(item)
            continue

        if verb == "use":
            if item.llm_date_parsed is None:
                errors.append(f"Item {item_no} has no usable LLM date. Use edit {item_no} YYYY-MM-DD.")
                continue
            _save_item(item, item.llm_date_parsed)
            continue

        if verb == "edit":
            user_date = _parse_date(action["value"])
            if user_date is None:
                errors.append(f"Item {item_no} -> date must be YYYY-MM-DD.")
                continue
            _save_item(item, user_date)
            continue

        if verb == "skip_all":
            for pending_item in pending_items.values():
                if pending_item.status == "pending":
                    _skip_item(pending_item)
            break

        if verb == "use_all":
            for pending_item in pending_items.values():
                if pending_item.status == "pending":
                    if pending_item.llm_date_parsed is None:
                        errors.append(
                            f"Item {pending_item.item_no} has no usable LLM date. Use edit {pending_item.item_no} YYYY-MM-DD."
                        )
                        continue
                    _save_item(pending_item, pending_item.llm_date_parsed)
            break

    remaining = _get_pending_review_items(db, session.id)
    if not remaining:
        session.status = "closed"
        session.updated_at = datetime.now(SG_TZ)
    elif resolved_any:
        session.updated_at = datetime.now(SG_TZ)

    return saved, skipped, errors, not remaining


async def _handle_pending_date_review_reply(
    application: Application,
    user_id: int,
    chat_id: int,
    raw_text: str,
) -> bool:
    with get_db() as db:
        session = _get_open_date_review_session(db, user_id)
        if session is None:
            return False

        actions, parse_errors = _parse_pending_review_actions(raw_text)
        if not actions and not parse_errors:
            await application.bot.send_message(
                chat_id=chat_id,
                text="Reply with edit <n> <YYYY-MM-DD>, use <n>, skip <n>, use all, or skip all.",
            )
            return True

        if not actions and parse_errors:
            await application.bot.send_message(
                chat_id=chat_id,
                text="Could not parse review reply:\n" + "\n".join(parse_errors),
            )
            return True

        saved, skipped, action_errors, _closed = _apply_pending_review_actions(
            db,
            session,
            actions,
        )
        db.commit()

        pending_items = _get_pending_review_items(db, session.id)
        remaining_count = len(pending_items)

    response_lines = []
    if saved:
        response_lines.append(f"Saved {saved} expense(s).")
    if skipped:
        response_lines.append(f"Skipped {skipped} expense(s).")
    if action_errors:
        response_lines.append("Errors:\n" + "\n".join(action_errors))
    if remaining_count:
        review_message = _render_review_prompt(session, pending_items)
        if response_lines:
            response_lines.insert(0, f"{remaining_count} item(s) still need review.")
            await application.bot.send_message(
                chat_id=chat_id,
                text="\n".join(response_lines),
            )
        else:
            await application.bot.send_message(
                chat_id=chat_id,
                text=f"{remaining_count} item(s) still need review.",
            )
        for chunk in _chunk_lines(review_message.splitlines()):
            await application.bot.send_message(chat_id=chat_id, text=chunk)
    else:
        response_lines.append("Date review completed.")
        await application.bot.send_message(chat_id=chat_id, text="\n".join(response_lines))
    return True


def _save_expenses(db, expenses: List[dict], user_id: int) -> int:
    if not expenses:
        return 0

    saved = 0
    for expense in expenses:
        record = Expense(user_id=user_id, **expense)
        db.add(record)
        saved += 1
    return saved


def _process_llm_expenses(
    application: Application,
    *,
    user_id: int,
    chat_id: int,
    source_name: str,
    expenses: List[dict],
) -> Tuple[int, int, int, Optional[PendingDateReviewSession]]:
    valid_expenses: List[dict] = []
    review_payloads: List[dict] = []
    skipped = 0

    for expense in expenses:
        payload, reason = _build_llm_expense_payload(expense)
        if payload is None:
            skipped += 1
            continue
        if payload["llm_date_parsed"] is not None:
            valid_expenses.append(
                {
                    "amount": payload["amount"],
                    "category": payload["category"],
                    "merchant": payload["merchant"],
                    "description": payload["description"],
                    "expense_date": payload["llm_date_parsed"],
                    "raw_payload": payload["raw_payload"],
                }
            )
            continue

        review_payloads.append(
            {
                "source_name": source_name,
                "amount": payload["amount"],
                "category": payload["category"],
                "merchant": payload["merchant"],
                "description": payload["description"],
                "llm_date_raw": payload["llm_date_raw"],
                "llm_date_parsed": payload["llm_date_parsed"],
                "reason": reason or "date requires review",
                "raw_payload": payload["raw_payload"],
            }
        )

    review_session = None
    with get_db() as db:
        if valid_expenses:
            _save_expenses(db, valid_expenses, user_id)

        if review_payloads:
            review_session = _create_or_append_date_review_items(
                db,
                user_id=user_id,
                chat_id=chat_id,
                review_payloads=review_payloads,
            )

        db.commit()

    return len(valid_expenses), len(review_payloads), skipped, review_session


def _parse_date(raw: Optional[str]) -> Optional[date]:
    if not raw:
        return None
    try:
        return date.fromisoformat(raw)
    except ValueError:
        return None


def _normalize_category(raw: Optional[str]) -> Optional[str]:
    if not raw:
        return None
    for allowed in ALLOWED_CATEGORIES:
        if raw.strip().lower() == allowed.lower():
            return allowed
    return None


def _parse_amount(raw: str) -> Optional[float]:
    normalized = raw.replace(",", ".").strip()
    try:
        return float(Decimal(normalized))
    except (InvalidOperation, ValueError):
        return None


async def add_expense(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    telegram_user = update.effective_user
    state = _get_authenticated_user(telegram_user.id)
    if not state:
        await update.message.reply_text("Please /login first.")
        return

    context.user_data["mode"] = "add_expenses"
    context.user_data["add_user_id"] = state.user_id
    await update.message.reply_text(
        "Send expense lines as: amount,date,category[,merchant][,description]\n"
        "One per line. Example: 12.50,2024-06-01,Food,McDonalds,Lunch\n"
        "Send 'cancel' to stop."
    )


async def edit_expense(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    telegram_user = update.effective_user
    state = _get_authenticated_user(telegram_user.id)
    if not state:
        await update.message.reply_text("Please /login first.")
        return

    context.user_data["mode"] = "edit_expenses"
    context.user_data["edit_user_id"] = state.user_id
    await update.message.reply_text(
        "Send expense edit lines as: id,amount[,date][,category][,merchant][,description]\n"
        "Example:\n"
        "1,15.20,2025-12-01,Food,McDonalds\n"
        "2,8.20,2025-12-01,Food\n"
        "Send multiple lines; 'cancel' to stop."
    )


async def list_expenses(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    telegram_user = update.effective_user
    state = _get_authenticated_user(telegram_user.id)
    if not state:
        await update.message.reply_text("Please /login first.")
        return

    try:
        limit = int(context.args[0]) if context.args else 5
    except (ValueError, TypeError):
        await update.message.reply_text(
            "Usage: /listexpense [count], e.g., /listexpense 10"
        )
        return
    limit = max(1, min(limit, 500))

    with get_db() as db:
        items = (
            db.query(Expense)
            .filter(Expense.user_id == state.user_id)
            .order_by(Expense.created_at.desc())
            .limit(limit)
            .all()
        )
    if not items:
        await update.message.reply_text("No expenses yet.")
        return

    lines = []
    for exp in items:
        date_display = exp.expense_date.isoformat() if exp.expense_date else "n/a"
        lines.append(
            f"ID {exp.id} | {exp.amount:.2f} SGD | {exp.category or 'Uncategorized'} | "
            f"{exp.merchant or ''} | {date_display}"
        )
    await update.message.reply_text("\n".join(lines))


async def summary(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    telegram_user = update.effective_user
    state = _get_authenticated_user(telegram_user.id)
    if not state:
        await update.message.reply_text("Please /login first.")
        return

    today = datetime.now(SG_TZ).date()
    month = today.month
    year = today.year
    if context.args:
        try:
            month = int(context.args[0])
            if len(context.args) > 1:
                year = int(context.args[1])
        except (ValueError, TypeError):
            await update.message.reply_text(
                "Usage: /summary [month] [year], e.g., /summary 5 2024"
            )
            return
    if not (1 <= month <= 12):
        await update.message.reply_text("Month must be between 1 and 12.")
        return

    month_start = today.replace(year=year, month=month, day=1)
    if month == 12:
        next_month = month_start.replace(year=year + 1, month=1, day=1)
    else:
        next_month = month_start.replace(month=month + 1, day=1)

    with get_db() as db:
        rows = (
            db.query(Expense.category, Expense.amount)
            .filter(
                Expense.user_id == state.user_id,
                Expense.expense_date >= month_start,
                Expense.expense_date < next_month,
            )
            .all()
        )
    if not rows:
        await update.message.reply_text("No expenses this month yet.")
        return

    totals: Dict[str, float] = {}
    for row in rows:
        key = row.category or "Uncategorized"
        totals[key] = totals.get(key, 0.0) + float(row.amount)

    lines = [f"{category} - {amt:.2f} SGD" for category, amt in totals.items()]
    await update.message.reply_text(f"{month} {year}:\n" + "\n".join(lines))


async def summary_all(update: Update, _: ContextTypes.DEFAULT_TYPE) -> None:
    telegram_user = update.effective_user
    state = _get_authenticated_user(telegram_user.id)
    if not state:
        await update.message.reply_text("Please /login first.")
        return

    with get_db() as db:
        expenses = (
            db.query(Expense)
            .filter(Expense.user_id == state.user_id, Expense.expense_date.isnot(None))
            .order_by(Expense.expense_date)
            .all()
        )
    if not expenses:
        await update.message.reply_text("No expenses with dates found.")
        return

    totals: Dict[str, Dict[str, float]] = {}
    for exp in expenses:
        key = exp.expense_date.strftime("%Y-%m")
        cat = exp.category or "Uncategorized"
        month_totals = totals.setdefault(key, {})
        month_totals[cat] = month_totals.get(cat, 0.0) + float(exp.amount)

    lines = []
    for key in sorted(totals.keys(), reverse=True):
        lines.append(key)
        for cat, amt in totals[key].items():
            lines.append(f"  {cat}: {amt:.2f} SGD")
    await update.message.reply_text("\n".join(lines))


async def delete_all_expenses(update: Update, _: ContextTypes.DEFAULT_TYPE) -> None:
    telegram_user = update.effective_user
    state = _get_authenticated_user(telegram_user.id)
    if not state:
        await update.message.reply_text("Please /login first.")
        return

    with get_db() as db:
        deleted = (
            db.query(Expense)
            .filter(Expense.user_id == state.user_id)
            .delete(synchronize_session=False)
        )
        db.commit()
    await update.message.reply_text(f"Deleted {deleted} expense(s).")


async def delete_month_expenses(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    telegram_user = update.effective_user
    state = _get_authenticated_user(telegram_user.id)
    if not state:
        await update.message.reply_text("Please /login first.")
        return

    if len(context.args) < 2:
        await update.message.reply_text(
            "Usage: /deletemonth <month> <year>, e.g., /deletemonth 5 2024"
        )
        return
    try:
        month = int(context.args[0])
        year = int(context.args[1])
    except (ValueError, TypeError):
        await update.message.reply_text(
            "Usage: /deletemonth <month> <year>, e.g., /deletemonth 5 2024"
        )
        return
    if not (1 <= month <= 12):
        await update.message.reply_text("Month must be between 1 and 12.")
        return

    month_start = datetime.now(SG_TZ).replace(year=year, month=month, day=1).date()
    if month == 12:
        next_month = month_start.replace(year=year + 1, month=1, day=1)
    else:
        next_month = month_start.replace(month=month + 1, day=1)

    with get_db() as db:
        deleted = (
            db.query(Expense)
            .filter(
                Expense.user_id == state.user_id,
                Expense.expense_date >= month_start,
                Expense.expense_date < next_month,
            )
            .delete(synchronize_session=False)
        )
        db.commit()
    await update.message.reply_text(f"Deleted {deleted} expense(s) for {month}/{year}.")


async def delete_expense(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    telegram_user = update.effective_user
    state = _get_authenticated_user(telegram_user.id)
    if not state:
        await update.message.reply_text("Please /login first.")
        return

    if not context.args:
        await update.message.reply_text("Usage: /deleteexpense <expense_id>")
        return
    try:
        expense_id = int(context.args[0])
    except (ValueError, TypeError):
        await update.message.reply_text("Usage: /deleteexpense <expense_id>")
        return

    with get_db() as db:
        deleted = (
            db.query(Expense)
            .filter(Expense.user_id == state.user_id, Expense.id == expense_id)
            .delete(synchronize_session=False)
        )
        db.commit()
    if deleted:
        await update.message.reply_text(f"Deleted expense {expense_id}.")
    else:
        await update.message.reply_text("Expense not found or not yours.")


async def delete_last_expenses(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    telegram_user = update.effective_user
    state = _get_authenticated_user(telegram_user.id)
    if not state:
        await update.message.reply_text("Please /login first.")
        return

    try:
        count = int(context.args[0]) if context.args else 1
    except (ValueError, TypeError):
        await update.message.reply_text(
            "Usage: /deletelast <count>, e.g., /deletelast 5"
        )
        return
    count = max(1, min(count, 500))

    with get_db() as db:
        ids = (
            db.query(Expense.id)
            .filter(Expense.user_id == state.user_id)
            .order_by(Expense.created_at.desc())
            .limit(count)
            .all()
        )
        id_list = [row.id for row in ids]
        if not id_list:
            await update.message.reply_text("No expenses to delete.")
            return
        deleted = (
            db.query(Expense)
            .filter(Expense.user_id == state.user_id, Expense.id.in_(id_list))
            .delete(synchronize_session=False)
        )
        db.commit()
    await update.message.reply_text(f"Deleted {deleted} expense(s).")


def _parse_expense_line(line: str) -> Tuple[Optional[dict], Optional[str]]:
    parts = [p.strip() for p in line.split(",")]
    if len(parts) < 3:
        return None, "Need at least amount,date,category."
    amount = _parse_amount(parts[0])
    if amount is None:
        return None, "Amount must be a number."
    date_val = _parse_date(parts[1])
    if not date_val:
        return None, "Date must be YYYY-MM-DD."
    category = _normalize_category(parts[2])
    if category is None:
        return None, f"Category must be one of: {ALLOWED_CATEGORIES_DISPLAY}."
    merchant = parts[3] if len(parts) > 3 else None
    description = ",".join(parts[4:]).strip() if len(parts) > 4 else None
    return (
        {
            "amount": amount,
            "expense_date": date_val,
            "category": category,
            "merchant": merchant,
            "description": description,
        },
        None,
    )


def _parse_edit_line(line: str) -> Tuple[Optional[dict], Optional[str]]:
    parts = [p.strip() for p in line.split(",")]
    if len(parts) < 2:
        return None, "Need at least id,amount."
    try:
        exp_id = int(parts[0])
    except ValueError:
        return None, "ID must be a number."
    amount = _parse_amount(parts[1])
    if amount is None:
        return None, "Amount must be a number."

    payload: dict = {"id": exp_id, "amount": amount}
    if len(parts) > 2 and parts[2]:
        date_val = _parse_date(parts[2])
        if not date_val:
            return None, "Date must be YYYY-MM-DD."
        payload["expense_date"] = date_val
    if len(parts) > 3 and parts[3]:
        cat = _normalize_category(parts[3])
        if cat is None:
            return None, f"Category must be one of: {ALLOWED_CATEGORIES_DISPLAY}."
        payload["category"] = cat
    if len(parts) > 4 and parts[4]:
        payload["merchant"] = parts[4]
    if len(parts) > 5:
        payload["description"] = ",".join(parts[5:]).strip()
    return payload, None


async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    telegram_user = update.effective_user
    state = _get_authenticated_user(telegram_user.id)
    if not state:
        return

    mode = context.user_data.get("mode")
    text = update.message.text or ""
    lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
    if not mode or not lines:
        return

    if any(ln.lower() == "cancel" for ln in lines):
        context.user_data.clear()
        await update.message.reply_text("Cancelled.")
        return

    if mode == "add_expenses":
        saved = 0
        errors = []
        with get_db() as db:
            for ln in lines:
                if ln.lower() == "cancel":
                    continue
                parsed, err = _parse_expense_line(ln)
                if err:
                    errors.append(f"{ln} -> {err}")
                    continue
                record = Expense(user_id=state.user_id, raw_payload="manual", **parsed)
                db.add(record)
                saved += 1
            db.commit()
        if saved:
            await update.message.reply_text(f"Added {saved} expense(s).")
        if errors:
            await update.message.reply_text("Errors:\n" + "\n".join(errors))
        return

    if mode == "edit_expenses":
        edits = []
        errors = []
        with get_db() as db:
            for ln in lines:
                if ln.lower() == "cancel":
                    continue
                parsed, err = _parse_edit_line(ln)
                if err:
                    errors.append(f"{ln} -> {err}")
                    continue
                exp = (
                    db.query(Expense)
                    .filter(
                        Expense.user_id == state.user_id, Expense.id == parsed["id"]
                    )
                    .first()
                )
                if not exp:
                    errors.append(f"{ln} -> expense not found")
                    continue
                exp.amount = parsed["amount"]
                if "expense_date" in parsed:
                    exp.expense_date = parsed["expense_date"]
                if "category" in parsed:
                    exp.category = parsed["category"]
                if "merchant" in parsed:
                    exp.merchant = parsed["merchant"]
                if "description" in parsed:
                    exp.description = parsed["description"]
                edits.append(parsed["id"])
            db.commit()
        if edits:
            await update.message.reply_text(
                f"Updated {len(edits)} expense(s): {', '.join(map(str, edits))}"
            )
        if errors:
            await update.message.reply_text("Errors:\n" + "\n".join(errors))
        return

    raw_text = update.message.text or ""
    handled = await _handle_pending_date_review_reply(
        context.application,
        state.user_id,
        update.effective_chat.id if update.effective_chat else telegram_user.id,
        raw_text,
    )
    if handled:
        return


def main() -> None:
    logging.basicConfig(
        format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
        level=logging.INFO,
    )
    init_db()

    if not settings.telegram_token:
        raise RuntimeError("TELEGRAM_BOT_TOKEN is not set.")

    application = Application.builder().token(settings.telegram_token).build()
    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("register", register))
    application.add_handler(CommandHandler("login", login))
    application.add_handler(CommandHandler("thinking", thinking))
    application.add_handler(CommandHandler("me", me))
    application.add_handler(CommandHandler("listexpense", list_expenses))
    application.add_handler(CommandHandler("addexpense", add_expense))
    application.add_handler(CommandHandler("editexpense", edit_expense))
    application.add_handler(CommandHandler("summary", summary))
    application.add_handler(CommandHandler("summaryall", summary_all))
    application.add_handler(CommandHandler("deleteall", delete_all_expenses))
    application.add_handler(CommandHandler("deletemonth", delete_month_expenses))
    application.add_handler(CommandHandler("deleteexpense", delete_expense))
    application.add_handler(CommandHandler("deletelast", delete_last_expenses))
    application.add_handler(
        MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text)
    )
    application.add_handler(
        MessageHandler(filters.Document.ALL | filters.PHOTO, handle_document)
    )

    application.run_polling()


if __name__ == "__main__":
    main()
