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


class PendingDateReviewSession(Base):
    __tablename__ = "pending_date_review_sessions"

    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False, index=True)
    chat_id = Column(Integer, nullable=False, index=True)
    status = Column(String(16), default="open", nullable=False, index=True)
    created_at = Column(
        DateTime,
        default=lambda: datetime.now(ZoneInfo("Asia/Singapore")),
        nullable=False,
    )
    updated_at = Column(
        DateTime,
        default=lambda: datetime.now(ZoneInfo("Asia/Singapore")),
        onupdate=lambda: datetime.now(ZoneInfo("Asia/Singapore")),
        nullable=False,
    )

    user = relationship("User")
    items = relationship(
        "PendingDateReviewItem",
        back_populates="session",
        cascade="all, delete-orphan",
    )


class PendingDateReviewItem(Base):
    __tablename__ = "pending_date_review_items"

    id = Column(Integer, primary_key=True, index=True)
    session_id = Column(
        Integer, ForeignKey("pending_date_review_sessions.id"), nullable=False, index=True
    )
    item_no = Column(Integer, nullable=False)
    source_name = Column(String(255))
    amount = Column(Float, nullable=False)
    category = Column(String(64))
    merchant = Column(String(128))
    description = Column(Text)
    llm_date_raw = Column(String(64))
    llm_date_parsed = Column(Date)
    reason = Column(Text)
    status = Column(String(16), default="pending", nullable=False, index=True)
    resolved_date = Column(Date)
    raw_payload = Column(Text, nullable=False)
    created_at = Column(
        DateTime,
        default=lambda: datetime.now(ZoneInfo("Asia/Singapore")),
        nullable=False,
    )
    updated_at = Column(
        DateTime,
        default=lambda: datetime.now(ZoneInfo("Asia/Singapore")),
        onupdate=lambda: datetime.now(ZoneInfo("Asia/Singapore")),
        nullable=False,
    )

    session = relationship("PendingDateReviewSession", back_populates="items")
