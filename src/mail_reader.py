"""Lectura de correos de un buzon Microsoft 365 + deteccion de respondidos."""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from datetime import datetime
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
    if subject_only:
        blob = subject.lower()
    else:
        blob = f"{subject}\n{body}".lower()
    return any(kw.lower() in blob for kw in keywords)


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
                "id,conversationId,subject,from,toRecipients,ccRecipients,"
                "receivedDateTime,bodyPreview,body,hasAttachments,isRead"
            ),
            "$orderby": "receivedDateTime desc",
        }
        if self.settings.unread_only:
            params["$filter"] = "isRead eq false"

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
        self, mailbox: str, download_attachments: bool = True
    ) -> list[Message]:
        results: list[Message] = []
        raw_messages = self.list_inbox_messages(mailbox)
        logger.info("Buzon %s: %d mensajes recuperados", mailbox, len(raw_messages))

        for raw in raw_messages:
            sender = (raw.get("from") or {}).get("emailAddress") or {}
            sender_email = (sender.get("address") or "").lower()
            sender_name = sender.get("name") or ""

            # Saltar correos que el mismo buzon se autoenvio
            if sender_email == mailbox.lower():
                continue
            # Filtro 1: remitentes excluidos (internos + blacklist)
            if _is_excluded_sender(sender_email, self.settings.exclude_sender_domains):
                continue

            subject = raw.get("subject") or ""

            # Filtro 2: excluir respuestas (RE:, Res:, Reply:)
            if _is_reply_subject(subject, self.settings.exclude_subject_prefixes):
                continue

            # Filtro 2b: excluir ordenes de compra ya aprobadas, facturas, etc.
            if _contains_excluded_text(subject, self.settings.exclude_subject_contains):
                continue

            body_obj = raw.get("body") or {}
            body_text = body_obj.get("content") or ""
            if (body_obj.get("contentType") or "").lower() == "html":
                body_text = _strip_html(body_text)

            # Filtro 3: keyword en asunto (o en cuerpo, segun config)
            if self.settings.keywords and not _matches_keywords(
                subject,
                body_text,
                self.settings.keywords,
                self.settings.keyword_in_subject_only,
            ):
                continue

            conversation_id = raw.get("conversationId", "")
            received_at = raw.get("receivedDateTime", "")
            was_replied, replied_at, cliente_ext = self.is_replied_to_client(
                mailbox, conversation_id, received_at, sender_email
            )

            internal_domain = (
                mailbox.split("@", 1)[1].lower() if "@" in mailbox else ""
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
                )
            )

        logger.info("Buzon %s: %d mensajes relevantes", mailbox, len(results))
        return results
