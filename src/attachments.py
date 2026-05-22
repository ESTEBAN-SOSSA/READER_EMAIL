"""Parser de adjuntos PDF y Excel/CSV para extraer texto y filas tabulares."""
from __future__ import annotations

import csv
import logging
from dataclasses import dataclass, field
from pathlib import Path

import pandas as pd
from pypdf import PdfReader

try:
    from docx import Document as _DocxDocument
except ImportError:
    _DocxDocument = None

logger = logging.getLogger(__name__)


@dataclass
class ParsedAttachment:
    path: Path
    kind: str  # "pdf" | "excel" | "csv" | "unknown"
    text: str = ""
    rows: list[dict] = field(default_factory=list)


def _parse_pdf(path: Path) -> ParsedAttachment:
    text_parts: list[str] = []
    try:
        reader = PdfReader(str(path))
        for page in reader.pages:
            text_parts.append(page.extract_text() or "")
    except Exception as exc:
        logger.warning("Error leyendo PDF %s: %s", path.name, exc)
    return ParsedAttachment(path=path, kind="pdf", text="\n".join(text_parts).strip())


def _parse_excel(path: Path) -> ParsedAttachment:
    rows: list[dict] = []
    text_parts: list[str] = []
    try:
        xls = pd.ExcelFile(path)
        for sheet in xls.sheet_names:
            df = xls.parse(sheet)
            df.columns = [str(c).strip().lower() for c in df.columns]
            for record in df.to_dict(orient="records"):
                cleaned = {k: v for k, v in record.items() if pd.notna(v)}
                if cleaned:
                    rows.append({"_sheet": sheet, **cleaned})
            text_parts.append(df.to_string(index=False))
    except Exception as exc:
        logger.warning("Error leyendo Excel %s: %s", path.name, exc)
    return ParsedAttachment(
        path=path, kind="excel", text="\n".join(text_parts), rows=rows
    )


def _parse_csv(path: Path) -> ParsedAttachment:
    rows: list[dict] = []
    try:
        with path.open("r", encoding="utf-8-sig", newline="") as fh:
            reader = csv.DictReader(fh)
            for record in reader:
                cleaned = {
                    (k or "").strip().lower(): (v or "").strip()
                    for k, v in record.items()
                    if k
                }
                if any(cleaned.values()):
                    rows.append(cleaned)
    except Exception as exc:
        logger.warning("Error leyendo CSV %s: %s", path.name, exc)
    text = "\n".join(", ".join(f"{k}={v}" for k, v in r.items()) for r in rows)
    return ParsedAttachment(path=path, kind="csv", text=text, rows=rows)


def _parse_docx(path: Path) -> ParsedAttachment:
    if _DocxDocument is None:
        logger.warning("python-docx no instalado; no se puede leer %s", path.name)
        return ParsedAttachment(path=path, kind="word")
    text_parts: list[str] = []
    rows: list[dict] = []
    try:
        doc = _DocxDocument(str(path))
        for p in doc.paragraphs:
            if p.text and p.text.strip():
                text_parts.append(p.text.strip())
        # Tablas: extraer filas como diccionarios (primera fila = header)
        for tbl in doc.tables:
            if not tbl.rows:
                continue
            headers = [c.text.strip().lower() for c in tbl.rows[0].cells]
            for row in tbl.rows[1:]:
                values = [c.text.strip() for c in row.cells]
                record = dict(zip(headers, values))
                cleaned = {k: v for k, v in record.items() if v}
                if cleaned:
                    rows.append(cleaned)
                text_parts.append(" | ".join(values))
    except Exception as exc:
        logger.warning("Error leyendo Word %s: %s", path.name, exc)
    return ParsedAttachment(
        path=path, kind="word", text="\n".join(text_parts).strip(), rows=rows
    )


def parse_attachment(path: Path) -> ParsedAttachment:
    ext = path.suffix.lower()
    if ext == ".pdf":
        return _parse_pdf(path)
    if ext in {".xlsx", ".xls"}:
        return _parse_excel(path)
    if ext == ".csv":
        return _parse_csv(path)
    if ext == ".docx":
        return _parse_docx(path)
    if ext == ".doc":
        logger.info(
            "Archivo .doc (formato antiguo Word) no soportado directamente: %s. "
            "Convertir a .docx para procesar.",
            path.name,
        )
        return ParsedAttachment(path=path, kind="word_old")
    # Imagenes: por ahora no extraemos texto (requiere OCR Tesseract)
    if ext in {".jpg", ".jpeg", ".png", ".gif", ".bmp", ".tiff"}:
        return ParsedAttachment(path=path, kind="image")
    return ParsedAttachment(path=path, kind="unknown")
