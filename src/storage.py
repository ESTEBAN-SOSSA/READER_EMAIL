"""Persistencia con SQLAlchemy sobre SQLite."""
from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

from sqlalchemy import (
    Boolean,
    DateTime,
    ForeignKey,
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

    # Solicitud ORIGINAL: correo que inicio la cadena (escenario 1 - RE/RV)
    original_received_at: Mapped[str | None] = mapped_column(String(64), nullable=True)
    original_snippet: Mapped[str | None] = mapped_column(Text, nullable=True)
    original_source: Mapped[str | None] = mapped_column(String(20), nullable=True)

    # Agrupacion de cotizaciones relacionadas / consolidadas (escenario 2)
    doc_ref: Mapped[str | None] = mapped_column(String(64), nullable=True)
    group_key: Mapped[str | None] = mapped_column(String(255), index=True, nullable=True)

    # Relacion RE -> original del hilo (escenario REs)
    is_original: Mapped[bool] = mapped_column(Boolean, default=True)
    original_graph_id: Mapped[str | None] = mapped_column(String(512), index=True, nullable=True)
    chain_source: Mapped[str | None] = mapped_column(String(30), nullable=True)

    # Dedup cross-mailbox: RFC Message-ID es estable entre buzones (mismo correo
    # enviado a varios destinatarios tiene el mismo internet_message_id, mientras
    # que graph_id y conversation_id son por-buzon).
    internet_message_id: Mapped[str | None] = mapped_column(String(998), index=True, nullable=True)

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


class ProcessedMessage(Base):
    """Registro de correos ya procesados, para no reprocesarlos (escenario 3)."""
    __tablename__ = "processed_messages"

    graph_id: Mapped[str] = mapped_column(String(512), primary_key=True)
    mailbox: Mapped[str] = mapped_column(String(255), index=True, default="")
    subject: Mapped[str] = mapped_column(String(1024), default="")
    result: Mapped[str] = mapped_column(String(20), default="")  # identificado | descartado
    reason: Mapped[str] = mapped_column(String(255), default="")
    processed_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)


class QuoteRelatedMessage(Base):
    """Fase B: correo relacionado (RE/RV) a una cotizacion. Los originales viven
    en quote_requests (1 fila = 1 cotizacion); aqui viven los correos de la cadena
    que NO son la solicitud original (respuestas, reenvios, follow-ups)."""
    __tablename__ = "quote_related_messages"

    graph_id: Mapped[str] = mapped_column(String(512), primary_key=True)
    quote_request_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("quote_requests.id"), index=True
    )
    mailbox: Mapped[str] = mapped_column(String(255), index=True, default="")
    conversation_id: Mapped[str] = mapped_column(String(512), default="")
    subject: Mapped[str] = mapped_column(String(1024), default="")
    sender_email: Mapped[str] = mapped_column(String(320), default="")
    sender_name: Mapped[str] = mapped_column(String(255), default="")
    received_at: Mapped[str] = mapped_column(String(64), default="")
    snippet: Mapped[str] = mapped_column(Text, default="")
    is_forwarded_internal: Mapped[bool] = mapped_column(Boolean, default=False)
    processed_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    # Dedup cross-mailbox + tipo de relacion explicito
    internet_message_id: Mapped[str | None] = mapped_column(String(998), index=True, nullable=True)
    relation_type: Mapped[str | None] = mapped_column(String(30), nullable=True)


def init_db(path: Path):
    path.parent.mkdir(parents=True, exist_ok=True)
    engine = create_engine(f"sqlite:///{path}", future=True)
    Base.metadata.create_all(engine)
    _migrar_columnas(engine)
    _migrar_respuestas_a_subtabla(engine)
    _crear_vistas(engine)
    return engine


