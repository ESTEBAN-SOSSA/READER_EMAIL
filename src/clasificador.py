"""Clasificacion semantica de correos con Claude (Anthropic).

Recibe correos que ya pasaron los filtros determinsticos y decide si son
realmente solicitudes de cotizacion de un cliente o no, devolviendo razonamiento
estructurado y reglas sugeridas para enriquecer el filtro determinstico.

Modelo recomendado: claude-haiku-4-5 (rapido y barato para clasificacion).
Prompt caching: el system prompt (reglas + ejemplos) se cachea para abaratar
las llamadas dentro de la misma corrida.
"""
from __future__ import annotations

import logging
from typing import Literal

import anthropic
from pydantic import BaseModel, Field

logger = logging.getLogger(__name__)


# ============================================================================
# Schema de salida (Claude debe devolver JSON con esta forma exacta)
# ============================================================================

class ReglaSugerida(BaseModel):
    """Una regla concreta para agregar al filtro determinstico."""
    tipo: Literal[
        "keyword",
        "exclude_subject_contains",
        "exclude_body_contains",
        "exclude_sender_domain",
        "exclude_sender_email",
        "supplier_email",
        "supplier_phrase",
    ] = Field(description="Tipo de regla a agregar")
    valor: str = Field(description="String concreto a agregar a la regla")
    razon: str = Field(description="Por que esta regla atrapa este patron")
    ejemplos_apoyo: list[str] = Field(
        default_factory=list,
        description="graph_ids de otros correos en BD que esta regla tambien cubrira (opcional)",
    )


TIPOS_VALIDOS = [
    "cliente_solicita",
    "proveedor_responde",
    "reunion_teams",
    "interno_administrativo",
    "cobro_factura",
    "automatico_notificacion",
    "otro",
]


class ClasificacionClaude(BaseModel):
    """Veredicto estructurado de Claude sobre un correo."""
    es_cotizacion_cliente: bool = Field(
        description="True si es una solicitud de cotizacion de un cliente externo"
    )
    tipo: Literal[
        "cliente_solicita",
        "proveedor_responde",
        "reunion_teams",
        "interno_administrativo",
        "cobro_factura",
        "automatico_notificacion",
        "otro",
    ] = Field(description="Categoria que mejor describe el correo")
    confianza: float = Field(ge=0.0, le=1.0, description="Confianza 0.0-1.0")
    razonamiento_si: str = Field(
        description="Argumento mas fuerte de POR QUE PODRIA ser una cotizacion (steelman)"
    )
    razonamiento_no: str = Field(
        description="Argumento mas fuerte de POR QUE PODRIA NO serlo (steelman opuesto)"
    )
    veredicto_final: str = Field(
        description="Decision sintetizada en 1-2 oraciones"
    )
    reglas_sugeridas: list[ReglaSugerida] = Field(
        default_factory=list,
        description="0..N propuestas de reglas para enriquecer el filtro determinstico. "
                    "Solo sugiere reglas con alta confianza en el patron, que NO esten ya "
                    "en las listas vigentes y que sean lo suficientemente especficas para "
                    "no causar falsos positivos.",
    )


# ============================================================================
# Construccion del system prompt
# ============================================================================

