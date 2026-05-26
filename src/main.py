"""CLI principal: lista solicitudes de cotizacion entrantes y su estado."""
from __future__ import annotations

import logging
import sys
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd
import typer
from rich.console import Console
from rich.logging import RichHandler
from rich.table import Table
from sqlalchemy import desc, select
from sqlalchemy.orm import Session

if sys.platform == "win32":
    try:
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stderr.reconfigure(encoding="utf-8")
    except (AttributeError, OSError):
        pass

from .config import load_settings
from .graph_client import GraphClient
from .mail_reader import MailReader, Message
from .storage import QuoteRequestRow, init_db, upsert_quote
from .summarizer import resumir

app = typer.Typer(
    add_completion=False,
    help="Lector de buzones M365: identifica cotizaciones entrantes y verifica si fueron respondidas.",
)
console = Console()


def _dias_desde(received_at_iso: str) -> int | None:
    """Cuantos dias enteros pasaron desde la fecha recibida hasta hoy."""
    if not received_at_iso:
        return None
    try:
        dt = datetime.fromisoformat(received_at_iso.replace("Z", "+00:00"))
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    now = datetime.now(timezone.utc)
    delta = now - dt
    return max(delta.days, 0)


def _color_dias(d: int | None) -> str:
    if d is None:
        return "[dim]-[/dim]"
    if d <= 1:
        return f"[green]{d}d[/green]"
    if d <= 3:
        return f"[yellow]{d}d[/yellow]"
    return f"[red bold]{d}d[/red bold]"


def _setup_logging(level: str) -> None:
    logging.basicConfig(
        level=level,
        format="%(message)s",
        datefmt="[%X]",
        handlers=[RichHandler(console=console, rich_tracebacks=True, show_path=False)],
    )
    for noisy in ("httpx", "httpcore", "msal", "urllib3"):
        logging.getLogger(noisy).setLevel(logging.WARNING)


def _guardar(session: Session, msg: Message) -> QuoteRequestRow:
    existente = upsert_quote(session, msg.id)
    if existente:
        # Actualizar estado de respuesta (puede haber cambiado desde la ultima corrida)
        existente.was_replied = msg.was_replied
        existente.replied_at = msg.replied_at
        existente.external_client = msg.external_client
        existente.is_forwarded_internal = msg.is_forwarded_internal
        session.commit()
        return existente

    resumen = resumir(msg.body_text, [a.name for a in msg.attachments])

    fila = QuoteRequestRow(
        graph_id=msg.id,
        conversation_id=msg.conversation_id,
        mailbox=msg.mailbox,
        subject=msg.subject,
        sender_email=msg.sender_email,
        sender_name=msg.sender_name,
        received_at=msg.received_at,
        snippet=resumen.snippet,
        num_items=resumen.num_items_estimado,
        was_replied=msg.was_replied,
        replied_at=msg.replied_at,
        external_client=msg.external_client,
        is_forwarded_internal=msg.is_forwarded_internal,
    )
    fila.cantidades = resumen.cantidades_mencionadas
    fila.adjuntos = resumen.adjuntos
    session.add(fila)
    session.commit()
    return fila


@app.command()
def run(
    dry_run: bool = typer.Option(False, "--dry-run", help="No guarda en BD."),
    no_attachments: bool = typer.Option(
        False, "--no-attachments", help="No descargar adjuntos (mas rapido)."
    ),
) -> None:
    """Lee buzones, identifica cotizaciones y verifica cuales no han sido respondidas."""
    settings = load_settings()
    _setup_logging(settings.log_level)

    if not settings.mailboxes:
        console.print("[red]No hay MAILBOXES configurados en .env[/red]")
        raise typer.Exit(code=1)

    engine = init_db(settings.database_path)

    with GraphClient(settings.tenant_id, settings.client_id, settings.client_secret) as client:
        reader = MailReader(client, settings)
        with Session(engine) as session:
            for mailbox in settings.mailboxes:
                console.rule(f"[bold]Buzon: {mailbox}")
                try:
                    messages = reader.fetch_relevant_messages(
                        mailbox, download_attachments=not no_attachments
                    )
                except Exception as exc:
                    console.print(f"[red]Error leyendo {mailbox}: {exc}[/red]")
                    continue

                nuevas = 0
                actualizadas = 0
                if messages and not dry_run:
                    for msg in messages:
                        existia = upsert_quote(session, msg.id) is not None
                        _guardar(session, msg)
                        if existia:
                            actualizadas += 1
                        else:
                            nuevas += 1

                # Resumen consultando la BD (toda la historia acumulada del buzon)
                _mostrar_dashboard_db(session, mailbox, nuevas, actualizadas)

    console.print("\n[bold green]Listo.[/bold green] Usa [cyan]list-pending[/cyan] para ver las pendientes.")


