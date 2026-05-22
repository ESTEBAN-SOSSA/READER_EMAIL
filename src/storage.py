"""Persistencia con SQLAlchemy sobre SQLite."""
from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

from sqlalchemy import (
    Boolean,
    DateTime,
    Integer,
    String,
    Text,
    create_engine,
    select,
)
from sqlalchemy.orm import (
    DeclarativeBase,
    Mapped,
    Session,
    mapped_column,
)


class Base(DeclarativeBase):
    pass


class QuoteRequestRow(Base):
    """Una solicitud de cotizacion entrante."""
    __tablename__ = "quote_requests"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    graph_id: Mapped[str] = mapped_column(String(512), unique=True, index=True)
    conversation_id: Mapped[str] = mapped_column(String(512), index=True, default="")
    mailbox: Mapped[str] = mapped_column(String(255), index=True)
    subject: Mapped[str] = mapped_column(String(1024))
    sender_email: Mapped[str] = mapped_column(String(320), index=True)
    sender_name: Mapped[str] = mapped_column(String(255), default="")
    received_at: Mapped[str] = mapped_column(String(64), default="")
    processed_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)

    # Resumen
    snippet: Mapped[str] = mapped_column(Text, default="")
    cantidades_json: Mapped[str] = mapped_column(Text, default="[]")
    adjuntos_json: Mapped[str] = mapped_column(Text, default="[]")
    num_items: Mapped[int] = mapped_column(Integer, default=0)

    # Estado de respuesta (al CLIENTE EXTERNO)
    was_replied: Mapped[bool] = mapped_column(Boolean, default=False, index=True)
    replied_at: Mapped[str | None] = mapped_column(String(64), nullable=True)
    external_client: Mapped[str | None] = mapped_column(String(320), nullable=True)
    is_forwarded_internal: Mapped[bool] = mapped_column(Boolean, default=False)

    @property
    def cantidades(self) -> list[str]:
        try:
            return json.loads(self.cantidades_json)
        except json.JSONDecodeError:
            return []

    @cantidades.setter
    def cantidades(self, value: list[str]) -> None:
        self.cantidades_json = json.dumps(value, ensure_ascii=False)

    @property
    def adjuntos(self) -> list[str]:
        try:
            return json.loads(self.adjuntos_json)
        except json.JSONDecodeError:
            return []

    @adjuntos.setter
    def adjuntos(self, value: list[str]) -> None:
        self.adjuntos_json = json.dumps(value, ensure_ascii=False)


def init_db(path: Path):
    path.parent.mkdir(parents=True, exist_ok=True)
    engine = create_engine(f"sqlite:///{path}", future=True)
    Base.metadata.create_all(engine)
    return engine


def upsert_quote(
    session: Session,
    graph_id: str,
) -> QuoteRequestRow | None:
    """Si ya existe la cotizacion (por graph_id), la retorna; sino None."""
    stmt = select(QuoteRequestRow).where(QuoteRequestRow.graph_id == graph_id)
    return session.execute(stmt).scalar_one_or_none()
