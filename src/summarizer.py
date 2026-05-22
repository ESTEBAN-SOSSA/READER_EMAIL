"""Genera un resumen breve de la solicitud de cotizacion.

NO extrae montos (las cotizaciones entrantes son solicitudes, no facturas).
Solo arma un snippet del cuerpo y lista los adjuntos para que la persona
encargada pueda decidir rapido.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

# Frases tipicas de saludo/firma que NO aportan al resumen
_RUIDO_PREFIJOS = [
    r"^buenos\s+d[ií]as[,.\s]*",
    r"^buenas\s+tardes[,.\s]*",
    r"^buenas\s+noches[,.\s]*",
    r"^hola[,.\s]*",
    r"^cordial\s+saludo[,.\s]*",
    r"^estimad[oa]s?[,.\s]*",
    r"^se[ñn]or[ea]?s?[,.\s]*",
    r"^apreciad[oa]s?[,.\s]*",
]

_RUIDO_FIRMAS = re.compile(
    r"(gracias|saludos|cordialmente|atentamente|atte\.?|"
    r"agradezco\s+su\s+gesti[oó]n|quedo\s+atent[oa]|"
    r"qualquier\s+inquietud|cualquier\s+inquietud).*$",
    re.IGNORECASE | re.DOTALL,
)


@dataclass
class QuoteRequest:
    snippet: str                          # primeras lineas utiles del correo
    productos_mencionados: list[str] = field(default_factory=list)
    cantidades_mencionadas: list[str] = field(default_factory=list)
    adjuntos: list[str] = field(default_factory=list)
    tiene_pdf_o_excel: bool = False
    num_items_estimado: int = 0           # estimacion de cuantos items pide cotizar


_RE_CANTIDAD = re.compile(
    r"\b(\d{1,6})\s*(unidades?|und\.?|pcs|piezas?|metros?|mts?\.?|"
    r"kg|kilos?|cajas?|rollos?|pares?|bultos?|toneladas?|gal|galones?|lts?)\b",
    re.IGNORECASE,
)


def _limpiar_snippet(texto: str, max_chars: int = 350) -> str:
    if not texto:
        return ""
    t = re.sub(r"\s+", " ", texto).strip()
    # Quitar saludos al inicio
    for pat in _RUIDO_PREFIJOS:
        t = re.sub(pat, "", t, count=1, flags=re.IGNORECASE).strip()
    # Cortar antes de la firma
    t = _RUIDO_FIRMAS.sub("", t, count=1).strip()
    # Quitar referencias tipicas "De: ... Enviado: ..." al final (hilos de correo)
    t = re.split(r"\bDe:\s+|\bFrom:\s+", t, maxsplit=1)[0].strip()
    if len(t) > max_chars:
        t = t[:max_chars].rsplit(" ", 1)[0] + "..."
    return t


def _detectar_cantidades(texto: str, limite: int = 5) -> list[str]:
    matches: list[str] = []
    vistos: set[str] = set()
    for m in _RE_CANTIDAD.finditer(texto):
        s = m.group(0).strip()
        key = s.lower()
        if key not in vistos:
            vistos.add(key)
            matches.append(s)
        if len(matches) >= limite:
            break
    return matches


# Patrones de listado de items en el cuerpo del correo
_RE_BULLET = re.compile(r"(?m)^\s*(?:[-*•·●]|\d{1,3}[\.\)])\s+\S")
_RE_NUMBERED_LINE = re.compile(r"(?m)^\s*\d{1,3}[\.\)]\s+\S")


def _contar_items_en_cuerpo(texto: str) -> int:
    """Cuenta lineas que parecen items: viñetas o numeradas."""
    if not texto:
        return 0
    return max(len(_RE_BULLET.findall(texto)), len(_RE_NUMBERED_LINE.findall(texto)))


def resumir(body_text: str, attachment_names: list[str]) -> QuoteRequest:
    snippet = _limpiar_snippet(body_text)
    cantidades = _detectar_cantidades(body_text)

    tiene_doc = any(
        n.lower().endswith((".pdf", ".xlsx", ".xls", ".csv"))
        for n in attachment_names
    )

    # Estimacion del numero de items que el cliente pide cotizar:
    # max entre lineas con viñeta/numero y cantidades mencionadas.
    # Si hay PDF/Excel adjunto, agregamos como minimo 1 (los items estan ahi).
    items_cuerpo = max(_contar_items_en_cuerpo(body_text), len(cantidades))
    if tiene_doc and items_cuerpo == 0:
        items_cuerpo = 1  # al menos un PDF/Excel = 1 "lote" de items

    return QuoteRequest(
        snippet=snippet,
        cantidades_mencionadas=cantidades,
        adjuntos=attachment_names,
        tiene_pdf_o_excel=tiene_doc,
        num_items_estimado=items_cuerpo,
    )
