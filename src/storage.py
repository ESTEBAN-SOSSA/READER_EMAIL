"""Persistencia con SQLAlchemy sobre SQLite."""
from __future__ import annotations

import json
import re
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

    # Agrupacion de cotizaciones relacionadas / consolidadas (escenario 2).
    # Puede contener VARIAS referencias separadas por ';' (un correo trae 2 ordenes);
    # el cruce reenvio<->original se hace por interseccion de esos conjuntos.
    doc_ref: Mapped[str | None] = mapped_column(String(200), nullable=True)
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


class ClaudeReview(Base):
    """Veredicto semantico de Claude para un correo + decision humana.
    Los correos que pasan los filtros determinsticos pasan por aqui antes de
    entrar a quote_requests (cuando USE_CLAUDE_CLASSIFIER esta activo)."""
    __tablename__ = "claude_reviews"

    graph_id: Mapped[str] = mapped_column(String(512), primary_key=True)
    mailbox: Mapped[str] = mapped_column(String(255), index=True, default="")
    subject: Mapped[str] = mapped_column(String(1024), default="")
    sender_email: Mapped[str] = mapped_column(String(320), default="")
    received_at: Mapped[str] = mapped_column(String(64), default="")
    internet_message_id: Mapped[str | None] = mapped_column(String(998), index=True, nullable=True)

    # Mensaje completo serializado para reconstruirlo al aprobar
    message_json: Mapped[str] = mapped_column(Text, default="{}")

    # Veredicto de Claude
    es_cotizacion_cliente: Mapped[bool] = mapped_column(Boolean, default=False)
    tipo: Mapped[str] = mapped_column(String(50), default="otro")
    confianza: Mapped[float] = mapped_column(default=0.0)
    razonamiento_si: Mapped[str] = mapped_column(Text, default="")
    razonamiento_no: Mapped[str] = mapped_column(Text, default="")
    veredicto_final: Mapped[str] = mapped_column(Text, default="")
    reglas_sugeridas_json: Mapped[str] = mapped_column(Text, default="[]")

    # Decision humana
    estado: Mapped[str] = mapped_column(String(20), index=True, default="pendiente")  # pendiente | aprobado | rechazado
    human_decision_es_cotizacion: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    human_motivo: Mapped[str | None] = mapped_column(Text, nullable=True)
    reviewed_by: Mapped[str | None] = mapped_column(String(120), nullable=True)
    reviewed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    # Contexto/pista que el humano le pasa a Claude antes de decidir (puede
    # disparar un reanalisis con esta informacion adicional).
    contexto_humano: Mapped[str | None] = mapped_column(Text, nullable=True)

    # Dedup LOGICO: agrupa correos lógicamente equivalentes (mismo asunto
    # normalizado + remitente) aunque tengan Message-ID/conversation distintos
    # (p.ej. un despachador reenvia 10 veces la misma petición de un portal).
    # Solo el REPRESENTANTE del grupo se clasifica y se muestra al humano; las
    # copias quedan como estado='copia_logica' apuntando al representante y
    # heredan su decision (rechazo => ruido; aprobacion => se consolidan).
    group_logico: Mapped[str | None] = mapped_column(String(700), index=True, nullable=True)
    representante_graph_id: Mapped[str | None] = mapped_column(String(512), index=True, nullable=True)

    classified_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)


class LearnedRule(Base):
    """Regla aprendida y aprobada por un humano a partir de sugerencia de Claude.
    Se aplica en los filtros determinsticos en cada nueva corrida del run."""
    __tablename__ = "learned_rules"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    tipo: Mapped[str] = mapped_column(String(40), index=True)
    valor: Mapped[str] = mapped_column(String(500))
    razon: Mapped[str] = mapped_column(Text, default="")
    fuente_graph_id: Mapped[str | None] = mapped_column(String(512), nullable=True)
    aprobada_por: Mapped[str] = mapped_column(String(120), default="UI")
    aprobada_en: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    activa: Mapped[bool] = mapped_column(Boolean, default=True, index=True)


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