def _construir_system_prompt(
    reglas_vigentes: dict[str, list[str]],
    ejemplos_humanos: list[dict] | None = None,
) -> str:
    """System prompt con el contexto de negocio, reglas vigentes y ejemplos
    aprendidos. Esta parte es ESTABLE dentro de una corrida y se cachea."""

    def _fmt_list(items: list[str], limite: int = 80) -> str:
        if not items:
            return "(ninguna)"
        recortados = [str(x) for x in items[:limite]]
        salida = ", ".join(f'"{x}"' for x in recortados)
        if len(items) > limite:
            salida += f", ... ({len(items) - limite} mas)"
        return salida

    bloques: list[str] = []
    bloques.append(
        "Eres un clasificador experto de correos para Edemco, una empresa colombiana "
        "del sector electrico industrial. Tu trabajo es decidir, para cada correo "
        "que ya paso los filtros determinsticos, si es una SOLICITUD DE COTIZACION "
        "DE UN CLIENTE EXTERNO (lo que SI queremos registrar) o NO (proveedor "
        "cotizandonos a nosotros, invitacion a reunion, factura/cobro, notificacion "
        "automatica, mensaje interno, etc.)."
    )
    bloques.append("")
    bloques.append("## Contexto del negocio")
    bloques.append(
        "- Edemco vende equipos electricos a empresas (clientes industriales).\n"
        "- Los CLIENTES nos escriben pidiendo precios sobre nuestros productos.\n"
        "- Los PROVEEDORES nos escriben ofreciendonos sus precios (eso NO se registra).\n"
        "- El dominio interno es @edemco.co. Cualquier remitente de ese dominio es interno.\n"
        "- Un correo puede ser un reenvio (RV:) o respuesta (RE:) y aun asi ser una "
        "cotizacion legtima si la SOLICITUD ORIGINAL en el hilo es de un cliente."
    )
    bloques.append("")
    bloques.append(
        "## Indicios de COTIZACION DE CLIENTE (favoreces es_cotizacion_cliente=True)"
    )
    bloques.append(
        "- Lenguaje de solicitud: 'solicitamos cotizacion', 'agradecemos cotizar', "
        "'requerimos su mejor oferta', 'enviar precios de'.\n"
        "- Listado de items, cantidades, especificaciones tecnicas pedidas.\n"
        "- Numero de licitacion o documento del cliente (ej. RFQ, OPM-..., licitacion #).\n"
        "- Remitente de empresa industrial/constructora/minera externa (no nuestro)."
    )
    bloques.append("")
    bloques.append(
        "## Indicios de NO ES cotizacion (favoreces es_cotizacion_cliente=False)"
    )
    bloques.append(
        "- 'con gusto cotizamos', 'adjunto cotizacion para su consideracion', "
        "'nuestra mejor oferta es', 'si salimos favorecidos' -> proveedor.\n"
        "- 'Reunion de Microsoft Teams', 'Unirse a la reunion', 'Id. de reunion' -> reunion.\n"
        "- 'factura electronica', 'pago de factura', 'nota credito' -> cobro.\n"
        "- 'OC aprobada', 'orden de compra', 'remision', 'despacho' -> pedido ya cerrado.\n"
        "- Notificaciones automaticas (sin contenido humano) o cadenas internas sin "
        "cliente externo identificable."
    )
    bloques.append("")
    bloques.append(
        "## Reglas determinsticas que YA estan en el filtro (no las dupliques)"
    )
    bloques.append("Solo veras correos que pasaron estos filtros. Si vas a sugerir una "
                   "regla nueva, asegurate de que el valor NO este en estas listas:")
    bloques.append(f"- keywords requeridas en asunto: {_fmt_list(reglas_vigentes.get('keywords', []))}")
    bloques.append(f"- exclude_subject_contains: {_fmt_list(reglas_vigentes.get('exclude_subject_contains', []))}")
    bloques.append(f"- exclude_body_contains: {_fmt_list(reglas_vigentes.get('exclude_body_contains', []))}")
    bloques.append(f"- exclude_sender_domains: {_fmt_list(reglas_vigentes.get('exclude_sender_domains', []))}")
    bloques.append("")
    bloques.append("## Tipos de regla que puedes sugerir — DOS FAMILIAS")
    bloques.append(
        "INCLUSION (hace que correos con esa palabra ENTREN al sistema):\n"
        "- 'keyword': palabra que si aparece en el ASUNTO -> el correo SE IDENTIFICA como candidato.\n"
        "\n"
        "DESCARTE (hace que correos que cumplan la regla se RECHACEN automaticamente):\n"
        "- 'exclude_subject_contains': texto en ASUNTO -> DESCARTA.\n"
        "- 'exclude_body_contains': texto en CUERPO -> DESCARTA.\n"
        "- 'exclude_sender_domain': dominio del remitente -> DESCARTA TODO correo de ese dominio.\n"
        "- 'exclude_sender_email': email especifico -> DESCARTA todo lo enviado por ese email.\n"
        "- 'supplier_email': email de proveedor (traduce a exclude_sender, DESCARTA).\n"
        "- 'supplier_phrase': frase tipica de proveedor (traduce a exclude_body, DESCARTA)."
    )
    bloques.append("")
    bloques.append(
        "## Formato del campo 'razon' — OBLIGATORIO"
    )
    bloques.append(
        "Tu campo 'razon' debe explicar en lenguaje claro QUE HACE la regla y POR QUE.\n"
        "SIEMPRE comienza la razon con la accion en mayusculas seguida del que/por que:\n"
        "\n"
        "EJEMPLOS BIEN ESCRITOS:\n"
        "- 'DESCARTAR todos los correos enviados desde fusiblesjavisar@hotmail.com — "
        "es un proveedor conocido que envia ofertas de precio, no es cliente.'\n"
        "- 'INCLUIR correos cuyo asunto contiene \"sourcing\" — es jerga comun en "
        "licitaciones que actualmente no esta en la lista de keywords.'\n"
        "- 'DESCARTAR correos cuyo cuerpo contenga \"agradecemos confirmar si salimos "
        "favorecidos\" — frase tipica de proveedor preguntando si fue asignado.'\n"
        "\n"
        "MAL escrito (NO hagas esto):\n"
        "- 'agregar fusiblesjavisar' (no dice que hace).\n"
        "- 'proveedor conocido' (no dice la accion ni el patron exacto)."
    )
    bloques.append("")
    bloques.append(
        "**Calidad de las reglas que sugieres:** una regla buena debe ser ESPECIFICA "
        "(no atraparia cotizaciones legtimas), CARACTERISTICA del tipo de correo "
        "que estas descartando, y GENERALIZABLE (atraparia otros correos similares). "
        "Si dudas, mejor NO sugieras la regla — es preferible escalar al humano."
    )
    bloques.append("")
    bloques.append(
        "## Confianza (calibrala honestamente)"
    )
    bloques.append(
        "- 0.95+ : Es obvio (frase de proveedor explcita, reunion clarsima, etc.).\n"
        "- 0.80-0.94 : Tienes buena evidencia pero hay algo de duda.\n"
        "- 0.50-0.79 : Es ambiguo; ambos lados tienen argumentos.\n"
        "- <0.50 : Realmente no estas seguro; el humano DEBE decidir."
    )

    if ejemplos_humanos:
        bloques.append("")
        bloques.append("## Ejemplos de decisiones humanas previas (usalas como gua)")
        for ej in ejemplos_humanos[:12]:
            asunto = (ej.get("subject") or "")[:90]
            remitente = ej.get("sender_email") or "?"
            decision = ej.get("decision") or "?"
            motivo = (ej.get("motivo") or "").strip()[:200]
            linea = f"- '{asunto}' de {remitente} -> {decision}"
            if motivo:
                linea += f" (motivo humano: {motivo})"
            bloques.append(linea)

    bloques.append("")
    bloques.append(
        "## Tu salida"
    )
    bloques.append(
        "Devuelve un objeto ClasificacionClaude con TODOS los campos. Razona si/no "
        "como steelman de ambos lados (incluso si la respuesta es obvia, da el "
        "mejor argumento contrario). Suggesta reglas SOLO cuando estas alta-mente "
        "confiado del patron."
    )
    return "\n".join(bloques)


