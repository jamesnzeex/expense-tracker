import argparse
from typing import Optional

from app.database import SessionLocal, init_db
from app.datamodel import Expense, User


def list_users() -> None:
    with SessionLocal() as db:
        users = db.query(User).order_by(User.id).all()
        if not users:
            print("No users found.")
            return
        for u in users:
            print(f"User {u.id} | tg_id={u.telegram_id} | username={u.username} | created={u.created_at}")


def list_expenses(user_id: Optional[int], limit: int) -> None:
    with SessionLocal() as db:
        query = db.query(Expense).order_by(Expense.created_at.desc())
        if user_id is not None:
            query = query.filter(Expense.user_id == user_id)
        expenses = query.limit(limit).all()
        if not expenses:
            print("No expenses found.")
            return
        for e in expenses:
            print(
                f"Expense {e.id} | user={e.user_id} | {e.amount:.2f} SGD | "
                f"{e.category or 'Uncategorized'} | {e.merchant or ''} | "
                f"date={e.expense_date} | created={e.created_at}"
            )


def main() -> None:
    parser = argparse.ArgumentParser(description="Query expense tracker database.")
    parser.add_argument("--users", action="store_true", help="List all users.")
    parser.add_argument("--expenses", action="store_true", help="List expenses.")
    parser.add_argument("--user-id", type=int, help="Filter expenses by user id.")
    parser.add_argument("--limit", type=int, default=20, help="Limit number of expenses.")
    args = parser.parse_args()

    init_db()

    if args.users:
        list_users()
    if args.expenses:
        list_expenses(user_id=args.user_id, limit=args.limit)
    if not args.users and not args.expenses:
        parser.print_help()


if __name__ == "__main__":
    main()
