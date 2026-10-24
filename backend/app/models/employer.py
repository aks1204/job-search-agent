"""Websites of your current and past employers (owner rule: they are never watched or contacted).

Who your employers are always comes from your profile; this table only remembers each one's website once
it has been found (or set by you with `employers fix`)."""

from datetime import datetime

from sqlalchemy import BigInteger, DateTime, Integer, String, func, text
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base


class OwnerEmployer(Base):
    __tablename__ = "owner_employers"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    normalized: Mapped[str] = mapped_column(String(255), unique=True)   # normalize_name() of the employer
    name: Mapped[str] = mapped_column(String(255))                      # as written in your profile
    domain: Mapped[str | None] = mapped_column(String(253))
    source: Mapped[str] = mapped_column(String(12))                     # known | ai | manual | none | pending
    checked_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    attempts: Mapped[int] = mapped_column(Integer, default=0, server_default=text("0"))  # lookups without an answer
    told_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))  # you were asked to set it yourself