# ============================================================================
# Cliente Anthropic
# ============================================================================

class Clasificador:
    """Encapsula el cliente Anthropic y el system prompt cacheado."""

    def __init__(self, api_key: str, model: str = "claude-haiku-4-5"):
        if not api_key:
            raise ValueError("ANTHROPIC_API_KEY vacia")
        self.client = anthropic.Anthropic(api_key=api_key)
        self.model = model
        self._system_prompt: str | None = None

    def preparar_system_prompt(
        self,
        reglas_vigentes: dict[str, list[str]],
        ejemplos_humanos: list[dict] | None = None,
    ) -> None:
        """Construye y memoriza el system prompt para esta corrida. Se invoca
        una sola vez antes del bucle de clasificacion."""
        self._system_prompt = _construir_system_prompt(reglas_vigentes, ejemplos_humanos)
        logger.info(
            "System prompt preparado (%d chars, ~%d tokens estimados)",
            len(self._system_prompt),
            len(self._system_prompt) // 4,
        )

    def clasificar(
        self,
        msg,
        contexto_humano: str | None = None,
    ) -> ClasificacionClaude | None:
        """Clasifica un correo. El parametro contexto_humano permite que un analista
        del equipo pase una pista/instruccion adicional para mejorar la decision.
        Devuelve None si la llamada falla."""
        if self._system_prompt is None:
            raise RuntimeError("preparar_system_prompt() debe llamarse antes")

        user_text = _construir_user_text(msg, contexto_humano)

        try:
            response = self.client.messages.parse(
                model=self.model,
                max_tokens=2048,
                system=[
                    {
                        "type": "text",
                        "text": self._system_prompt,
                        "cache_control": {"type": "ephemeral"},
                    }
                ],
                messages=[{"role": "user", "content": user_text}],
                output_format=ClasificacionClaude,
            )
        except anthropic.APIError as exc:
            logger.warning("Fallo llamando a Claude para %s: %s", msg.id[-22:], exc)
            return None

        return response.parsed_output


# ============================================================================
# Construccion del user message (varia por correo)
# ============================================================================

def _tipo_adjunto(nombre: str) -> str:
    """Etiqueta legible del tipo de archivo."""
    ext = nombre.rsplit(".", 1)[-1].lower() if "." in nombre else ""
    return {
        "pdf": "PDF",
        "xlsx": "Excel", "xls": "Excel",
        "docx": "Word", "doc": "Word",
        "csv": "CSV",
        "png": "imagen", "jpg": "imagen", "jpeg": "imagen",
        "gif": "imagen", "svg": "imagen", "bmp": "imagen",
    }.get(ext, ext or "sin extension")


