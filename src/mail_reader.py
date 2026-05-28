"""Lectura de correos de un buzon Microsoft 365 + deteccion de respondidos."""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Iterable

from .config import Settings
from .graph_client import GraphClient

logger = logging.getLogger(__name__)


@dataclass
class Attachment:
    id: str
    name: str
    content_type: str
    size: int
    local_path: Path


@dataclass
class Message:
    id: str
    mailbox: str
    conversation_id: str
    internet_message_id: str               # RFC Message-ID (estable cross-mailbox)
    subject: str
    sender_email: str
    sender_name: str
    received_at: str
    body_preview: str
    body_text: str
    has_attachments: bool
    attachments: list[Attachment] = field(default_factory=list)
    was_replied: bool = False                # respondida al cliente externo
    replied_at: str | None = None
    external_client: str | None = None       # email del cliente externo identificado
    is_forwarded_internal: bool = False      # True si llega de un colega (@dominio interno)
    # Solicitud ORIGINAL: correo que inicio la cadena (escenario 1)
    original_received_at: str | None = None
    original_snippet: str | None = None
    original_source: str | None = None       # hilo | cuerpo_citado | mismo_correo
    # Agrupacion de cotizaciones relacionadas (escenario 2)
    doc_ref: str | None = None
    group_key: str | None = None
    # Relacion RE -> original del hilo
    is_original: bool = True
    original_graph_id: str | None = None
    chain_source: str = "directo"            # directo | backfill_via_RE


def _strip_html(html: str) -> str:
    text = re.sub(r"<script.*?</script>", " ", html, flags=re.DOTALL | re.IGNORECASE)
    text = re.sub(r"<style.*?</style>", " ", text, flags=re.DOTALL | re.IGNORECASE)
    text = re.sub(r"<[^>]+>", " ", text)
    text = re.sub(r"&nbsp;", " ", text)
    text = re.sub(r"\s+", " ", text)
    return text.strip()


def _matches_keywords(
    subject: str, body: str, keywords: Iterable[str], subject_only: bool
) -> bool:
    blob = subject if subject_only else f"{subject}\n{body}"
    # Coincidencia por PALABRA, no por substring: limite de palabra al inicio y
    # se permiten sufijos/plurales al final (\w*). Asi "oferta" sigue matcheando
    # "ofertas", pero "bid" ya NO matchea dentro de "SUBIDA" (falso positivo).
    return any(
        re.search(rf"\b{re.escape(kw)}\w*", blob, re.IGNORECASE)
        for kw in keywords
    )


def _is_excluded_sender(sender_email: str, blacklist: Iterable[str]) -> bool:
    email = sender_email.lower()
    return any(token in email for token in blacklist)


def _is_reply_subject(subject: str, prefixes: Iterable[str]) -> bool:
    s = subject.strip().lower()
    return any(s.startswith(p.lower()) for p in prefixes)


def _contains_excluded_text(subject: str, terms: Iterable[str]) -> bool:
    """True si el asunto contiene alguno de los terminos (case-insensitive).
    Util para descartar OCC, ordenes de compra, facturas, etc.
    """
    s = subject.lower()
    return any(t.lower() in s for t in terms)


def _parse_iso(dt: str) -> datetime | None:
    if not dt:
        return None
    try:
        return datetime.fromisoformat(dt.replace("Z", "+00:00"))
    except ValueError:
        return None


def _inicio_de_hoy_utc() -> str:
    """Inicio del dia de hoy en hora local de Colombia (UTC-5), en UTC ISO 8601.
    Se usa para filtrar en Graph los correos recibidos hoy."""
    col = timezone(timedelta(hours=-5))
    inicio_local = datetime.now(col).replace(hour=0, minute=0, second=0, microsecond=0)
    return inicio_local.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _cuerpo_a_texto(msg: dict) -> str:
    """Texto plano del cuerpo de un mensaje de Graph (quita HTML)."""
    body_obj = msg.get("body") or {}
    txt = body_obj.get("content") or msg.get("bodyPreview") or ""
    if (body_obj.get("contentType") or "").lower() == "html":
        txt = _strip_html(txt)
    return txt


def _resumen_corto(texto: str, max_chars: int = 400) -> str:
    t = re.sub(r"\s+", " ", texto or "").strip()
    if len(t) > max_chars:
        t = t[:max_chars].rsplit(" ", 1)[0] + "..."
    return t