def _mostrar_dashboard_db(
    session: Session, mailbox: str, nuevas: int, actualizadas: int
) -> None:
    """Lee todas las cotizaciones del buzon desde la BD (historico acumulado)."""
    rows = session.execute(
        select(QuoteRequestRow).where(QuoteRequestRow.mailbox == mailbox)
    ).scalars().all()

    total = len(rows)
    if total == 0:
        console.print("  [dim]Sin cotizaciones en historia para este buzon.[/dim]")
        return

    respondidas = sum(1 for r in rows if r.was_replied)
    pendientes_rows = [r for r in rows if not r.was_replied]
    pendientes = len(pendientes_rows)
    pct_resp = (respondidas / total * 100) if total else 0

    dias_pend = [
        d for r in pendientes_rows
        if (d := _dias_desde(r.received_at)) is not None
    ]
    prom_dias = (sum(dias_pend) / len(dias_pend)) if dias_pend else 0
    mas_antigua_dias = max(dias_pend) if dias_pend else 0
    urgentes = sum(1 for d in dias_pend if d > 3)
    criticas = sum(1 for d in dias_pend if d > 7)

    console.print()
    console.print(f"  [bold cyan]Resumen del buzon[/bold cyan] {mailbox}:")
    console.print(
        f"    Esta corrida:  [bold green]+{nuevas}[/bold green] nuevas, "
        f"[dim]{actualizadas} actualizadas[/dim]"
    )
    console.print(f"    [bold]Acumulado en BD[/bold] (toda la historia):")
    console.print(f"      Total identificadas:                 [bold]{total}[/bold]")
    console.print(
        f"      [green]Respondidas al cliente:[/green]   [bold]{respondidas}[/bold]  "
        f"([dim]{pct_resp:.0f}%[/dim])"
    )
    console.print(
        f"      [yellow]PENDIENTES de responder:[/yellow]  [bold]{pendientes}[/bold]  "
        f"([dim]{100 - pct_resp:.0f}%[/dim])"
    )
    if dias_pend:
        console.print(
            f"        [dim]· Espera promedio:[/dim]            "
            f"[bold]{prom_dias:.1f} dias[/bold]"
        )
        console.print(
            f"        [dim]· Mas antigua:[/dim]                "
            f"{_color_dias(mas_antigua_dias)}"
        )
        if urgentes:
            console.print(
                f"        [red]· Mas de 3 dias sin responder:[/red]   "
                f"[bold red]{urgentes}[/bold red]"
            )
        if criticas:
            console.print(
                f"        [bold red]· CRITICAS (>7 dias):[/bold red]            "
                f"[bold red on white] {criticas} [/bold red on white]"
            )


@app.command("list-pending")
def list_pending(limit: int = typer.Option(50, help="Cantidad maxima a mostrar.")) -> None:
    """Lista las cotizaciones que aun NO han sido respondidas."""
    _listar(only_pending=True, limit=limit, titulo="Cotizaciones PENDIENTES de responder")


@app.command("list-all")
def list_all(limit: int = typer.Option(50, help="Cantidad maxima a mostrar.")) -> None:
    """Lista todas las cotizaciones procesadas (respondidas y pendientes)."""
    _listar(only_pending=False, limit=limit, titulo="Todas las cotizaciones procesadas")