def _crear_vistas(engine) -> None:
    """Crea (o recrea) las vistas SQL para consulta directa con SQL.
    Se ejecutan en cada init_db para mantenerlas sincronizadas con el esquema.

    Vistas creadas:
      - v_quote_request_relations_count: 1 fila por cotizacion + columna
        related_count con la cantidad de correos relacionados (REs/RVs).
      - v_quote_request_relations: 1 fila por correo relacionado, JOIN con su
        cotizacion padre + columna tipo_relacionado (Respuesta/Reenvio/Mensaje)
        calculada del prefijo del asunto."""
    vistas = {
        "v_quote_request_relations_count": """
            SELECT
              qr.id                       AS quote_request_id,
              qr.graph_id                 AS quote_graph_id,
              qr.internet_message_id      AS internet_message_id,
              qr.mailbox                  AS mailbox,
              qr.subject                  AS subject,
              qr.sender_email             AS sender_email,
              qr.external_client          AS external_client,
              qr.received_at              AS received_at,
              qr.was_replied              AS was_replied,
              qr.replied_at               AS replied_at,
              qr.doc_ref                  AS doc_ref,
              qr.group_key                AS group_key,
              qr.chain_source             AS chain_source,
              COUNT(rel.graph_id)         AS related_count,
              SUM(CASE WHEN rel.relation_type = 'copia_buzon' THEN 1 ELSE 0 END)
                                          AS copias_cross_mailbox
            FROM quote_requests qr
            LEFT JOIN quote_related_messages rel ON rel.quote_request_id = qr.id
            GROUP BY qr.id
        """,
        "v_quote_request_relations": """
            SELECT
              qr.id                       AS quote_request_id,
              qr.graph_id                 AS quote_graph_id,
              qr.subject                  AS quote_subject,
              qr.external_client          AS quote_client,
              qr.received_at              AS quote_received_at,
              qr.was_replied              AS quote_was_replied,
              rel.graph_id                AS related_graph_id,
              rel.internet_message_id     AS related_internet_message_id,
              rel.mailbox                 AS related_mailbox,
              rel.conversation_id         AS related_conversation_id,
              rel.subject                 AS related_subject,
              rel.sender_email            AS related_sender_email,
              rel.sender_name             AS related_sender_name,
              rel.received_at             AS related_received_at,
              rel.snippet                 AS related_snippet,
              rel.is_forwarded_internal   AS related_is_forwarded_internal,
              rel.processed_at            AS related_processed_at,
              CASE
                WHEN rel.relation_type IS NOT NULL AND rel.relation_type != ''
                  THEN rel.relation_type
                WHEN LOWER(TRIM(rel.subject)) LIKE 'rv:%'
                  OR LOWER(TRIM(rel.subject)) LIKE 'fw:%'
                  OR LOWER(TRIM(rel.subject)) LIKE 'fwd:%' THEN 'reenvio'
                WHEN LOWER(TRIM(rel.subject)) LIKE 're:%'
                  OR LOWER(TRIM(rel.subject)) LIKE 'res:%'
                  OR LOWER(TRIM(rel.subject)) LIKE 'reply:%' THEN 'respuesta'
                WHEN rel.is_forwarded_internal = 1 THEN 'reenvio_interno'
                ELSE 'mensaje'
              END                         AS tipo_relacionado
            FROM quote_requests qr
            INNER JOIN quote_related_messages rel ON rel.quote_request_id = qr.id
        """,
    }
    with engine.begin() as conn:
        for nombre, sql in vistas.items():
            conn.exec_driver_sql(f"DROP VIEW IF EXISTS {nombre}")
            conn.exec_driver_sql(f"CREATE VIEW {nombre} AS {sql.strip()}")


def _migrar_columnas(engine) -> None:
    """Agrega columnas nuevas a tablas existentes (SQLite create_all no altera
    tablas con esquema previo). Preserva los datos. Idempotente."""
    cambios: dict[str, list[tuple[str, str]]] = {
        "quote_requests": [
            ("original_received_at", "VARCHAR(64)"),
            ("original_snippet", "TEXT"),
            ("original_source", "VARCHAR(20)"),
            ("doc_ref", "VARCHAR(64)"),
            ("group_key", "VARCHAR(255)"),
            ("is_original", "BOOLEAN DEFAULT 1"),
            ("original_graph_id", "VARCHAR(512)"),
            ("chain_source", "VARCHAR(30)"),
            ("internet_message_id", "VARCHAR(998)"),
        ],
        "quote_related_messages": [
            ("internet_message_id", "VARCHAR(998)"),
            ("relation_type", "VARCHAR(30)"),
        ],
    }
    with engine.begin() as conn:
        for tabla, cols in cambios.items():
            existentes = {
                row[1] for row in conn.exec_driver_sql(f"PRAGMA table_info({tabla})")
            }
            if not existentes:
                continue
            for nombre, tipo in cols:
                if nombre not in existentes:
                    conn.exec_driver_sql(
                        f"ALTER TABLE {tabla} ADD COLUMN {nombre} {tipo}"
                    )


