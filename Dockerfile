# ---- Imagen base ----
# Python 3.12 slim: la app requiere 3.10+ (usa sintaxis `int | None`).
FROM python:3.12-slim AS base

# Evita .pyc y fuerza logs sin buffer (para ver salida de rich/logging en tiempo real)
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    TZ=America/Bogota

WORKDIR /app

# ---- Dependencias (capa cacheada) ----
# Se copia requirements.txt primero para reutilizar la capa mientras no cambie.
COPY requirements.txt ./
RUN pip install --upgrade pip && \
    pip install -r requirements.txt

# ---- Codigo de la aplicacion ----
COPY src/ ./src/
COPY config/ ./config/

# Directorios de datos persistentes (se montan como volumenes en runtime).
# config.py resuelve estas rutas relativas a la raiz del proyecto (/app).
RUN mkdir -p /app/data /app/attachments

# Usuario no-root con permisos sobre /app
RUN useradd --create-home --uid 10001 appuser && \
    chown -R appuser:appuser /app
USER appuser

# La app es un CLI: el entrypoint es el modulo y el comando se pasa como argumento.
#   docker run <img> run --dry-run
#   docker run <img> list-pending
ENTRYPOINT ["python", "-m", "src.main"]
CMD ["--help"]
