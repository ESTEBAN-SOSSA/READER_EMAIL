"""Carga de variables de entorno y settings.yaml."""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

import yaml
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent
load_dotenv(ROOT / ".env")


@dataclass
class Settings:
    tenant_id: str
    client_id: str
    client_secret: str
    mailboxes: list[str]
    max_messages_per_mailbox: int
    unread_only: bool
    attachments_dir: Path
    database_path: Path
    log_level: str

    keywords: list[str] = field(default_factory=list)
    keyword_in_subject_only: bool = True
    exclude_sender_domains: list[str] = field(default_factory=list)
    exclude_subject_prefixes: list[str] = field(default_factory=list)
    exclude_subject_contains: list[str] = field(default_factory=list)
    attachment_extensions: list[str] = field(default_factory=list)


def _require(name: str) -> str:
    value = os.getenv(name)
    if not value:
        raise RuntimeError(
            f"Falta la variable de entorno {name}. Revisa tu archivo .env"
        )
    return value


def load_settings(settings_path: Path | None = None) -> Settings:
    settings_path = settings_path or (ROOT / "config" / "settings.yaml")
    with settings_path.open("r", encoding="utf-8") as fh:
        raw = yaml.safe_load(fh) or {}

    mailboxes = [m.strip() for m in os.getenv("MAILBOXES", "").split(",") if m.strip()]

    return Settings(
        tenant_id=_require("AZURE_TENANT_ID"),
        client_id=_require("AZURE_CLIENT_ID"),
        client_secret=_require("AZURE_CLIENT_SECRET"),
        mailboxes=mailboxes,
        max_messages_per_mailbox=int(os.getenv("MAX_MESSAGES_PER_MAILBOX", "50")),
        unread_only=os.getenv("UNREAD_ONLY", "true").lower() == "true",
        attachments_dir=ROOT / os.getenv("ATTACHMENTS_DIR", "./attachments"),
        database_path=ROOT / os.getenv("DATABASE_PATH", "./data/reader_email.db"),
        log_level=os.getenv("LOG_LEVEL", "INFO").upper(),
        keywords=raw.get("keywords", []),
        keyword_in_subject_only=bool(raw.get("keyword_in_subject_only", True)),
        exclude_sender_domains=raw.get(
            "exclude_sender_domains", raw.get("sender_blacklist_domains", [])
        ),
        exclude_subject_prefixes=raw.get("exclude_subject_prefixes", []),
        exclude_subject_contains=raw.get("exclude_subject_contains", []),
        attachment_extensions=raw.get("attachment_extensions", []),
    )