def _migrar_respuestas_a_subtabla(engine) -> None:
    """Fase B: mueve filas de quote_requests con is_original=False hacia
    quote_related_messages, enlazandolas con su padre via original_graph_id.
    Idempotente: si no hay filas con is_original=False, no hace nada."""
    with Session(engine) as session:
        respuestas = session.execute(
            select(QuoteRequestRow).where(QuoteRequestRow.is_original.is_(False))
        ).scalars().all()
        if not respuestas:
            return
        for r in respuestas:
            padre = None
            if r.original_graph_id:
                padre = session.execute(
                    select(QuoteRequestRow).where(
                        QuoteRequestRow.graph_id == r.original_graph_id
                    )
                ).scalar_one_or_none()
            if padre is None:
                # Huerfana: dejarla donde esta hasta que se reprocese y resuelva
                continue
            existing = session.get(QuoteRelatedMessage, r.graph_id)
            if existing is None:
                rel = QuoteRelatedMessage(
                    graph_id=r.graph_id,
                    quote_request_id=padre.id,
                    mailbox=r.mailbox,
                    conversation_id=r.conversation_id,
                    subject=r.subject,
                    sender_email=r.sender_email,
                    sender_name=r.sender_name,
                    received_at=r.received_at,
                    snippet=r.snippet,
                    is_forwarded_internal=r.is_forwarded_internal,
                    processed_at=r.processed_at,
                )
                session.add(rel)
            session.delete(r)
        session.commit()


def upsert_quote(
    session: Session,
    graph_id: str,
) -> QuoteRequestRow | None:
    """Si ya existe la cotizacion (por graph_id), la retorna; sino None."""
    stmt = select(QuoteRequestRow).where(QuoteRequestRow.graph_id == graph_id)
    return session.execute(stmt).scalar_one_or_none()


def get_skip_ids(session: Session) -> set[str]:
    """IDs que NO se deben reprocesar (escenario 3): correos descartados +
    cotizaciones ya respondidas (estado final) + REs/RVs ya registradas en la
    sub-tabla (su estado lo controla el padre, no aportan info nueva al reevaluarse)."""
    descartados = session.execute(
        select(ProcessedMessage.graph_id).where(ProcessedMessage.result == "descartado")
    ).scalars().all()
    respondidas = session.execute(
        select(QuoteRequestRow.graph_id).where(QuoteRequestRow.was_replied.is_(True))
    ).scalars().all()
    relacionadas = session.execute(
        select(QuoteRelatedMessage.graph_id)
    ).scalars().all()
    return set(descartados) | set(respondidas) | set(relacionadas)


def mark_processed(
    session: Session,
    graph_id: str,
    mailbox: str,
    subject: str,
    result: str,
    reason: str = "",
) -> None:
    """Registra (o actualiza) un correo como procesado: identificado | descartado."""
    obj = session.get(ProcessedMessage, graph_id)
    if obj is None:
        obj = ProcessedMessage(graph_id=graph_id)
        session.add(obj)
    obj.mailbox = mailbox
    obj.subject = subject
    obj.result = result
    obj.reason = reason
    obj.processed_at = datetime.utcnow()
    session.commit()


def upsert_related(
    session: Session,
    msg,
    quote_request_id: int,
    relation_type: str | None = None,
) -> bool:
    """Fase B: inserta/actualiza un correo en quote_related_messages enlazado al
    padre. relation_type opcional ('respuesta' | 'reenvio' | 'copia_buzon').
    Si es None, la vista SQL lo computa por prefijo de asunto."""
    existing = session.get(QuoteRelatedMessage, msg.id)
    es_nueva = existing is None
    if es_nueva:
        existing = QuoteRelatedMessage(graph_id=msg.id)
        session.add(existing)
    existing.quote_request_id = quote_request_id
    existing.mailbox = msg.mailbox
    existing.conversation_id = msg.conversation_id
    existing.subject = msg.subject
    existing.sender_email = msg.sender_email
    existing.sender_name = msg.sender_name
    existing.received_at = msg.received_at
    existing.snippet = (msg.body_preview or (msg.body_text or "")[:300])
    existing.is_forwarded_internal = msg.is_forwarded_internal
    existing.internet_message_id = msg.internet_message_id
    if relation_type:
        existing.relation_type = relation_type
    existing.processed_at = datetime.utcnow()
    session.commit()
    return es_nueva


def find_by_imid(
    session: Session, internet_message_id: str | None
) -> int | None:
    """Busca un internet_message_id (RFC Message-ID) en ambas tablas y devuelve
    el quote_request_id (cotizacion padre) si lo encuentra. Sirve para detectar
    duplicados cross-mailbox del mismo correo recibido por varios buzones."""
    if not internet_message_id:
        return None
    qr_id = session.execute(
        select(QuoteRequestRow.id).where(
            QuoteRequestRow.internet_message_id == internet_message_id
        )
    ).scalar_one_or_none()
    if qr_id is not None:
        return qr_id
    return session.execute(
        select(QuoteRelatedMessage.quote_request_id).where(
            QuoteRelatedMessage.internet_message_id == internet_message_id
        )
    ).scalar_one_or_none()
