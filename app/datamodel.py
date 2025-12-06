from datetime import datetime
from zoneinfo import ZoneInfo

from sqlalchemy import (
    Column,
    Date,
    DateTime,
    Float,
    ForeignKey,
    Integer,
    String,
    Text,
    Index,
)
from sqlalchemy.orm import relationship

from app.database import Base


class User(Base):
    __tablename__ = "users"

    id = Column(Integer, primary_key=True, index=True)
    telegram_id = Column(Integer, unique=True, index=True, nullable=False)
    username = Column(String(255))
    created_at = Column(
        DateTime,
        default=lambda: datetime.now(ZoneInfo("Asia/Singapore")),
        nullable=False,
    )

    expenses = relationship("Expense", back_populates="user")


class Expense(Base):
    __tablename__ = "expenses"

    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False, index=True)
    amount = Column(Float, nullable=False)
    category = Column(String(64))
    merchant = Column(String(128))
    description = Column(Text)
    expense_date = Column(Date)
    raw_payload = Column(Text)
    created_at = Column(
        DateTime,
        default=lambda: datetime.now(ZoneInfo("Asia/Singapore")),
        nullable=False,
    )

    user = relationship("User", back_populates="expenses")


Index(
    "idx_expenses_user_date",
    Expense.user_id,
    Expense.expense_date,
)