class ReenvioLinkCandidato(Base):
    """Cola de validacion MANUAL para enlaces reenvio<->original que NO tienen un
    numero de referencia compartido y se proponen solo por asunto normalizado (mas
    riesgo de falso positivo). El humano confirma o descarta; los enlaces por
    referencia se aplican solos y NO pasan por aqui."""
    __tablename__ = "reenvio_link_candidatos"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    # Reenvio interno del analista (huerfano, sin cliente externo)
    rv_quote_id: Mapped[int] = mapped_column(Integer, index=True)
    rv_mailbox: Mapped[str] = mapped_column(String(255), default="")
    rv_subject: Mapped[str] = mapped_column(String(1024), default="")
    # Original del despachador (candidato, trae el cliente externo)
    orig_quote_id: Mapped[int] = mapped_column(Integer, index=True)
    orig_mailbox: Mapped[str] = mapped_column(String(255), default="")
    orig_subject: Mapped[str] = mapped_column(String(1024), default="")
    orig_external_client: Mapped[str | None] = mapped_column(String(320), nullable=True)
    asunto_normalizado: Mapped[str] = mapped_column(String(700), default="")
    estado: Mapped[str] = mapped_column(String(20), index=True, default="pendiente")  # pendiente | aprobado | descartado
    creado_en: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)


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
        "claude_reviews": [
            ("contexto_humano", "TEXT"),
            ("group_logico", "VARCHAR(700)"),
            ("representante_graph_id", "VARCHAR(512)"),
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


def serialize_message(msg) -> str:
    """Serializa un Message a JSON para almacenarlo en claude_reviews y poder
    reconstruirlo al momento de la aprobacion humana."""
    data = {
        "id": msg.id,
        "mailbox": msg.mailbox,
        "conversation_id": msg.conversation_id,
        "internet_message_id": msg.internet_message_id,
        "subject": msg.subject,
        "sender_email": msg.sender_email,
        "sender_name": msg.sender_name,
        "received_at": msg.received_at,
        "body_preview": msg.body_preview,
        "body_text": msg.body_text,
        "has_attachments": msg.has_attachments,
        "attachments": [
            {"id": a.id, "name": a.name, "content_type": a.content_type,
             "size": a.size, "local_path": str(a.local_path)}
            for a in msg.attachments
        ],
        "was_replied": msg.was_replied,
        "replied_at": msg.replied_at,
        "external_client": msg.external_client,
        "is_forwarded_internal": msg.is_forwarded_internal,
        "original_received_at": msg.original_received_at,
        "original_snippet": msg.original_snippet,
        "original_source": msg.original_source,
        "doc_ref": msg.doc_ref,
        "group_key": msg.group_key,
        "is_original": msg.is_original,
        "original_graph_id": msg.original_graph_id,
        "chain_source": msg.chain_source,
    }
    return json.dumps(data, ensure_ascii=False)


def deserialize_message(blob: str):
    """Reconstruye un Message desde el JSON guardado en claude_reviews."""
    from pathlib import Path
    from .mail_reader import Message, Attachment
    d = json.loads(blob)
    attachments = [
        Attachment(
            id=a["id"], name=a["name"], content_type=a["content_type"],
            size=a["size"], local_path=Path(a["local_path"]),
        )
        for a in d.get("attachments", [])
    ]
    return Message(
        id=d["id"],
        mailbox=d["mailbox"],
        conversation_id=d["conversation_id"],
        internet_message_id=d.get("internet_message_id") or "",
        subject=d["subject"],
        sender_email=d["sender_email"],
        sender_name=d["sender_name"],
        received_at=d["received_at"],
        body_preview=d.get("body_preview", ""),
        body_text=d.get("body_text", ""),
        has_attachments=d.get("has_attachments", False),
        attachments=attachments,
        was_replied=d.get("was_replied", False),
        replied_at=d.get("replied_at"),
        external_client=d.get("external_client"),
        is_forwarded_internal=d.get("is_forwarded_internal", False),
        original_received_at=d.get("original_received_at"),
        original_snippet=d.get("original_snippet"),
        original_source=d.get("original_source"),
        doc_ref=d.get("doc_ref"),
        group_key=d.get("group_key"),
        is_original=d.get("is_original", True),
        original_graph_id=d.get("original_graph_id"),
        chain_source=d.get("chain_source", "directo"),
    )


def upsert_claude_review(session: Session, msg, veredicto) -> ClaudeReview:
    """Inserta/actualiza el veredicto de Claude para un correo. Si ya hay un
    veredicto previo en estado 'pendiente', lo sobrescribe; si esta aprobado/rechazado,
    no se toca."""
    existing = session.get(ClaudeReview, msg.id)
    if existing is not None and existing.estado != "pendiente":
        return existing
    if existing is None:
        existing = ClaudeReview(graph_id=msg.id)
        session.add(existing)
    existing.mailbox = msg.mailbox
    existing.subject = msg.subject
    existing.sender_email = msg.sender_email
    existing.received_at = msg.received_at
    existing.internet_message_id = msg.internet_message_id
    existing.message_json = serialize_message(msg)
    existing.es_cotizacion_cliente = bool(veredicto.es_cotizacion_cliente)
    existing.tipo = veredicto.tipo
    existing.confianza = float(veredicto.confianza)
    existing.razonamiento_si = veredicto.razonamiento_si
    existing.razonamiento_no = veredicto.razonamiento_no
    existing.veredicto_final = veredicto.veredicto_final
    existing.reglas_sugeridas_json = json.dumps(
        [r.model_dump() for r in veredicto.reglas_sugeridas], ensure_ascii=False
    )
    existing.estado = "pendiente"
    existing.group_logico = clave_grupo_logico(msg.subject, msg.sender_email)
    existing.classified_at = datetime.utcnow()
    session.commit()
    return existing


def update_claude_review_con_contexto(
    session: Session, msg, veredicto, contexto_humano: str
) -> ClaudeReview:
    """Actualiza un review existente con un nuevo veredicto que tuvo en cuenta
    un contexto humano. Lo deja como 'pendiente' (humano aun debe aprobar)."""
    review = upsert_claude_review(session, msg, veredicto)
    review.contexto_humano = contexto_humano
    session.commit()
    return review


def get_pending_reviews(session: Session, limit: int = 100) -> list[ClaudeReview]:
    """Lista los reviews que esperan decision humana."""
    return session.execute(
        select(ClaudeReview)
        .where(ClaudeReview.estado == "pendiente")
        .order_by(ClaudeReview.classified_at)
        .limit(limit)
    ).scalars().all()


def record_human_decision(
    session: Session,
    graph_id: str,
    es_cotizacion: bool,
    motivo: str,
    reviewed_by: str = "UI",
) -> ClaudeReview | None:
    """Marca el review como aprobado o rechazado por un humano."""
    review = session.get(ClaudeReview, graph_id)
    if review is None:
        return None
    review.human_decision_es_cotizacion = es_cotizacion
    review.human_motivo = motivo or ""
    review.reviewed_by = reviewed_by
    review.reviewed_at = datetime.utcnow()
    review.estado = "aprobado" if es_cotizacion else "rechazado"
    session.commit()
    return review


def add_learned_rule(
    session: Session,
    tipo: str,
    valor: str,
    razon: str,
    fuente_graph_id: str | None,
    aprobada_por: str = "UI",
) -> LearnedRule:
    """Registra una regla aprendida (aprobada por humano a partir de sugerencia de Claude)."""
    existing = session.execute(
        select(LearnedRule).where(
            LearnedRule.tipo == tipo,
            LearnedRule.valor == valor,
            LearnedRule.activa.is_(True),
        )
    ).scalar_one_or_none()
    if existing is not None:
        return existing
    rule = LearnedRule(
        tipo=tipo,
        valor=valor,
        razon=razon,
        fuente_graph_id=fuente_graph_id,
        aprobada_por=aprobada_por,
        aprobada_en=datetime.utcnow(),
        activa=True,
    )
    session.add(rule)
    session.commit()
    return rule


def get_active_learned_rules(session: Session) -> list[LearnedRule]:
    return session.execute(
        select(LearnedRule).where(LearnedRule.activa.is_(True))
    ).scalars().all()


def apply_learned_rules_to_settings(session: Session, settings) -> None:
    """Mezcla las reglas aprendidas activas con las del settings.yaml in-place.
    Los tipos 'supplier_email' y 'supplier_phrase' se traducen a las listas de
    exclusion existentes (no requieren columna nueva)."""
    for rule in get_active_learned_rules(session):
        if rule.tipo == "keyword":
            if rule.valor not in settings.keywords:
                settings.keywords.append(rule.valor)
        elif rule.tipo == "exclude_subject_contains":
            if rule.valor not in settings.exclude_subject_contains:
                settings.exclude_subject_contains.append(rule.valor)
        elif rule.tipo == "exclude_body_contains":
            if rule.valor not in settings.exclude_body_contains:
                settings.exclude_body_contains.append(rule.valor)
        elif rule.tipo in ("exclude_sender_domain", "exclude_sender_email", "supplier_email"):
            if rule.valor not in settings.exclude_sender_domains:
                settings.exclude_sender_domains.append(rule.valor)
        elif rule.tipo == "supplier_phrase":
            if rule.valor not in settings.exclude_body_contains:
                settings.exclude_body_contains.append(rule.valor)


def get_recent_human_examples(session: Session, limit: int = 12) -> list[dict]:
    """Decisiones humanas recientes para inyectarlas como ejemplos few-shot en el
    system prompt de Claude (cierra el loop de aprendizaje)."""
    rows = session.execute(
        select(ClaudeReview)
        .where(ClaudeReview.estado.in_(["aprobado", "rechazado"]))
        .order_by(ClaudeReview.reviewed_at.desc())
        .limit(limit)
    ).scalars().all()
    return [
        {
            "subject": r.subject,
            "sender_email": r.sender_email,
            "decision": "ES cotizacion" if r.human_decision_es_cotizacion else "NO es cotizacion",
            "motivo": (r.human_motivo or "").strip()[:200],
        }
        for r in rows
    ]


def find_by_conversation(
    session: Session,
    mailbox: str,
    conversation_id: str,
    exclude_graph_id: str | None = None,
) -> int | None:
    """Busca una cotizacion existente en el MISMO buzon y mismo hilo
    (conversation_id) y devuelve su quote_request_id (la mas antigua si hay varias).

    Sirve para deduplicar el caso en que el mismo hilo de cotizacion es reenviado
    por varios colaboradores internos al mismo buzon: cada reenvio tiene un
    internet_message_id distinto (por eso find_by_imid no lo atrapa), pero todos
    comparten el conversation_id. El primero queda como la cotizacion (original) y
    los demas se guardan como quote_related_messages (relation_type='copia_hilo')."""
    if not conversation_id:
        return None
    stmt = select(QuoteRequestRow.id).where(
        QuoteRequestRow.mailbox == mailbox,
        QuoteRequestRow.conversation_id == conversation_id,
    )
    if exclude_graph_id:
        stmt = stmt.where(QuoteRequestRow.graph_id != exclude_graph_id)
    return session.execute(
        stmt.order_by(QuoteRequestRow.received_at).limit(1)
    ).scalar_one_or_none()


def merge_duplicate_threads(session: Session, dry_run: bool = False) -> list[dict]:
    """Fusiona quote_requests que comparten (mailbox, conversation_id): conserva la
    mas antigua como cotizacion (original) y mueve las demas a
    quote_related_messages (relation_type='copia_hilo'). Idempotente: si cada hilo
    ya tiene una sola cotizacion, no hace nada. Devuelve el plan de fusiones.

    Util para purgar duplicados legacy creados antes de activar el dedup por hilo
    (mismo hilo reenviado por varios colaboradores internos al mismo buzon)."""
    from collections import defaultdict

    rows = session.execute(
        select(QuoteRequestRow).order_by(QuoteRequestRow.received_at)
    ).scalars().all()
    grupos: dict[tuple[str, str], list[QuoteRequestRow]] = defaultdict(list)
    for r in rows:
        if r.conversation_id:
            grupos[(r.mailbox, r.conversation_id)].append(r)

    plan: list[dict] = []
    for (mailbox, conv), miembros in grupos.items():
        if len(miembros) < 2:
            continue
        miembros.sort(key=lambda x: x.received_at or "")
        canonical = miembros[0]
        for dup in miembros[1:]:
            plan.append({
                "mailbox": mailbox,
                "conversation_id": conv,
                "canonical_id": canonical.id,
                "dup_id": dup.id,
                "dup_subject": dup.subject,
                "dup_was_replied": dup.was_replied,
            })
            if dry_run:
                continue
            # Propagar estado respondido al canonical (el original manda)
            if dup.was_replied and not canonical.was_replied:
                canonical.was_replied = True
                canonical.replied_at = dup.replied_at or canonical.replied_at
                canonical.external_client = (
                    canonical.external_client or dup.external_client
                )
            # Re-apuntar relacionados que colgaban del duplicado hacia el canonical
            for child in session.execute(
                select(QuoteRelatedMessage).where(
                    QuoteRelatedMessage.quote_request_id == dup.id
                )
            ).scalars().all():
                child.quote_request_id = canonical.id
            # Guardar el duplicado como correo relacionado del canonical
            if session.get(QuoteRelatedMessage, dup.graph_id) is None:
                session.add(QuoteRelatedMessage(
                    graph_id=dup.graph_id,
                    quote_request_id=canonical.id,
                    mailbox=dup.mailbox,
                    conversation_id=dup.conversation_id,
                    subject=dup.subject,
                    sender_email=dup.sender_email,
                    sender_name=dup.sender_name,
                    received_at=dup.received_at,
                    snippet=dup.snippet,
                    is_forwarded_internal=dup.is_forwarded_internal,
                    internet_message_id=dup.internet_message_id,
                    relation_type="copia_hilo",
                ))
            session.delete(dup)
    if not dry_run:
        session.commit()
    return plan


def referencias_de(doc_ref: str | None) -> set[str]:
    """Conjunto de referencias de un doc_ref serializado ('6000149870;COL-7087')."""
    if not doc_ref:
        return set()
    return {r.strip() for r in doc_ref.split(";") if r.strip()}


def backfill_doc_ref(session: Session) -> int:
    """Recalcula doc_ref para TODAS las quote_requests con el extractor de referencias
    vigente (asunto + snippet). Necesario porque las filas viejas se guardaron con un
    regex anterior que casi nunca casaba, dejando doc_ref nulo y haciendo imposible el
    cruce por referencia. Idempotente. Devuelve cuantas filas cambiaron."""
    from .mail_reader import extraer_referencias  # import perezoso (evita ciclo)

    cambiadas = 0
    for r in session.execute(select(QuoteRequestRow)).scalars().all():
        refs = extraer_referencias(r.subject or "", r.snippet or "")
        nuevo = ";".join(sorted(refs)) if refs else None
        if nuevo != r.doc_ref:
            r.doc_ref = nuevo
            cambiadas += 1
    if cambiadas:
        session.commit()
    return cambiadas


def _demote_a_relacionado(
    session: Session, dup: QuoteRequestRow, canonical: QuoteRequestRow,
    relation_type: str,
) -> None:
    """Baja una cotizacion duplicada a quote_related_messages colgando del canonical.
    Propaga al canonical el cliente externo y el estado de respuesta si este los tiene
    y el canonical no."""
    if not (canonical.external_client or "").strip() and (dup.external_client or "").strip():
        canonical.external_client = dup.external_client
    if dup.was_replied and not canonical.was_replied:
        canonical.was_replied = True
        canonical.replied_at = dup.replied_at or canonical.replied_at
    # Re-apuntar lo que colgaba del duplicado hacia el canonical
    for child in session.execute(
        select(QuoteRelatedMessage).where(
            QuoteRelatedMessage.quote_request_id == dup.id
        )
    ).scalars().all():
        child.quote_request_id = canonical.id
    if session.get(QuoteRelatedMessage, dup.graph_id) is None:
        session.add(QuoteRelatedMessage(
            graph_id=dup.graph_id,
            quote_request_id=canonical.id,
            mailbox=dup.mailbox,
            conversation_id=dup.conversation_id,
            subject=dup.subject,
            sender_email=dup.sender_email,
            sender_name=dup.sender_name,
            received_at=dup.received_at,
            snippet=dup.snippet,
            is_forwarded_internal=dup.is_forwarded_internal,
            internet_message_id=dup.internet_message_id,
            relation_type=relation_type,
        ))
    session.delete(dup)


def _rank_canonical(r: QuoteRequestRow) -> tuple:
    """Orden de preferencia para elegir el canonical de un cluster: el correo
    ORIGINAL del cliente (con cliente externo y NO reenviado) es el mejor; luego
    cualquiera con cliente; luego un no-reenvio; desempata el mas antiguo."""
    tiene_cliente = bool((r.external_client or "").strip())
    es_reenvio = bool(r.is_forwarded_internal)
    if tiene_cliente and not es_reenvio:
        pref = 0
    elif tiene_cliente:
        pref = 1
    elif not es_reenvio:
        pref = 2
    else:
        pref = 3
    return (pref, r.received_at or "")


def enlazar_reenvios_internos(
    session: Session, dry_run: bool = False
) -> dict:
    """Consolidacion de cotizaciones DUPLICADAS (el mismo RFQ que llega varias veces).

    Cubre la duplicacion CROSS-REMITENTE y CROSS-ANALISTA que copia_logica (que keya en
    asunto+remitente) no ve: el mismo pedido entra desde el cliente directo, desde el
    despachador A y desde el despachador B, con remitentes y conversation_id distintos.

    Estrategia:
      - Agrupa todas las quote_requests por NUMERO DE REFERENCIA compartido (doc_ref).
        Cada cluster con >1 fila es el mismo RFQ duplicado: se conserva el canonical
        (el original del cliente; ver _rank_canonical) y las demas bajan a
        quote_related_messages ('reenvio_interno' si venian reenviadas, si no
        'copia_referencia'). Alta confianza -> se aplica solo.
      - Los reenvios HUERFANOS (sin cliente, sin referencia que case) se intentan casar
        por ASUNTO normalizado contra una cotizacion con cliente; eso va a la cola de
        validacion MANUAL (riesgo de falso positivo), no se aplica.

    Idempotente. Devuelve {'auto': [...], 'manual': [...]}.
    """
    from collections import defaultdict

    # Asegura que doc_ref este actualizado con el extractor vigente antes de cruzar.
    backfill_doc_ref(session)

    rows = session.execute(select(QuoteRequestRow)).scalars().all()

    # --- 1) Clusters por referencia compartida (auto) ---
    # Une rows que comparten >=1 referencia (transitivamente: A-B por ref X, B-C por
    # ref Y => {A,B,C}) via union-find sobre las referencias.
    padre: dict[int, int] = {r.id: r.id for r in rows}
    def find(x):
        while padre[x] != x:
            padre[x] = padre[padre[x]]
            x = padre[x]
        return x
    def union(a, b):
        padre[find(a)] = find(b)
    ref_a_row: dict[str, int] = {}
    for r in rows:
        for ref in referencias_de(r.doc_ref):
            if ref in ref_a_row:
                union(r.id, ref_a_row[ref])
            else:
                ref_a_row[ref] = r.id
    clusters: dict[int, list[QuoteRequestRow]] = defaultdict(list)
    for r in rows:
        if referencias_de(r.doc_ref):
            clusters[find(r.id)].append(r)

    auto: list[dict] = []
    consolidados: set[int] = set()
    for miembros in clusters.values():
        if len(miembros) < 2:
            continue
        miembros.sort(key=_rank_canonical)
        canonical = miembros[0]
        for dup in miembros[1:]:
            rel = "reenvio_interno" if dup.is_forwarded_internal else "copia_referencia"
            auto.append({
                "orig_id": canonical.id, "orig_mailbox": canonical.mailbox,
                "dup_id": dup.id, "dup_mailbox": dup.mailbox,
                "cliente": canonical.external_client or dup.external_client,
                "ref": sorted(referencias_de(dup.doc_ref) & referencias_de(canonical.doc_ref))
                       or sorted(referencias_de(dup.doc_ref)),
                "rel": rel,
            })
            consolidados.add(dup.id)
            if not dry_run:
                _demote_a_relacionado(session, dup, canonical, rel)

    # --- 2) Huerfanos sin referencia: match por asunto -> cola manual ---
    ya_en_cola = {
        (c.rv_quote_id, c.orig_quote_id)
        for c in session.execute(
            select(ReenvioLinkCandidato).where(
                ReenvioLinkCandidato.estado == "pendiente"
            )
        ).scalars().all()
    }
    con_cliente = [r for r in rows if (r.external_client or "").strip()]
    manual: list[dict] = []
    for rv in rows:
        if rv.id in consolidados:
            continue
        if not rv.is_forwarded_internal or (rv.external_client or "").strip():
            continue
        if referencias_de(rv.doc_ref):
            continue  # tenia ref pero no caso: no forzar por asunto
        asunto_rv = _normalizar_asunto(rv.subject)
        if len(asunto_rv) < 8:
            continue
        for orig in con_cliente:
            if orig.id == rv.id:
                continue
            if _normalizar_asunto(orig.subject) == asunto_rv:
                if (rv.id, orig.id) in ya_en_cola:
                    break
                manual.append({
                    "rv_id": rv.id, "rv_subject": rv.subject, "rv_mailbox": rv.mailbox,
                    "orig_id": orig.id, "orig_mailbox": orig.mailbox,
                    "cliente": orig.external_client, "asunto": asunto_rv,
                })
                if not dry_run:
                    session.add(ReenvioLinkCandidato(
                        rv_quote_id=rv.id, rv_mailbox=rv.mailbox, rv_subject=rv.subject,
                        orig_quote_id=orig.id, orig_mailbox=orig.mailbox,
                        orig_subject=orig.subject, orig_external_client=orig.external_client,
                        asunto_normalizado=asunto_rv,
                    ))
                break

    if not dry_run:
        session.commit()
    return {"auto": auto, "manual": manual}


def find_claude_review_by_reference(
    session: Session, refs: set[str] | list[str]
) -> ClaudeReview | None:
    """Representante de staging por NUMERO DE REFERENCIA: el review mas antiguo (que no
    es copia) cuyo asunto comparte alguna referencia con `refs`. Sirve para deduplicar
    EN EL STAGING el mismo RFQ que llega de remitentes distintos (cliente directo +
    varios despachadores) — caso que copia_logica (asunto+remitente) no ve."""
    from .mail_reader import extraer_referencias  # import perezoso

    refs = set(refs)
    if not refs:
        return None
    candidatos = session.execute(
        select(ClaudeReview)
        .where(ClaudeReview.estado != "copia_logica")
        .order_by(ClaudeReview.classified_at)
    ).scalars().all()
    for rev in candidatos:
        if refs & set(extraer_referencias(rev.subject or "", "")):
            return rev
    return None


def consolidar_staging_por_referencia(session: Session) -> int:
    """Limpieza: colapsa en la cola de revision (claude_reviews) los duplicados del
    mismo RFQ que comparten numero de referencia pero llegaron de remitentes distintos
    (lo que copia_logica no agrupo). Conserva un representante por referencia (uno ya
    decidido si existe, si no el mas antiguo pendiente) y marca los demas PENDIENTES como
    copia (heredan la decision del representante). Devuelve cuantos colapso. Idempotente."""
    from collections import defaultdict
    from .mail_reader import extraer_referencias

    reviews = session.execute(
        select(ClaudeReview).where(ClaudeReview.estado != "copia_logica")
    ).scalars().all()

    # Union-find por referencia compartida (transitivo)
    padre: dict[str, str] = {r.graph_id: r.graph_id for r in reviews}
    def find(x):
        while padre[x] != x:
            padre[x] = padre[padre[x]]
            x = padre[x]
        return x
    ref_a_gid: dict[str, str] = {}
    refs_de: dict[str, set[str]] = {}
    for r in reviews:
        rs = set(extraer_referencias(r.subject or "", ""))
        refs_de[r.graph_id] = rs
        for ref in rs:
            if ref in ref_a_gid:
                padre[find(r.graph_id)] = find(ref_a_gid[ref])
            else:
                ref_a_gid[ref] = r.graph_id

    grupos: dict[str, list[ClaudeReview]] = defaultdict(list)
    for r in reviews:
        if refs_de[r.graph_id]:
            grupos[find(r.graph_id)].append(r)

    colapsados = 0
    for miembros in grupos.values():
        if len(miembros) < 2:
            continue
        # Representante: uno ya decidido si existe; si no, el mas antiguo pendiente.
        miembros.sort(key=lambda r: (
            0 if r.estado in ("aprobado", "rechazado") else 1,
            r.classified_at or datetime.min,
        ))
        rep = miembros[0]
        for r in miembros[1:]:
            if r.estado in ("aprobado", "rechazado", "copia_logica"):
                continue  # no tocar decididos ni copias ya marcadas
            msg = deserialize_message(r.message_json)
            registrar_copia_logica(session, msg, rep)
            colapsados += 1
    if colapsados:
        session.commit()
    return colapsados


def aprobar_enlace_candidato(session: Session, candidato_id: int) -> bool:
    """Aplica un enlace de la cola manual: baja el RV a reenvio del original y marca
    el candidato como aprobado. Devuelve False si no existe o ya no aplica."""
    cand = session.get(ReenvioLinkCandidato, candidato_id)
    if cand is None or cand.estado != "pendiente":
        return False
    rv = session.get(QuoteRequestRow, cand.rv_quote_id)
    original = session.get(QuoteRequestRow, cand.orig_quote_id)
    if rv is None or original is None:
        cand.estado = "descartado"
        session.commit()
        return False
    _demote_a_relacionado(session, rv, original, "reenvio_interno")
    cand.estado = "aprobado"
    session.commit()
    return True


def descartar_enlace_candidato(session: Session, candidato_id: int) -> bool:
    cand = session.get(ReenvioLinkCandidato, candidato_id)
    if cand is None or cand.estado != "pendiente":
        return False
    cand.estado = "descartado"
    session.commit()
    return True


def find_claude_review_by_imid(
    session: Session, internet_message_id: str | None
) -> ClaudeReview | None:
    """Busca un review de Claude por internet_message_id (RFC Message-ID, estable
    cross-mailbox). Sirve para deduplicar EN EL STAGING: el mismo correo recibido
    en varios buzones interceptados tiene el mismo Message-ID (pero distinto
    graph_id por buzon), y solo debe clasificarse/revisarse UNA vez."""
    if not internet_message_id:
        return None
    return session.execute(
        select(ClaudeReview)
        .where(ClaudeReview.internet_message_id == internet_message_id)
        .order_by(ClaudeReview.classified_at)
        .limit(1)
    ).scalar_one_or_none()


# ---------------------------------------------------------------------------
# Dedup LOGICO: agrupar correos equivalentes (mismo asunto + remitente) aunque
# tengan Message-ID/conversation distintos (reenvios repetidos del despachador).
# ---------------------------------------------------------------------------

_PREFIJOS_ASUNTO = re.compile(
    r"^\s*((re|rv|rsv|res|reply|fw|fwd|rmt|tr|fyi|psi)\s*:\s*)+", re.IGNORECASE
)


def _normalizar_asunto(subject: str) -> str:
    """Quita prefijos de reenvio/respuesta repetidos (RE:/RV:/FW:...), colapsa
    espacios y pasa a minusculas. 'RV: RE: Petición de oferta' -> 'petición de oferta'."""
    s = (subject or "").strip()
    prev = None
    while prev != s:
        prev = s
        s = _PREFIJOS_ASUNTO.sub("", s).strip()
    return re.sub(r"\s+", " ", s).lower()


def clave_grupo_logico(subject: str, sender_email: str) -> str | None:
    """Clave de agrupacion logica = asunto normalizado + remitente. Devuelve None
    si el asunto queda demasiado corto/trivial para ser discriminante (evita
    colapsar solicitudes distintas que solo comparten un asunto generico)."""
    asunto = _normalizar_asunto(subject)
    if len(asunto) < 8:
        return None
    remitente = (sender_email or "").strip().lower()
    return f"{asunto}|{remitente}"


def find_review_by_group(
    session: Session, group_logico: str | None
) -> ClaudeReview | None:
    """Representante de un grupo logico: el review mas antiguo que NO es copia."""
    if not group_logico:
        return None
    return session.execute(
        select(ClaudeReview)
        .where(
            ClaudeReview.group_logico == group_logico,
            ClaudeReview.estado != "copia_logica",
        )
        .order_by(ClaudeReview.classified_at)
        .limit(1)
    ).scalar_one_or_none()


def _aplicar_decision_a_copia(
    session: Session, copia: ClaudeReview, es_cotizacion: bool,
    parent_quote_id: int | None,
) -> None:
    """Aplica a una copia logica la MISMA suerte del representante: si es
    cotizacion, se consolida bajo la cotizacion padre; si no, se descarta."""
    copia.human_decision_es_cotizacion = es_cotizacion
    copia.reviewed_at = datetime.utcnow()
    if es_cotizacion and parent_quote_id is not None:
        msg = deserialize_message(copia.message_json)
        upsert_related(session, msg, parent_quote_id, relation_type="copia_logica")
        mark_processed(session, copia.graph_id, copia.mailbox, copia.subject,
                       "identificado", "copia_logica")
    else:
        mark_processed(session, copia.graph_id, copia.mailbox, copia.subject,
                       "descartado", "copia_logica_rechazada")
    session.commit()


def registrar_copia_logica(
    session: Session, msg, representante: ClaudeReview
) -> str:
    """Registra un correo como COPIA LOGICA del representante (sin gastar Claude).
    Si el representante ya tiene decision humana, la copia la hereda de inmediato.
    Devuelve un 'reason' para processed_messages."""
    copia = session.get(ClaudeReview, msg.id)
    if copia is None:
        copia = ClaudeReview(graph_id=msg.id)
        session.add(copia)
    copia.mailbox = msg.mailbox
    copia.subject = msg.subject
    copia.sender_email = msg.sender_email
    copia.received_at = msg.received_at
    copia.internet_message_id = msg.internet_message_id
    copia.message_json = serialize_message(msg)
    copia.es_cotizacion_cliente = representante.es_cotizacion_cliente
    copia.tipo = representante.tipo
    copia.confianza = representante.confianza
    copia.veredicto_final = "(copia logica del representante)"
    copia.estado = "copia_logica"
    copia.group_logico = representante.group_logico
    copia.representante_graph_id = representante.graph_id
    copia.classified_at = datetime.utcnow()
    session.commit()

    if representante.estado in ("aprobado", "rechazado"):
        parent_id = None
        if representante.estado == "aprobado":
            parent = upsert_quote(session, representante.graph_id)
            parent_id = parent.id if parent else None
        _aplicar_decision_a_copia(
            session, copia,
            es_cotizacion=bool(representante.human_decision_es_cotizacion),
            parent_quote_id=parent_id,
        )
        return ("copia_logica_aprobada" if representante.estado == "aprobado"
                else "copia_logica_rechazada")
    return "copia_logica_pendiente"


def propagar_a_copias_logicas(
    session: Session, representante_graph_id: str, es_cotizacion: bool,
    parent_quote_id: int | None,
) -> int:
    """Tras decidir el representante, aplica la misma decision a sus copias logicas
    aun sin resolver. Devuelve cuantas copias se afectaron."""
    copias = session.execute(
        select(ClaudeReview).where(
            ClaudeReview.representante_graph_id == representante_graph_id,
            ClaudeReview.estado == "copia_logica",
            ClaudeReview.human_decision_es_cotizacion.is_(None),
        )
    ).scalars().all()
    for copia in copias:
        _aplicar_decision_a_copia(session, copia, es_cotizacion, parent_quote_id)
    return len(copias)


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