def _extraer_original_citado(body: str) -> tuple[str | None, str | None]:
    """Best-effort: extrae (fecha, texto) del correo original CITADO dentro de un
    reenvio/respuesta (bloque 'De:/Enviado:/Asunto:'). Devuelve (None, None) si no se
    halla. Util cuando un RV rompio el hilo y el original solo vive en el cuerpo."""
    if not body:
        return None, None
    m = re.search(r"\b(?:De|From)\s*:\s*\S", body, re.IGNORECASE)
    if not m:
        return None, None
    bloque = body[m.start():]
    fecha = None
    mf = re.search(
        r"(?:Enviado|Sent|Fecha)\s*:\s*(.+?)(?=\s+(?:Para|To|CC|Asunto|Subject)\s*:|$)",
        bloque,
        re.IGNORECASE | re.DOTALL,
    )
    if mf:
        fecha = mf.group(1).strip()[:120]
    ms = re.search(r"(?:Asunto|Subject)\s*:\s*.+", bloque, re.IGNORECASE)
    texto = bloque[ms.end():].strip() if ms else bloque.strip()
    return fecha, (texto or None)


def _dominio(email: str) -> str:
    return email.split("@", 1)[1].lower() if email and "@" in email else ""


def _es_cadena_saliente(conv_msgs: list[dict], internal_domain: str) -> bool:
    """True si el PRIMER mensaje del hilo (mas antiguo) lo envio alguien INTERNO
    a destinatarios TODOS externos. Significa que EDEMCO inicio la conversacion
    (cotizacion saliente: nosotros le pedimos a un proveedor) y por tanto no es
    una solicitud de cotizacion entrante.

    Distincion clave con reenvio interno: en un reenvio entre colegas, los
    destinatarios del primer mensaje son INTERNOS -> NO se descarta."""
    if not conv_msgs or not internal_domain:
        return False
    ordenados = sorted(
        conv_msgs,
        key=lambda m: _parse_iso(
            m.get("sentDateTime") or m.get("receivedDateTime", "")
        ) or datetime.max.replace(tzinfo=timezone.utc),
    )
    primero = ordenados[0]
    sender = (
        ((primero.get("from") or {}).get("emailAddress") or {}).get("address") or ""
    ).lower()
    internal = internal_domain.lower().lstrip("@")
    if not sender.endswith(internal):
        return False  # primer mensaje es externo -> cadena entrante

    destinatarios: list[str] = []
    for rec_field in ("toRecipients", "ccRecipients"):
        for r in primero.get(rec_field) or []:
            addr = ((r.get("emailAddress") or {}).get("address") or "").lower()
            if addr:
                destinatarios.append(addr)
    if not destinatarios:
        return False  # sin info de destinatarios -> no decidir
    return all(not d.endswith(internal) for d in destinatarios)


def _localizar_primer_externo(
    conv_msgs: list[dict], internal_domain: str
) -> dict | None:
    """Primer mensaje del hilo enviado por alguien EXTERNO al dominio interno.
    Ignora mensajes previos enviados por nosotros (asi el 'original de la solicitud'
    es realmente la del cliente y no algo que nosotros le escribimos primero)."""
    if not conv_msgs:
        return None
    ordenados = sorted(
        conv_msgs,
        key=lambda m: _parse_iso(
            m.get("sentDateTime") or m.get("receivedDateTime", "")
        ) or datetime.max.replace(tzinfo=timezone.utc),
    )
    internal = internal_domain.lower().lstrip("@")
    for m in ordenados:
        sender = (
            ((m.get("from") or {}).get("emailAddress") or {}).get("address") or ""
        ).lower()
        if sender and (not internal or not sender.endswith(internal)):
            return m
    return None


def _extraer_referencia(subject: str, body: str, pattern: str) -> str | None:
    """Extrae el numero de documento/solicitud (p. ej. 'OPM-20260527') que agrupa
    varios correos de una misma cotizacion consolidada. Devuelve None si no hay
    patron configurado o no se halla coincidencia. Busca primero en el asunto."""
    if not pattern:
        return None
    try:
        rx = re.compile(pattern)
    except re.error:
        logger.warning("group_reference_regex invalido: %s", pattern)
        return None
    m = rx.search(subject or "") or rx.search(body or "")
    return m.group(0).upper() if m else None


