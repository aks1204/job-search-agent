"""Module 3 Part 3b: one outreach draft per role (sibling cluster). Only the greeting name changes
between recipients; approval and sending come in Part 3c."""

from datetime import datetime

from sqlalchemy import BigInteger, Boolean, DateTime, Float, ForeignKey, Integer, String, Text, func, text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base


class OutreachDraft(Base):
    __tablename__ = "outreach_drafts"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    cluster_id: Mapped[int] = mapped_column(ForeignKey("job_clusters.id", ondelete="CASCADE"), unique=True)
    version: Mapped[int] = mapped_column(Integer, default=1, server_default=text("1"))  # +1 on every rewrite
    scenario: Mapped[str] = mapped_column(String(24))      # fresh | multi_req | reopening | post_rejection | post_interview
    role_name: Mapped[str] = mapped_column(Text)
    subject: Mapped[str] = mapped_column(Text)
    body: Mapped[str] = mapped_column(Text)                 # final text; "{greeting}" filled per recipient
    intro: Mapped[str] = mapped_column(Text)
    points: Mapped[list] = mapped_column(JSONB, default=list, server_default=text("'[]'::jsonb"))
    req_ids: Mapped[list] = mapped_column(JSONB, default=list, server_default=text("'[]'::jsonb"))
    attach_resume: Mapped[bool] = mapped_column(Boolean, default=True, server_default=text("true"))
    status: Mapped[str] = mapped_column(String(10))         # ready | flagged (truth check couldn't fix it cleanly)
    truth: Mapped[dict] = mapped_column(JSONB, default=dict, server_default=text("'{}'::jsonb"))
    inputs_version: Mapped[str] = mapped_column(String(32))  # posting + resume + profile + template + scenario
    cost_usd: Mapped[float] = mapped_column(Float, default=0.0, server_default=text("0"))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())
