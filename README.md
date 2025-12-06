# Expense Tracker (LLM powered)

Minimal Telegram bot that ingests receipts/statements, sends them to a locally hosted Ollama model for JSON expense extraction, and stores results in SQLite.

## Prerequisites
- Python 3.10+
- Local Ollama instance running and a model downloaded (default `qwen3-vl:8b-instruct`; set `OLLAMA_MODEL` to override).
- Telegram bot token from BotFather.

## Setup
```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

Set environment variables (or create a `.env` file and export them):
- `TELEGRAM_BOT_TOKEN` – bot token from BotFather
- `OLLAMA_URL` – defaults to `http://localhost:11434`
- `OLLAMA_MODEL` – defaults to `qwen3-vl:8b-instruct`
- `DATABASE_URL` – defaults to `sqlite:///./expense_tracker.db`
- `STORAGE_DIR` – defaults to `./uploads`
- `GLOBAL_REGISTRATION_PASSWORD` – shared secret users must supply to register/login
- `ALLOWED_CATEGORIES` – comma-separated list; defaults to `Food,Transport,Groceries,Shopping,Bills,Entertainment,Travel,Health,Other`

## Run
```bash
python -m app.bot
```

## Docker
```bash
docker compose up --build
```
Configure variables in `.env` (see list above). Uploads and the SQLite DB are mounted to the host (`./uploads`, `./expense_tracker.db`).

## Commands
- `/start` – help text
- `/register <global_password>` – create an account bound to your Telegram user id
- `/login <global_password>` – login for the current session
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
With the virtualenv active:
```bash
python -m app.query --users
python -m app.query --expenses --limit 10
python -m app.query --expenses --user-id 1 --limit 5
```

## How it works
- Incoming files are saved to `STORAGE_DIR`.
- PDFs/text are parsed for text; images are base64-encoded for Ollama.
- A structured prompt asks Ollama for JSON `{ "expenses": [ ... ] }`.
- Parsed expenses are stored with amount, category, merchant, description, and date.
