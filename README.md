# Expense Tracker (LLM powered)

Minimal Telegram bot that ingests receipts/statements, sends them to a locally hosted vLLM server for JSON expense extraction, and stores results in SQLite.

## Prerequisites
- Python 3.10+
- Local vLLM server running on `http://localhost:8000` or reachable from Docker via `http://host.docker.internal:8000` with the model loaded (default `Qwen/Qwen3.6-35B-A3B-FP8`; set `VLLM_MODEL` to override).
- Telegram bot token from BotFather.

## Setup
```bash
uv sync
```

Set environment variables (or copy `.env.template` to `.env` and edit):
- `TELEGRAM_BOT_TOKEN` – bot token from BotFather
- `VLLM_URL` – defaults to `auto`; the app picks `localhost` when run directly and `host.docker.internal` when run in Docker, then falls back to the alternate loopback target if the first one cannot be reached
- `VLLM_MODEL` – defaults to `Qwen/Qwen3.6-35B-A3B-FP8`
- `DATE_LOOKBACK_MONTHS` – defaults to `6`; LLM-proposed dates older than this are sent to review
- `DATABASE_URL` – defaults to `sqlite:///./expense_tracker.db`
- `STORAGE_DIR` – defaults to `./uploads`
- `GLOBAL_REGISTRATION_PASSWORD` – shared secret users must supply to register/login
- `ALLOWED_CATEGORIES` – comma-separated list; defaults to `Food,Transport,Groceries,Shopping,Bills,Entertainment,Travel,Health,Other`

## Run
```bash
uv run python -m app.bot
```

## Docker
```bash
docker compose up --build
```
Configure variables in `.env` (see list above). Uploads and the SQLite DB are mounted to the host (`./uploads`, `./expense_tracker.db`).
If you want to pin the endpoint manually, set `VLLM_URL` to either `http://localhost:8000` or `http://host.docker.internal:8000`.

## Commands
- `/start` – help text
- `/register <global_password>` – create an account bound to your Telegram user id
- `/login <global_password>` – login for the current session
- `/thinking on|off|default` – set thinking mode for your next LLM requests
- `/me` – show session info plus your total expenses and last update time
- `/listexpense [count]` – list your last N expenses (default 5, max 500)
  - Each row includes `ID` for deletion or editing
- `/addexpense` – enter manual expenses (after command, send lines: `amount,date,category[,merchant][,description]`; category must be one of your allowed list; send `cancel` to stop)
- `/editexpense` – update expenses interactively via lines: `id,amount[,date][,category][,merchant][,description]`; category (if provided) must be allowed; send `cancel` to stop
- `/deleteexpense <expense_id>` – delete a single expense
- `/deletelast <count>` – delete your last N expenses (max 100)
- `/deletemonth <month> <year>` – delete expenses in a specific month/year
- `/deleteall` – delete all your expenses
- `/summary [month] [year]` – monthly totals (defaults to current month)
- `/summaryall` – totals for every month in the database


### Examples
Add expenses manually:
```
/addexpense  (format: amount,date,category[,merchant][,description])
12.50,2024-06-01,Food,McDonalds,Lunch
8,2024-06-02,Transport,Bus
```
Edit existing expenses (ID + amount required):
```
/editexpense  (format: id,amount[,date][,category][,merchant][,description])
1,15.20,2025-12-01,Food,McDonalds
2,8.20,2025-12-01,Food
```
Delete expenses:
```
/deletemonth 5 2024 (format: month, year)
/deleteexpense 42 (format: ID)
/deletelast 3
/deleteall
```
Summaries:
```
/summary 5 2024 (format: month, year)
/summaryall
```

Notes:
- Categories are restricted to `ALLOWED_CATEGORIES`; defaults: Food, Transport, Groceries, Shopping, Bills, Utilities, Entertainment, Travel, Health, Other.

## Query the DB
```bash
uv run python -m app.query --users
uv run python -m app.query --expenses --limit 10
uv run python -m app.query --expenses --user-id 1 --limit 5
```

## How it works
- Incoming files are saved to `STORAGE_DIR`.
- PDFs/text are parsed for text; images are base64-encoded and sent to vLLM as multimodal inputs.
- PDF and image uploads from the same user within the batching window are grouped into one request.
- A structured prompt asks the model for JSON `{ "expenses": [ ... ] }`.
- Thinking mode is controlled per session with `/thinking on|off|default`; when set, the bot sends `chat_template_kwargs={"enable_thinking": ...}` on the request.
- Date handling is deterministic: LLM dates are accepted only if they are not in the future and not older than `DATE_LOOKBACK_MONTHS`.
- If a batch has items needing review, reply with `edit 1 2025-03-10, use 2, skip 3` or `use all` / `skip all`. Commas and semicolons are both accepted.
- Parsed expenses are stored with amount, category, merchant, description, and date.
