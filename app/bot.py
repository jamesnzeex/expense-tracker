import asyncio
import logging
import time
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, date
from decimal import Decimal, InvalidOperation
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

from sqlalchemy import func

from app.config import settings
from app.database import SessionLocal, init_db
from app.datamodel import Expense, User
from app.ollama_client import generate_expenses_from_text
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


session_cache: Dict[int, SessionState] = {}


async def start(update: Update, _: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(
        "*Welcome to the expense tracker bot*.\n"
        "Commands:\n"
        "/register <password> - create an account\n"
        "/login <password> - login\n"
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
        "Send a credit card statement or receipt (in PDF/image) to extract expenses."
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
        f"Total expenses: {total}. Last update: {last_display}."
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
    if message.document:
        telegram_file = await message.document.get_file()
        file_name = message.document.file_name or file_name
    elif message.photo:
        telegram_file = await message.photo[-1].get_file()
        file_name = f"{int(time.time())}.jpg"
    else:
        await message.reply_text("Unsupported attachment type.")
        return

    storage_path = (
        settings.storage_dir / f"{telegram_user.id}_{int(time.time())}_{file_name}"
    )
    await telegram_file.download_to_drive(custom_path=str(storage_path))
    await message.reply_text("Received. Parsing now...")

    extracted_text = extract_text_from_file(storage_path) or ""
    image_b64 = encode_image_to_base64(storage_path)
    if not extracted_text and not image_b64:
        await message.reply_text("Unsupported file type. Provide PDF, text, or image.")
        return

    try:
        raw_llm, parsed = await asyncio.get_event_loop().run_in_executor(
            None,
            generate_expenses_from_text,
            extracted_text or "Use the attached image.",
            image_b64,
        )
    except Exception as exc:  # noqa: BLE001
        logger.exception("Failed to call Ollama")
        await message.reply_text(f"Ollama request failed: {exc}")
        return

    if not parsed or "expenses" not in parsed:
        snippet = raw_llm[:500]
        await message.reply_text(
            "Could not parse JSON from the model. Raw response preview:\n" f"{snippet}"
        )
        return

    saved = _save_expenses(parsed["expenses"], session_state.user_id)
    if saved:
        await message.reply_text(
            f"Saved {saved} expense(s). Use /listexpense to view recent entries."
        )
    else:
        await message.reply_text(
            "No expenses were saved. Please try again or adjust the file."
        )


def _save_expenses(expenses: List[dict], user_id: int) -> int:
    if not expenses:
        return 0

    saved = 0
    with get_db() as db:
        for expense in expenses:
            amount = expense.get("amount")
            if amount is None:
                continue
            raw_category = (expense.get("category") or "").strip()
            category = _normalize_category(raw_category)
            if category is None:
                category = next(
                    (c for c in ALLOWED_CATEGORIES if c.lower() == "other"),
                    ALLOWED_CATEGORIES[0],
                )
            merchant = (expense.get("merchant") or "")[:128] or None
            description = expense.get("description") or None
            date_val = _parse_date(expense.get("date"))
            record = Expense(
                user_id=user_id,
                amount=float(amount),
                category=category,
                merchant=merchant,
                description=description,
                expense_date=date_val,
                raw_payload=str(expense),
            )
            db.add(record)
            saved += 1
        db.commit()
    return saved


def _parse_date(raw: Optional[str]) -> Optional[date]:
    if not raw:
        return None
    try:
        return datetime.fromisoformat(raw).date()
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