def _listar(only_pending: bool, limit: int, titulo: str) -> None:
    settings = load_settings()
    _setup_logging(settings.log_level)
    engine = init_db(settings.database_path)

    with Session(engine) as session:
        stmt = (
            select(QuoteRequestRow)
            .order_by(desc(QuoteRequestRow.received_at))
            .limit(limit)
        )
        if only_pending:
            stmt = stmt.where(QuoteRequestRow.was_replied.is_(False))
        rows = session.execute(stmt).scalars().all()

    # Ordenar pendientes primero por dias descendentes (las mas viejas arriba)
    rows_sorted = sorted(
        rows,
        key=lambda r: (
            r.was_replied,
            -(_dias_desde(r.received_at) or 0),
        ),
    )

    table = Table(title=titulo, show_lines=True)
    table.add_column("ID", justify="right")
    table.add_column("Dias", justify="right", no_wrap=True)
    table.add_column("Recibido", no_wrap=True)
    table.add_column("Cliente externo", overflow="fold", max_width=24)
    table.add_column("Asunto", overflow="fold", max_width=38)
    table.add_column("Llego de", overflow="fold", max_width=22)
    table.add_column("Estado", no_wrap=True)

    pend = resp = 0
    dias_pendientes: list[int] = []
    for r in rows_sorted:
        if r.was_replied:
            estado = "[green]RESPONDIDA[/green]"
            resp += 1
        else:
            estado = "[yellow]PENDIENTE[/yellow]"
            pend += 1

        dias = _dias_desde(r.received_at)
        if not r.was_replied and dias is not None:
            dias_pendientes.append(dias)

        cliente_txt = r.external_client or "[dim]no identificado[/dim]"
        llego_de = r.sender_email
        if r.is_forwarded_internal:
            llego_de = f"[cyan](reenvio)[/cyan] {llego_de}"

        table.add_row(
            str(r.id),
            _color_dias(dias),
            (r.received_at or "")[:10],
            cliente_txt,
            r.subject or "[dim](sin asunto)[/dim]",
            llego_de,
            estado,
        )
    console.print(table)
    resumen = (
        f"\n[bold]{len(rows_sorted)}[/bold] registros   "
        f"([yellow]{pend} pendientes[/yellow], [green]{resp} respondidas[/green])"
    )
    if dias_pendientes:
        prom = sum(dias_pendientes) / len(dias_pendientes)
        mayor = max(dias_pendientes)
        resumen += (
            f"\n[bold]Espera promedio:[/bold] {prom:.1f} dias   "
            f"[bold]Mas antigua:[/bold] {_color_dias(mayor)}"
        )
    console.print(resumen)


@app.command()
def show(quote_id: int) -> None:
    """Muestra el detalle completo de una cotizacion."""
    settings = load_settings()
    _setup_logging(settings.log_level)
    engine = init_db(settings.database_path)

    with Session(engine) as session:
        r = session.get(QuoteRequestRow, quote_id)
        if not r:
            console.print(f"[red]No existe la cotizacion {quote_id}[/red]")
            raise typer.Exit(code=1)

        console.rule(f"Cotizacion #{r.id}")
        console.print(f"[bold]Asunto:[/bold] {r.subject}")
        console.print(f"[bold]Cliente externo:[/bold] {r.external_client or '[dim]no identificado[/dim]'}")
        marca = " [cyan](reenvio interno)[/cyan]" if r.is_forwarded_internal else ""
        console.print(f"[bold]Llego de:[/bold] {r.sender_name} <{r.sender_email}>{marca}")
        console.print(f"[bold]Buzon receptor:[/bold] {r.mailbox}")
        console.print(f"[bold]Recibido:[/bold] {r.received_at}")
        if r.was_replied:
            console.print(f"[bold green]RESPONDIDA AL CLIENTE[/bold green] el {r.replied_at}")
        else:
            console.print("[bold yellow]PENDIENTE de responder al cliente[/bold yellow]")

        console.print("\n[bold]Resumen de la solicitud:[/bold]")
        console.print(f"  {r.snippet or '[dim](sin texto extraido)[/dim]'}")

        if r.cantidades:
            console.print("\n[bold]Cantidades mencionadas:[/bold]")
            for c in r.cantidades:
                console.print(f"  - {c}")

        if r.adjuntos:
            console.print("\n[bold]Adjuntos:[/bold]")
            for a in r.adjuntos:
                console.print(f"  - {a}")


