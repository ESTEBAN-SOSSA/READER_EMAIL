"""UI Streamlit para revisar las decisiones de Claude y aprobar/rechazar
cotizaciones + aprobar reglas semanticas sugeridas.

Corre como servicio paralelo al `reader` (mismo contenedor, comando distinto).
Comparte la BD SQLite via volumen Docker.

Lanzar (dentro del contenedor):
    streamlit run ui/reviewer.py --server.address=0.0.0.0 --server.port=8501
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

# Asegurar que /app esta en sys.path para importar `src.*`
APP_DIR = Path(__file__).resolve().parent.parent
if str(APP_DIR) not in sys.path:
    sys.path.insert(0, str(APP_DIR))

import streamlit as st
from sqlalchemy.orm import Session

from src.config import load_settings
from src.storage import (
    ClaudeReview,
    LearnedRule,
    add_learned_rule,
    apply_learned_rules_to_settings,
    deserialize_message,
    get_pending_reviews,
    get_recent_human_examples,
    init_db,
    mark_processed,
    propagar_a_copias_logicas,
    record_human_decision,
    update_claude_review_con_contexto,
)
from sqlalchemy import func, select
from src.main import guardar_aprobada


# ============================================================================
# Setup
# ============================================================================

st.set_page_config(
    page_title="Revisión cotizaciones — Claude",
    page_icon="📬",
    layout="wide",
)

settings = load_settings()
engine = init_db(settings.database_path)


# ============================================================================
# Helpers
# ============================================================================

TIPO_EMOJI = {
    "cliente_solicita": "👤",
    "proveedor_responde": "🏭",
    "reunion_teams": "📅",
    "interno_administrativo": "📝",
    "cobro_factura": "💰",
    "automatico_notificacion": "🤖",
    "otro": "❓",
}

# Mapeo de tipos de regla → etiqueta amigable que deja claro QUÉ HACE la regla.
RULE_LABELS = {
    "keyword": ("✅", "INCLUIR — palabra clave en el asunto", "green"),
    "exclude_subject_contains": ("🚫", "DESCARTAR — texto en ASUNTO", "red"),
    "exclude_body_contains": ("🚫", "DESCARTAR — texto en CUERPO", "red"),
    "exclude_sender_domain": ("🚫", "DESCARTAR — DOMINIO de remitente", "red"),
    "exclude_sender_email": ("🚫", "DESCARTAR — EMAIL específico", "red"),
    "supplier_email": ("🚫", "DESCARTAR — email de PROVEEDOR conocido", "red"),
    "supplier_phrase": ("🚫", "DESCARTAR — frase típica de PROVEEDOR", "red"),
}


@st.cache_resource
def _get_clasificador():
    """Crea (y cachea) el cliente Claude para reanálisis bajo demanda."""
    from src.clasificador import Clasificador
    s = load_settings()
    if not s.anthropic_api_key:
        return None, None
    return Clasificador(api_key=s.anthropic_api_key, model=s.anthropic_model), s


def _reclasificar_con_contexto(review: ClaudeReview, contexto: str) -> bool:
    """Llama a Claude de nuevo con el contexto humano. Retorna True si tuvo éxito."""
    clasif, s = _get_clasificador()
    if clasif is None:
        st.error("ANTHROPIC_API_KEY no configurada en .env")
        return False
    try:
        msg = deserialize_message(review.message_json)
    except Exception as exc:
        st.error(f"No se pudo reconstruir el mensaje: {exc}")
        return False
    with Session(engine) as session:
        # Mezclamos reglas vigentes (yaml + learned) + ejemplos few-shot actuales
        apply_learned_rules_to_settings(session, s)
        ejemplos = get_recent_human_examples(session, limit=12)
    clasif.preparar_system_prompt(
        reglas_vigentes={
            "keywords": s.keywords,
            "exclude_subject_contains": s.exclude_subject_contains,
            "exclude_body_contains": s.exclude_body_contains,
            "exclude_sender_domains": s.exclude_sender_domains,
        },
        ejemplos_humanos=ejemplos,
    )
    nuevo = clasif.clasificar(msg, contexto_humano=contexto)
    if nuevo is None:
        st.error("Claude devolvió error (rate limit o fallo de red). Intenta de nuevo en unos segundos.")
        return False
    with Session(engine) as session:
        update_claude_review_con_contexto(session, msg, nuevo, contexto)
    return True


def _aplicar_decision(
    review: ClaudeReview,
    es_cotizacion: bool,
    motivo: str,
    reglas_aprobadas: list[dict],
    reviewer: str = "UI",
) -> None:
    """Persiste la decisión humana: registra en claude_reviews, guarda o
    descarta en quote_requests/processed_messages y agrega reglas aprendidas."""
    with Session(engine) as session:
        record_human_decision(
            session, review.graph_id, es_cotizacion, motivo, reviewed_by=reviewer
        )
        parent_quote_id = None
        if es_cotizacion:
            try:
                msg = deserialize_message(review.message_json)
                parent = guardar_aprobada(session, msg)
                parent_quote_id = parent.id if parent else None
                mark_processed(
                    session, review.graph_id, review.mailbox, review.subject,
                    "identificado", "aprobado_humano",
                )
            except Exception as exc:
                st.error(f"Error guardando cotización: {exc}")
                return
        else:
            mark_processed(
                session, review.graph_id, review.mailbox, review.subject,
                "descartado", f"rechazado_humano: {motivo[:100]}",
            )
        # Propagar la MISMA decision a las copias logicas del grupo (mismo asunto
        # + remitente reenviado varias veces): el humano revisa una sola vez.
        n_copias = propagar_a_copias_logicas(
            session, review.graph_id, es_cotizacion, parent_quote_id
        )
        if n_copias:
            st.caption(
                f"↳ Decisión aplicada también a {n_copias} copia(s) lógica(s) del grupo."
            )
        for regla in reglas_aprobadas:
            add_learned_rule(
                session,
                tipo=regla["tipo"],
                valor=regla["valor"],
                razon=regla.get("razon", ""),
                fuente_graph_id=review.graph_id,
                aprobada_por=reviewer,
            )
        session.commit()


def _confianza_color(conf: float) -> str:
    if conf >= 0.85:
        return "green"
    if conf >= 0.6:
        return "orange"
    return "red"


# ============================================================================
# UI
# ============================================================================

st.title("📬 Revisión de cotizaciones — Claude AI")
st.caption(
    "Claude clasifica los correos que pasaron los filtros determinísticos y propone "
    "reglas para enriquecer el filtro. Tú confirmas / corriges / apruebas reglas."
)

# Reviewer name
with st.sidebar:
    st.header("👤 Revisor")
    reviewer = st.text_input("Tu nombre/usuario", value=st.session_state.get("reviewer", ""))
    if reviewer:
        st.session_state["reviewer"] = reviewer

    st.divider()
    st.header("📊 Estado")
    with Session(engine) as _s:
        total_pend = len(get_pending_reviews(_s, limit=10000))
        from sqlalchemy import select, func
        total_aprobadas = _s.execute(
            select(func.count(ClaudeReview.graph_id)).where(ClaudeReview.estado == "aprobado")
        ).scalar() or 0
        total_rechazadas = _s.execute(
            select(func.count(ClaudeReview.graph_id)).where(ClaudeReview.estado == "rechazado")
        ).scalar() or 0
        total_reglas = _s.execute(
            select(func.count(LearnedRule.id)).where(LearnedRule.activa.is_(True))
        ).scalar() or 0
    st.metric("Pendientes", total_pend)
    st.metric("Aprobadas", total_aprobadas)
    st.metric("Rechazadas", total_rechazadas)
    st.metric("Reglas aprendidas activas", total_reglas)

    st.divider()
    if st.button("🔄 Refrescar"):
        st.rerun()

# Tabs principales
tab_pending, tab_learned, tab_history = st.tabs(
    ["⏳ Pendientes", "📋 Reglas aprendidas", "📜 Histórico"]
)

# ----------------------------------------------------------------------------
# TAB: Pendientes
# ----------------------------------------------------------------------------
with tab_pending:
    if not reviewer:
        st.warning("👈 Pon tu nombre en la barra lateral antes de aprobar.")
        st.stop()

    with Session(engine) as session:
        pendientes = get_pending_reviews(session, limit=50)
        # Cuantas copias logicas cuelga cada representante (mismo asunto+remitente
        # reenviado varias veces): se decide una vez y aplica a todas.
        copias_por_rep = dict(
            session.execute(
                select(
                    ClaudeReview.representante_graph_id,
                    func.count(ClaudeReview.graph_id),
                )
                .where(ClaudeReview.estado == "copia_logica")
                .group_by(ClaudeReview.representante_graph_id)
            ).all()
        )

    if not pendientes:
        st.success("✅ No hay correos pendientes de revisión. ¡Buen trabajo!")
    else:
        st.info(f"**{len(pendientes)}** correo(s) esperando tu decisión.")

    for review in pendientes:
        veredicto_str = "ES cotización" if review.es_cotizacion_cliente else "NO es cotización"
        veredicto_color = "🟢" if review.es_cotizacion_cliente else "🔴"
        emoji = TIPO_EMOJI.get(review.tipo, "❓")
        conf_color = _confianza_color(review.confianza)

        with st.container(border=True):
            col_h1, col_h2 = st.columns([4, 1])
            with col_h1:
                st.markdown(f"### {emoji} {review.subject[:120]}")
                st.caption(
                    f"De **{review.sender_email}** · Buzón **{review.mailbox}** · "
                    f"Recibido {review.received_at}"
                )
                _n_copias = copias_por_rep.get(review.graph_id, 0)
                if _n_copias:
                    st.caption(
                        f"🧩 **+{_n_copias} copia(s) lógica(s)** del mismo asunto/remitente "
                        f"reenviadas a otros buzones — tu decisión aplica a todas."
                    )
            with col_h2:
                st.markdown(
                    f"**{veredicto_color} {veredicto_str}**  \n"
                    f"`{review.tipo}`  \n"
                    f":{conf_color}[confianza {review.confianza:.0%}]"
                )

            # Razonamiento bilateral
            with st.expander("🤖 Razonamiento de Claude", expanded=True):
                col_si, col_no = st.columns(2)
                with col_si:
                    st.markdown("**✅ Por qué SÍ podría serlo**")
                    st.info(review.razonamiento_si or "(sin argumentos)")
                with col_no:
                    st.markdown("**❌ Por qué NO lo es**")
                    st.warning(review.razonamiento_no or "(sin argumentos)")
                st.markdown("**🎯 Veredicto final de Claude**")
                st.markdown(f"> {review.veredicto_final}")

            # Cuerpo del correo
            with st.expander("📄 Ver cuerpo del correo"):
                try:
                    msg_data = json.loads(review.message_json or "{}")
                except json.JSONDecodeError:
                    msg_data = {}
                body = msg_data.get("body_text") or msg_data.get("body_preview") or "(vacío)"
                st.text_area(
                    "Cuerpo",
                    body[:5000],
                    height=200,
                    key=f"body_{review.graph_id}",
                    disabled=True,
                )
                if msg_data.get("attachments"):
                    st.caption(
                        f"📎 Adjuntos: {', '.join(a['name'] for a in msg_data['attachments'])}"
                    )

            # Reglas sugeridas (con etiquetas legibles que aclaran QUÉ HACE cada regla)
            try:
                reglas = json.loads(review.reglas_sugeridas_json or "[]")
            except json.JSONDecodeError:
                reglas = []
            reglas_aprobadas = []
            if reglas:
                st.markdown("**📋 Reglas que Claude propone agregar al filtro determinístico**")
                st.caption(
                    "Lee bien la acción de cada regla. 🚫 DESCARTAR = el correo se rechazará "
                    "automáticamente sin pasar por Claude. ✅ INCLUIR = nuevo candidato al sistema."
                )
                for i, regla in enumerate(reglas):
                    emoji, label, color = RULE_LABELS.get(
                        regla["tipo"], ("❓", regla["tipo"], "gray")
                    )
                    aprobada = st.checkbox(
                        f"{emoji} **{label}** → `{regla['valor']}`",
                        key=f"rule_{review.graph_id}_{i}",
                    )
                    if regla.get("razon"):
                        st.caption(f"  💭 _{regla['razon']}_")
                    if regla.get("ejemplos_apoyo"):
                        st.caption(f"  📎 También cubriría: {', '.join(regla['ejemplos_apoyo'][:3])}")
                    if aprobada:
                        reglas_aprobadas.append(regla)
            else:
                st.caption("_(Claude no propuso reglas para este correo)_")

            # --- Zona de contexto humano + reanálisis ---
            st.divider()
            st.markdown("**💬 Tu contexto/pista para Claude (opcional, antes de decidir)**")
            if review.contexto_humano:
                st.info(f"📝 Contexto previo que ya le diste: _{review.contexto_humano}_")
            contexto = st.text_area(
                "Si tienes información que ayudaría a Claude a clasificar mejor este correo, escríbela aquí. Luego pulsa 'Reanalizar' y Claude usará tu pista en el razonamiento.",
                key=f"contexto_{review.graph_id}",
                placeholder=(
                    "Ej: 'fusiblesjavisar es nuestro proveedor histórico de fusibles' / "
                    "'el código GH-XXXX al inicio del asunto es del cliente Haceb, siempre cotizable' / "
                    "'el dominio sourcing@cemex.coupahost.com es plataforma de licitaciones de Cemex'"
                ),
                height=80,
                label_visibility="visible",
            )
            col_reanalizar, col_spacer = st.columns([2, 4])
            if col_reanalizar.button(
                "🔄 Reanalizar con este contexto",
                key=f"reanalize_{review.graph_id}",
                disabled=not contexto.strip(),
                help="Llama a Claude de nuevo pasándole tu pista para que la incluya en su razonamiento.",
            ):
                with st.spinner("Claude está reanalizando..."):
                    ok = _reclasificar_con_contexto(review, contexto)
                if ok:
                    st.success("✅ Reanálisis listo. El veredicto se actualizó arriba.")
                    st.rerun()

            st.divider()

            # Acciones
            motivo = st.text_input(
                "Comentario (opcional)",
                key=f"motivo_{review.graph_id}",
                placeholder="¿Por qué? (queda registrado para mejorar el sistema)",
            )
            col_a, col_b, col_c = st.columns(3)
            if col_a.button(
                f"✅ Confirmar veredicto ({veredicto_str})",
                key=f"approve_{review.graph_id}",
                type="primary",
                use_container_width=True,
            ):
                _aplicar_decision(
                    review, review.es_cotizacion_cliente, motivo, reglas_aprobadas, reviewer
                )
                st.rerun()
            override_label = "🔄 NO es" if review.es_cotizacion_cliente else "🔄 SÍ es"
            override_decision = not review.es_cotizacion_cliente
            if col_b.button(
                f"{override_label} cotización",
                key=f"override_{review.graph_id}",
                use_container_width=True,
            ):
                _aplicar_decision(
                    review, override_decision, motivo or "override manual", reglas_aprobadas, reviewer
                )
                st.rerun()
            if col_c.button(
                "⏭️ Saltar (lo veo después)",
                key=f"skip_{review.graph_id}",
                use_container_width=True,
            ):
                pass  # no-op; ya viene en el siguiente refresh

# ----------------------------------------------------------------------------
# TAB: Reglas aprendidas
# ----------------------------------------------------------------------------
with tab_learned:
    st.subheader("Reglas aprendidas por aprobación humana")
    st.caption(
        "Estas reglas se aplican AUTOMÁTICAMENTE en cada corrida del lector. "
        "Puedes desactivarlas (toggle off) si una resulta ser falsa positiva."
    )
    with Session(engine) as session:
        from sqlalchemy import select
        reglas = session.execute(
            select(LearnedRule).order_by(LearnedRule.aprobada_en.desc())
        ).scalars().all()

        if not reglas:
            st.info("No hay reglas aprendidas todavía. Aparecerán aquí a medida que apruebes sugerencias de Claude.")
        else:
            for r in reglas:
                col_main, col_toggle = st.columns([5, 1])
                with col_main:
                    icono = "🟢" if r.activa else "⚪"
                    st.markdown(
                        f"{icono} **`{r.tipo}`**: `{r.valor}`"
                    )
                    st.caption(
                        f"_{r.razon}_  ·  Aprobada por **{r.aprobada_por}** "
                        f"el {r.aprobada_en.strftime('%Y-%m-%d %H:%M')}"
                    )
                with col_toggle:
                    nuevo_estado = st.toggle(
                        "Activa", value=r.activa, key=f"toggle_rule_{r.id}"
                    )
                    if nuevo_estado != r.activa:
                        r.activa = nuevo_estado
                        session.commit()
                        st.rerun()

# ----------------------------------------------------------------------------
# TAB: Histórico
# ----------------------------------------------------------------------------
with tab_history:
    st.subheader("Decisiones previas (últimas 50)")
    with Session(engine) as session:
        from sqlalchemy import select
        historicas = session.execute(
            select(ClaudeReview)
            .where(ClaudeReview.estado.in_(["aprobado", "rechazado"]))
            .order_by(ClaudeReview.reviewed_at.desc())
            .limit(50)
        ).scalars().all()

    if not historicas:
        st.info("Sin decisiones registradas aún.")
    else:
        import pandas as pd
        df = pd.DataFrame([
            {
                "Asunto": (r.subject or "")[:60],
                "Remitente": r.sender_email,
                "Claude dijo": "SÍ" if r.es_cotizacion_cliente else "NO",
                "Confianza": f"{r.confianza:.0%}",
                "Humano dijo": "SÍ" if r.human_decision_es_cotizacion else "NO",
                "¿Coincide?": "✅" if r.es_cotizacion_cliente == r.human_decision_es_cotizacion else "❌",
                "Por": r.reviewed_by or "?",
                "Cuándo": r.reviewed_at.strftime("%Y-%m-%d %H:%M") if r.reviewed_at else "?",
                "Motivo": (r.human_motivo or "")[:80],
            }
            for r in historicas
        ])
        st.dataframe(df, use_container_width=True, hide_index=True)

        coincidencias = sum(
            1 for r in historicas
            if r.es_cotizacion_cliente == r.human_decision_es_cotizacion
        )
        total = len(historicas)
        pct = (coincidencias / total * 100) if total else 0
        st.metric(
            "Acuerdo Claude ↔ humano",
            f"{pct:.1f}%",
            help="Si este % sube consistentemente, podemos pensar en auto-aprobar casos de alta confianza.",
        )