def _construir_user_text(msg, contexto_humano: str | None = None) -> str:
    """Construye el mensaje de usuario con METADATOS ESTRUCTURADOS + cuerpo.
    El bloque de metadatos da a Claude TODA la informacion que ya extrajimos en
    el pipeline (posicion en hilo, cliente externo, documento, etc.), sin que
    el modelo tenga que inferirla del cuerpo. Mejor consistencia y confianza."""
    L: list[str] = []

    # --- Metadatos del correo ---
    L.append("═══ METADATOS DEL CORREO ═══")
    L.append(f"Buzon receptor:    {msg.mailbox}")
    L.append(f"Remitente:         {msg.sender_name or '?'} <{msg.sender_email}>")
    if msg.is_forwarded_internal:
        L.append("                   ↳ El remitente es del DOMINIO INTERNO @edemco.co "
                 "(probable reenvio interno o respuesta de un colega).")
    L.append(f"Asunto:            {msg.subject}")
    L.append(f"Fecha recepcion:   {msg.received_at}")
    L.append(f"Tiene adjuntos:    {'Si' if msg.has_attachments else 'No'}")

    # --- Posicion en el hilo ---
    L.append("")
    L.append("═══ POSICION EN EL HILO ═══")
    if msg.is_original:
        L.append("Tipo:              ES el ORIGINAL del hilo "
                 "(primer correo de la cadena en este buzon — no es respuesta de algo anterior)")
    else:
        L.append("Tipo:              ES una RESPUESTA/REENVIO de un hilo previo "
                 "(hay correos anteriores en la cadena)")
    if msg.external_client:
        L.append(f"Cliente externo:   {msg.external_client}  "
                 "(identificado como primer remitente externo del hilo)")
    else:
        L.append("Cliente externo:   (no se identifico un cliente externo claro en el hilo)")
    L.append(f"Hilo respondido:   {'Si — alguien interno ya respondio al cliente' if msg.was_replied else 'No'}")
    if msg.replied_at:
        L.append(f"                   ↳ Fecha de respuesta: {msg.replied_at}")
    if msg.chain_source and msg.chain_source != "directo":
        L.append(f"Origen del registro: {msg.chain_source.upper()} "
                 "(fue insertado al detectar una RE en otro buzon, no llego directo)")

    # --- Agrupacion / documento ---
    if msg.doc_ref or msg.group_key:
        L.append("")
        L.append("═══ AGRUPACION ═══")
        if msg.doc_ref:
            L.append(f"Numero de documento detectado: {msg.doc_ref}  "
                     "(p.ej. una licitacion o solicitud consolidada)")
        if msg.group_key:
            L.append(f"Pertenece a una cotizacion consolidada (varios correos del mismo doc).")

    # --- Adjuntos ---
    if msg.has_attachments and msg.attachments:
        L.append("")
        L.append(f"═══ ADJUNTOS ({len(msg.attachments)}) ═══")
        for a in msg.attachments[:10]:
            tamano_kb = a.size // 1024 if a.size else "?"
            L.append(f"  - {a.name}  [{_tipo_adjunto(a.name)}, {tamano_kb} KB]")
    elif msg.has_attachments:
        L.append("")
        L.append("═══ ADJUNTOS ═══")
        L.append("(hay adjuntos pero no se descargaron en esta corrida)")

    # --- Original detectado ---
    if msg.original_source == "hilo" and msg.original_snippet:
        L.append("")
        L.append("═══ SOLICITUD ORIGINAL DETECTADA EN EL HILO ═══")
        L.append(f"Fecha del original: {msg.original_received_at or '?'}")
        L.append("Snippet:")
        L.append((msg.original_snippet or "")[:1500])
    elif msg.original_source == "cuerpo_citado" and msg.original_snippet:
        L.append("")
        L.append("═══ ORIGINAL EMBEBIDO EN EL CUERPO (RV con hilo roto) ═══")
        if msg.original_received_at:
            L.append(f"Fecha del original: {msg.original_received_at}")
        L.append("Texto extraido del bloque citado:")
        L.append((msg.original_snippet or "")[:1500])

    # --- Contexto humano (si lo hay) ---
    if contexto_humano:
        L.append("")
        L.append("═══ ⚠️ CONTEXTO/PISTA DEL ANALISTA HUMANO ═══")
        L.append("Un analista del equipo Edemco te esta pasando ESTE contexto adicional "
                 "para clasificar este correo. PRESTALE atencion y referencialo en tu "
                 "razonamiento:")
        L.append("")
        L.append(contexto_humano.strip())

    # --- Cuerpo ---
    L.append("")
    L.append("═══ CUERPO DEL CORREO ═══")
    L.append((msg.body_text or "")[:6000])

    return "\n".join(L)