@app.command()
def export(
    output: str = typer.Option(
        None, "--output", "-o", help="Ruta del archivo .xlsx. Por defecto: cotizaciones_pendientes_YYYYMMDD.xlsx"
    ),
    incluir_respondidas: bool = typer.Option(
        False, "--all", help="Incluir tambien las respondidas (por defecto solo pendientes)."
    ),
) -> None:
    """Exporta las cotizaciones PENDIENTES a Excel, ordenadas por antiguedad."""
    settings = load_settings()
    _setup_logging(settings.log_level)
    engine = init_db(settings.database_path)

    with Session(engine) as session:
        stmt = select(QuoteRequestRow).order_by(desc(QuoteRequestRow.received_at))
        if not incluir_respondidas:
            stmt = stmt.where(QuoteRequestRow.was_replied.is_(False))
        rows = session.execute(stmt).scalars().all()

    if not rows:
        console.print("[yellow]No hay cotizaciones para exportar.[/yellow]")
        raise typer.Exit(code=0)

    # Ordenar: pendientes mas antiguas primero
    rows = sorted(
        rows,
        key=lambda r: (r.was_replied, -(_dias_desde(r.received_at) or 0)),
    )

    data = []
    for r in rows:
        dias = _dias_desde(r.received_at)
        data.append({
            "ID": r.id,
            "Dias sin responder": dias if (dias is not None and not r.was_replied) else "",
            "Recibido": (r.received_at or "").replace("T", " ").replace("Z", ""),
            "Cliente externo": r.external_client or "",
            "Asunto": r.subject,
            "Llego de (remitente)": r.sender_email,
            "Es reenvio interno": "SI" if r.is_forwarded_internal else "NO",
            "Nombre remitente": r.sender_name,
            "Estado": "RESPONDIDA AL CLIENTE" if r.was_replied else "PENDIENTE",
            "Respondido en": (r.replied_at or "").replace("T", " ").replace("+00:00", "")
            if r.was_replied else "",
            "Buzon": r.mailbox,
            "Items": r.num_items,
            "Resumen": r.snippet,
            "Cantidades mencionadas": ", ".join(r.cantidades) if r.cantidades else "",
            "Adjuntos": ", ".join(r.adjuntos) if r.adjuntos else "",
        })
    df = pd.DataFrame(data)

    if output is None:
        suffix = "" if incluir_respondidas else "_pendientes"
        output = f"cotizaciones{suffix}_{datetime.now():%Y%m%d_%H%M}.xlsx"
    out_path = Path(output).resolve()

    with pd.ExcelWriter(out_path, engine="openpyxl") as writer:
        df.to_excel(writer, sheet_name="Cotizaciones", index=False)
        # Auto-ajustar ancho de columnas
        ws = writer.sheets["Cotizaciones"]
        for col_idx, col_name in enumerate(df.columns, start=1):
            max_len = max(
                len(str(col_name)),
                df[col_name].astype(str).map(len).max() if len(df) else 0,
            )
            ws.column_dimensions[ws.cell(row=1, column=col_idx).column_letter].width = min(
                max_len + 2, 60
            )
        # Resaltar header
        from openpyxl.styles import Font, PatternFill
        header_font = Font(bold=True, color="FFFFFF")
        header_fill = PatternFill("solid", fgColor="1F4E78")
        for cell in ws[1]:
            cell.font = header_font
            cell.fill = header_fill
        ws.freeze_panes = "A2"

        # Colorear "Dias sin responder" segun antiguedad
        if "Dias sin responder" in df.columns:
            col_idx = df.columns.get_loc("Dias sin responder") + 1
            fill_red = PatternFill("solid", fgColor="F8CBAD")     # rojo claro
            fill_yellow = PatternFill("solid", fgColor="FFE699")  # amarillo claro
            fill_green = PatternFill("solid", fgColor="C6EFCE")   # verde claro
            for row_idx in range(2, len(df) + 2):
                cell = ws.cell(row=row_idx, column=col_idx)
                v = cell.value
                if isinstance(v, (int, float)):
                    if v > 3:
                        cell.fill = fill_red
                        cell.font = Font(bold=True)
                    elif v > 1:
                        cell.fill = fill_yellow
                    else:
                        cell.fill = fill_green

    total = len(rows)
    pend = sum(1 for r in rows if not r.was_replied)
    console.print(
        f"\n[bold green]Exportado:[/bold green] {out_path}\n"
        f"  Filas: {total}  ([yellow]{pend} pendientes[/yellow], "
        f"[green]{total - pend} respondidas[/green])"
    )


if __name__ == "__main__":
    app()
