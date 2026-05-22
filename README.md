# READER_EMAIL

Lector de buzones Microsoft 365 que identifica **solicitudes de cotización**
entrantes y **extrae el valor monetario** que se está pidiendo
(del cuerpo del correo o de los adjuntos PDF/Excel/CSV).

> **Alcance actual:** detectar que un correo es una cotización y reportar su
> monto. **No** clasifica auto/manual y **no** requiere catálogo de productos.

## Stack

- **Python 3.10+**
- **Microsoft Graph API** (App-only auth, MSAL client-credentials)
- **SQLite** para persistencia
- **CLI** con `typer` + `rich`
- Parsing: `pypdf`, `openpyxl`, `pandas`

## Estructura

```
READER_EMAIL/
├── config/
│   └── settings.yaml      # Keywords, blacklist, extensiones
├── src/
│   ├── config.py          # Carga de .env + settings.yaml
│   ├── graph_client.py    # Cliente Microsoft Graph (App-only)
│   ├── mail_reader.py     # Lista correos y descarga adjuntos
│   ├── attachments.py     # Parser PDF/Excel/CSV
│   ├── extractor.py       # Deteccion de montos, plazo y urgencia
│   ├── storage.py         # SQLite (SQLAlchemy)
│   └── main.py            # CLI (typer)
├── .env.example
├── requirements.txt
└── README.md
```

## Setup

### 1. App registration en Azure AD

1. Azure Portal → Microsoft Entra ID → **App registrations** → **New registration**
2. Copia **Tenant ID** y **Application (client) ID**
3. En **Certificates & secrets** → New client secret → copia el `Value` (¡solo aparece una vez!)
4. En **API permissions** → Add permission → Microsoft Graph → **Application permissions**:
   - `Mail.Read`
5. **Grant admin consent**

### 2. Configurar el proyecto

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt

copy .env.example .env
# Edita .env con tus IDs y los buzones a leer
```

### 3. Ajustar keywords (opcional)

`config/settings.yaml` contiene la lista de palabras clave que disparan la
identificación. Por defecto incluye: `cotizacion`, `cotizar`, `quote`, `rfq`,
`presupuesto`, `oferta`, etc. Agrega o quita según necesites.

## Uso

```powershell
# Leer correos nuevos, procesar adjuntos y detectar montos
python -m src.main run

# Solo listar correos identificados, sin procesar ni guardar (dry-run)
python -m src.main run --dry-run

# Ver cotizaciones procesadas
python -m src.main list

# Solo las que tienen monto detectado
python -m src.main list --with-amount

# Detalle de una cotizacion (con todos los montos encontrados y su contexto)
python -m src.main show <id>
```

## Cómo identifica una cotización

Un correo se considera cotización si cumple **todas** estas condiciones:

1. El remitente **no** está en `sender_blacklist_domains`
2. El asunto o el cuerpo contiene al menos una `keyword`
   (`cotizacion`, `cotizar`, `quote`, `rfq`, `presupuesto`, etc.)

## Cómo extrae el monto

El extractor busca montos en este orden:

1. **Etiquetas explícitas** en el texto: `Total: $1.234.567`,
   `Valor total COP 5.000.000`, `Subtotal $...`, `Importe total ...` →
   alta confianza.
2. **Columnas en Excel/CSV**: si hay columna `total`/`subtotal`/`importe`,
   suma sus valores. Si no, busca `precio * cantidad`.
3. **Cualquier monto con símbolo de moneda** (`$`, `COP`, `USD`) en el texto.

Soporta formatos:
- Colombiano: `1.234.567,89`
- US: `1,234,567.89`
- Monedas: COP, USD, EUR (con o sin símbolo)

### Niveles de confianza

| Confianza | Cuándo |
|---|---|
| **ALTA** | Encontró un monto precedido por "Total", "Subtotal", etc. |
| **MEDIA** | Encontró algún monto con símbolo de moneda pero sin etiqueta clara |
| **BAJA** | No detectó montos numéricos relevantes |

## Roadmap

- [ ] Integración con sistema ERP / cotizador automático
- [ ] Notificación por Teams cuando hay cotizaciones urgentes
- [ ] Catálogo de productos para identificar items específicos
- [ ] Dashboard web para revisión
- [ ] Capa de IA (Claude) para correos con info no estructurada