class MailReader:
    def __init__(self, client: GraphClient, settings: Settings) -> None:
        self.client = client
        self.settings = settings
        self.settings.attachments_dir.mkdir(parents=True, exist_ok=True)

    # -------- Listar mensajes del Inbox --------
    def list_inbox_messages(self, mailbox: str) -> list[dict]:
        params: dict[str, str | int] = {
            "$top": self.settings.max_messages_per_mailbox,
            "$select": (
                "id,internetMessageId,conversationId,subject,from,toRecipients,"
                "ccRecipients,receivedDateTime,bodyPreview,body,hasAttachments,isRead"
            ),
            "$orderby": "receivedDateTime desc",
        }
        filters: list[str] = []
        if self.settings.unread_only:
            filters.append("isRead eq false")
        if self.settings.only_today:
            filters.append(f"receivedDateTime ge {_inicio_de_hoy_utc()}")
        if filters:
            params["$filter"] = " and ".join(filters)

        path = f"/users/{mailbox}/mailFolders/Inbox/messages"
        data = self.client.get(path, params=params)
        return data.get("value", [])

    # -------- Hilo completo de un conversationId --------
    def get_conversation_messages(
        self, mailbox: str, conversation_id: str
    ) -> list[dict]:
        if not conversation_id:
            return []
        cid_escaped = conversation_id.replace("'", "''")
        params = {
            "$top": 100,
            "$select": (
                "id,from,toRecipients,ccRecipients,sentDateTime,"
                "receivedDateTime,subject"
            ),
            "$filter": f"conversationId eq '{cid_escaped}'",
        }
        try:
            data = self.client.get(f"/users/{mailbox}/messages", params=params)
        except Exception as exc:
            logger.warning("Error leyendo hilo %s: %s", conversation_id, exc)
            return []
        return data.get("value", [])

    # -------- Detalle (con cuerpo) de un mensaje puntual --------
    def get_message_detail(self, mailbox: str, message_id: str) -> dict | None:
        params = {
            "$select": (
                "id,internetMessageId,conversationId,subject,from,toRecipients,"
                "ccRecipients,sentDateTime,receivedDateTime,body,bodyPreview,"
                "hasAttachments,isRead"
            ),
        }
        try:
            return self.client.get(
                f"/users/{mailbox}/messages/{message_id}", params=params
            )
        except Exception as exc:
            logger.warning("No se pudo leer el correo original %s: %s", message_id, exc)
            return None

    # -------- Identificar al CLIENTE EXTERNO del hilo --------
    def _identify_external_client(
        self, conversation_msgs: list[dict], internal_domain: str
    ) -> str | None:
        """Cliente externo = primer remitente del hilo que NO sea del dominio interno.
        Mira el correo mas antiguo del hilo primero.
        """
        internal = internal_domain.lower().lstrip("@")
        # Ordenamos del mas antiguo al mas nuevo
        msgs = sorted(
            conversation_msgs,
            key=lambda m: _parse_iso(
                m.get("sentDateTime") or m.get("receivedDateTime", "")
            ) or datetime.min,
        )
        for m in msgs:
            email = (
                ((m.get("from") or {}).get("emailAddress") or {}).get("address") or ""
            ).lower()
            if email and internal and not email.endswith(internal):
                return email
        return None

    # -------- Verificar si la cotizacion ya fue respondida AL CLIENTE --------
    def is_replied_to_client(
        self,
        mailbox: str,
        conversation_id: str,
        incoming_received_at: str,
        incoming_sender: str,
        conv_msgs: list[dict] | None = None,
    ) -> tuple[bool, str | None, str | None]:
        """Devuelve (respondida_al_cliente, fecha_respuesta_iso, cliente_externo).

        Una cotizacion se considera RESPONDIDA solo si:
        - Existe un mensaje en el mismo hilo
        - Enviado por el dueño del buzon (alguien interno)
        - Cuyo destinatario incluye al CLIENTE EXTERNO del hilo
        - Posterior al correo entrante
        """
        if not conversation_id:
            return False, None, None

        # Dominio interno = el del buzon (ej. 'edemco.co')
        internal_domain = mailbox.split("@", 1)[1].lower() if "@" in mailbox else ""
        if conv_msgs is None:
            conv_msgs = self.get_conversation_messages(mailbox, conversation_id)

        # Cliente externo: primero buscar en el hilo, si no se infiere del entrante
        cliente_ext = self._identify_external_client(conv_msgs, internal_domain)
        if not cliente_ext and incoming_sender and "@" in incoming_sender:
            if not incoming_sender.lower().endswith(internal_domain):
                cliente_ext = incoming_sender.lower()

        if not cliente_ext:
            # Hilo 100% interno → no podemos verificar respuesta al cliente
            return False, None, None

        entrante = _parse_iso(incoming_received_at)

        # Buscar en el hilo un mensaje enviado por el buzon (o cualquier interno)
        # cuyos destinatarios incluyan al cliente externo
        for m in conv_msgs:
            sender_email = (
                ((m.get("from") or {}).get("emailAddress") or {}).get("address") or ""
            ).lower()
            # El emisor debe ser interno
            if not internal_domain or not sender_email.endswith(internal_domain):
                continue

            # ¿Destinatarios incluyen al cliente externo?
            recipients = []
            for rec_field in ("toRecipients", "ccRecipients"):
                for r in (m.get(rec_field) or []):
                    addr = ((r.get("emailAddress") or {}).get("address") or "").lower()
                    if addr:
                        recipients.append(addr)
            if cliente_ext not in recipients:
                continue

            sent_at = _parse_iso(m.get("sentDateTime") or m.get("receivedDateTime", ""))
            if not sent_at:
                continue
            if entrante is None or sent_at > entrante:
                return True, sent_at.isoformat(), cliente_ext

        return False, None, cliente_ext

    # -------- Localizar la SOLICITUD ORIGINAL (escenario 1 + REs) --------
    def _obtener_solicitud_original(
        self,
        mailbox: str,
        conv_msgs: list[dict],
        incoming_raw: dict,
        internal_domain: str = "",
    ) -> tuple[str | None, str | None, str | None, str]:
        """Localiza el correo que INICIO la cadena (primer mensaje EXTERNO del hilo)
        y devuelve (original_graph_id, fecha, texto, fuente).
        fuente: 'hilo' (primer externo del hilo, distinto del entrante) |
                'cuerpo_citado' (RV que rompio el hilo, original embebido en cuerpo) |
                'mismo_correo' (el entrante es el primer externo del hilo)."""
        incoming_id = incoming_raw.get("id")

        # 1) Primer mensaje EXTERNO del hilo (no necesariamente el primero absoluto)
        primer_externo = _localizar_primer_externo(conv_msgs, internal_domain)
        if (
            primer_externo
            and primer_externo.get("id")
            and primer_externo["id"] != incoming_id
        ):
            detalle = self.get_message_detail(mailbox, primer_externo["id"])
            if detalle:
                fecha = (
                    detalle.get("sentDateTime")
                    or detalle.get("receivedDateTime")
                    or ""
                )
                texto = _resumen_corto(_cuerpo_a_texto(detalle))
                return primer_externo["id"], (fecha or None), (texto or None), "hilo"

        # 2) Original embebido en el cuerpo del entrante (reenvio que rompio el hilo)
        fecha_cit, texto_cit = _extraer_original_citado(_cuerpo_a_texto(incoming_raw))
        if texto_cit:
            return None, fecha_cit, _resumen_corto(texto_cit), "cuerpo_citado"

        # 3) El propio correo es la solicitud original
        return None, (incoming_raw.get("receivedDateTime") or None), None, "mismo_correo"

    # -------- Backfill: arma un Message para un correo ORIGINAL no registrado --------
    def build_original_message(
        self,
        mailbox: str,
        original_graph_id: str,
        download_attachments: bool = True,
    ) -> Message | None:
        """Trae el detalle de un correo original y arma un Message listo para guardar.
        Se EXIME de los filtros (keywords/exclusiones) porque la cadena ya fue
        identificada como cotizacion via la RE; el original es la raiz de esa cadena."""
        raw = self.get_message_detail(mailbox, original_graph_id)
        if not raw:
            return None

        sender = (raw.get("from") or {}).get("emailAddress") or {}
        sender_email = (sender.get("address") or "").lower()
        sender_name = sender.get("name") or ""
        subject = raw.get("subject") or ""
        body_text = _cuerpo_a_texto(raw)
        conversation_id = raw.get("conversationId", "")
        received_at = raw.get("receivedDateTime", "")
        internal_domain = mailbox.split("@", 1)[1].lower() if "@" in mailbox else ""

        conv_msgs = self.get_conversation_messages(mailbox, conversation_id)
        was_replied, replied_at, cliente_ext = self.is_replied_to_client(
            mailbox, conversation_id, received_at, sender_email, conv_msgs=conv_msgs
        )

        is_internal_fwd = bool(
            internal_domain and sender_email.endswith(internal_domain)
        )

        doc_ref = _extraer_referencia(
            subject, body_text, self.settings.group_reference_regex
        )
        cliente_para_grupo = cliente_ext or sender_email
        group_key = (
            f"{_dominio(cliente_para_grupo)}|{doc_ref}" if doc_ref else None
        )

        attachments: list[Attachment] = []
        if download_attachments and raw.get("hasAttachments"):
            try:
                for att in self.list_attachments(mailbox, original_graph_id):
                    if att.get("@odata.type", "").endswith("fileAttachment"):
                        downloaded = self.download_attachment(
                            mailbox, original_graph_id, att
                        )
                        if downloaded:
                            attachments.append(downloaded)
            except Exception as exc:
                logger.warning(
                    "No se pudieron listar adjuntos del original %s: %s",
                    original_graph_id, exc,
                )

        return Message(
            id=original_graph_id,
            mailbox=mailbox,
            conversation_id=conversation_id,
            internet_message_id=raw.get("internetMessageId") or "",
            subject=subject,
            sender_email=sender_email,
            sender_name=sender_name,
            received_at=received_at,
            body_preview=raw.get("bodyPreview", ""),
            body_text=body_text,
            has_attachments=bool(raw.get("hasAttachments")),
            attachments=attachments,
            was_replied=was_replied,
            replied_at=replied_at,
            external_client=cliente_ext,
            is_forwarded_internal=is_internal_fwd,
            original_received_at=None,
            original_snippet=None,
            original_source="mismo_correo",
            doc_ref=doc_ref,
            group_key=group_key,
            is_original=True,
            original_graph_id=None,
            chain_source="backfill_via_RE",
        )

    # -------- Adjuntos --------
    def list_attachments(self, mailbox: str, message_id: str) -> list[dict]:
        path = f"/users/{mailbox}/messages/{message_id}/attachments"
        data = self.client.get(path)
        return data.get("value", [])

    def download_attachment(
        self, mailbox: str, message_id: str, att: dict
    ) -> Attachment | None:
        name = att.get("name", "attachment.bin")
        ext = Path(name).suffix.lower()
        if (
            self.settings.attachment_extensions
            and ext not in self.settings.attachment_extensions
        ):
            return None
        path = f"/users/{mailbox}/messages/{message_id}/attachments/{att['id']}/$value"
        try:
            content = self.client.get_bytes(path)
        except Exception as exc:
            logger.warning("No se pudo descargar adjunto %s: %s", name, exc)
            return None

        target_dir = self.settings.attachments_dir / mailbox / message_id
        target_dir.mkdir(parents=True, exist_ok=True)
        safe_name = re.sub(r"[^\w.\-]+", "_", name)
        local_path = target_dir / safe_name
        local_path.write_bytes(content)
        return Attachment(
            id=att["id"],
            name=name,
            content_type=att.get("contentType", "application/octet-stream"),
            size=att.get("size", len(content)),
            local_path=local_path,
        )

    # -------- Flujo principal --------
    def fetch_relevant_messages(
        self,
        mailbox: str,
        download_attachments: bool = True,
        skip_ids: set[str] | None = None,
    ) -> tuple[list[Message], list[tuple[str, str, str]]]:
        """Lee el buzon y devuelve (relevantes, descartados).
        descartados = lista de (graph_id, asunto, motivo) para registrarlos y no
        reprocesarlos en futuras corridas (escenario 3)."""
        skip_ids = skip_ids or set()
        results: list[Message] = []
        descartados: list[tuple[str, str, str]] = []
        raw_messages = self.list_inbox_messages(mailbox)
        logger.info("Buzon %s: %d mensajes recuperados", mailbox, len(raw_messages))

        internal_domain = mailbox.split("@", 1)[1].lower() if "@" in mailbox else ""

        for raw in raw_messages:
            gid = raw.get("id", "")
            subject = raw.get("subject") or ""

            # Escenario 3: saltar lo ya procesado en estado final (no reprocesar)
            if gid in skip_ids:
                continue

            sender = (raw.get("from") or {}).get("emailAddress") or {}
            sender_email = (sender.get("address") or "").lower()
            sender_name = sender.get("name") or ""

            # Saltar correos que el mismo buzon se autoenvio
            if sender_email == mailbox.lower():
                descartados.append((gid, subject, "autoenvio"))
                continue
            # Filtro 1: remitentes excluidos (internos + blacklist)
            if _is_excluded_sender(sender_email, self.settings.exclude_sender_domains):
                descartados.append((gid, subject, "remitente_excluido"))
                continue
            # Filtro 2: excluir respuestas por prefijo de asunto
            if _is_reply_subject(subject, self.settings.exclude_subject_prefixes):
                descartados.append((gid, subject, "prefijo_excluido"))
                continue
            # Filtro 2b: excluir ordenes de compra ya aprobadas, facturas, etc.
            if _contains_excluded_text(subject, self.settings.exclude_subject_contains):
                descartados.append((gid, subject, "texto_excluido"))
                continue

            body_text = _cuerpo_a_texto(raw)

            # Filtro 2c: cuerpo con texto excluido (reuniones de Teams, calendarios, etc.)
            if _contains_excluded_text(body_text, self.settings.exclude_body_contains):
                descartados.append((gid, subject, "cuerpo_excluido"))
                continue

            # Filtro 3: keyword en asunto (o en cuerpo, segun config)
            if self.settings.keywords and not _matches_keywords(
                subject,
                body_text,
                self.settings.keywords,
                self.settings.keyword_in_subject_only,
            ):
                descartados.append((gid, subject, "sin_keyword"))
                continue

            conversation_id = raw.get("conversationId", "")
            received_at = raw.get("receivedDateTime", "")

            # El hilo se trae UNA sola vez y se reutiliza (respuesta + original)
            conv_msgs = self.get_conversation_messages(mailbox, conversation_id)

            # Filtro 4: cadena SALIENTE (la iniciamos nosotros pidiendo a un proveedor)
            # -> no es una solicitud de cotizacion entrante, descartar.
            if _es_cadena_saliente(conv_msgs, internal_domain):
                descartados.append((gid, subject, "cotizacion_saliente"))
                continue

            was_replied, replied_at, cliente_ext = self.is_replied_to_client(
                mailbox, conversation_id, received_at, sender_email, conv_msgs=conv_msgs
            )

            # Escenario 1 + REs: localizar el correo ORIGINAL (primer externo del hilo)
            orig_id, orig_fecha, orig_texto, orig_fuente = self._obtener_solicitud_original(
                mailbox, conv_msgs, raw, internal_domain
            )
            es_original = orig_fuente == "mismo_correo"

            # Escenario 2: agrupar cotizaciones relacionadas por numero de documento
            doc_ref = _extraer_referencia(
                subject, body_text, self.settings.group_reference_regex
            )
            cliente_para_grupo = cliente_ext or sender_email
            group_key = (
                f"{_dominio(cliente_para_grupo)}|{doc_ref}" if doc_ref else None
            )

            is_internal_fwd = bool(
                internal_domain and sender_email.endswith(internal_domain)
            )

            attachments: list[Attachment] = []
            if download_attachments and raw.get("hasAttachments"):
                for att in self.list_attachments(mailbox, raw["id"]):
                    if att.get("@odata.type", "").endswith("fileAttachment"):
                        downloaded = self.download_attachment(mailbox, raw["id"], att)
                        if downloaded:
                            attachments.append(downloaded)

            results.append(
                Message(
                    id=raw["id"],
                    mailbox=mailbox,
                    conversation_id=conversation_id,
                    internet_message_id=raw.get("internetMessageId") or "",
                    subject=subject,
                    sender_email=sender_email,
                    sender_name=sender_name,
                    received_at=received_at,
                    body_preview=raw.get("bodyPreview", ""),
                    body_text=body_text,
                    has_attachments=bool(raw.get("hasAttachments")),
                    attachments=attachments,
                    was_replied=was_replied,
                    replied_at=replied_at,
                    external_client=cliente_ext,
                    is_forwarded_internal=is_internal_fwd,
                    original_received_at=orig_fecha,
                    original_snippet=orig_texto,
                    original_source=orig_fuente,
                    doc_ref=doc_ref,
                    group_key=group_key,
                    is_original=es_original,
                    original_graph_id=orig_id,
                    chain_source="directo",
                )
            )

        logger.info(
            "Buzon %s: %d relevantes, %d descartados",
            mailbox,
            len(results),
            len(descartados),
        )
        return results, descartados
