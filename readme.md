> **Nota:** Esta solución nace de la necesidad como padre de extraer automáticamente la información de los menús escolares y disponer de ella de forma estructurada antes de que comience la semana, facilitando la organización familiar.

# Menú semanal (Miravalles + Kids Garden) 🍱

### Azure Functions + Blob + Azure OpenAI + Graph + Telegram

Esta solución **serverless** automatiza la extracción, procesamiento y envío de los menús escolares semanales. Cada domingo, el sistema analiza los PDFs más recientes y envía un resumen unificado por correo electrónico y Telegram.

---

## 🚀 Funcionalidades principales

* **Ingesta Automática:** Escaneo de archivos PDF en Azure Blob Storage en las rutas `infantil/` (Miravalles) y `kids/` (Kids Garden).
* **Inteligencia Artificial:** Uso de **Azure OpenAI (GPT-4o-mini)** para extraer y estructurar el menú desde texto plano a JSON.
* **Control de Duplicados:** Sistema de hashing (SHA-256) para evitar el envío de menús repetidos.
* **Notificación Multicanal:** * **Email:** Envío profesional mediante **Microsoft Graph API**.
* **Telegram:** Mensajería instantánea mediante **Telegram Bot API**.



---

## 🛠️ Arquitectura y Flujo de Datos

1. **Entrada:** Se buscan los PDFs con la fecha `last_modified` más reciente en el contenedor de blobs.
2. **Procesamiento:**
* Extracción de texto con `pypdf`.
* Cálculo de hash para verificar cambios.


3. **IA:** El modelo genera un resumen estructurado para la semana entrante (Lunes-Viernes).
4. **Salida:** Envío de un único resumen consolidado con ambos menús.

---

## 📋 Requisitos Técnicos

### Infraestructura Azure

* **Azure Function App:** Python 3.10+ (v4).
* **Storage Account:** Contenedor para PDFs y persistencia de estado (`.json`).
* **Azure OpenAI:** Deployment activo (ej. `gpt-4o-mini`).
* **Microsoft Entra ID:** App Registration con permisos `Mail.Send` para Graph API.

### Servicios Externos

* **Telegram:** Un Bot API Token y un `chat_id` (personal o de grupo).

---

## ⚙️ Configuración (Variables de Entorno)

Debes configurar las siguientes *Application Settings* en tu Function App:

| Categoría | Variable | Descripción |
| --- | --- | --- |
| **Storage** | `MENUS_CONTAINER` | Nombre del contenedor (ej: `menu`). |
| **OpenAI** | `AZURE_OPENAI_ENDPOINT` | URL de tu recurso de Azure OpenAI. |
|  | `AZURE_OPENAI_KEY` | Clave de API. |
|  | `AZURE_OPENAI_DEPLOYMENT` | Nombre de tu modelo desplegado. |
| **Graph** | `GRAPH_TENANT_ID` | ID del inquilino de Azure. |
|  | `GRAPH_CLIENT_ID` | ID de la aplicación (Client ID). |
|  | `GRAPH_CLIENT_SECRET` | Secreto de la aplicación. |
|  | `GRAPH_SENDER_UPN` | Email del remitente. |
| **Telegram** | `TELEGRAM_BOT_TOKEN` | Token de BotFather. |
|  | `TELEGRAM_CHAT_ID` | ID del chat/grupo de destino. |
| **Testing** | `FORCE_SEND` | `true` para ignorar el hash de duplicados. |

---

## 📂 Estructura de Carpetas Recomendada

```text
/ (Contenedor Blob)
├── infantil/        # PDFs de Miravalles
├── kids/            # PDFs de Kids Garden
└── state/           # Archivos JSON para control de versiones/hash
    ├── weekly_infantil.json
    └── weekly_kids.json

```

---

## 🛠️ Instalación y Despliegue

1. **Clonar y configurar:**
```bash
git clone https://github.com/tu-usuario/tu-repo.git
cd tu-repo

```


2. **Instalar dependencias:**
```bash
pip install -r requirements.txt

```


3. **Pruebas locales:**
Crea un archivo `local.settings.json` con las variables mencionadas arriba y ejecuta:
```bash
func start

```


4. **Despliegue:**
Usa VS Code (Azure Extensions) o GitHub Actions para desplegar a tu **Function App**.

---

## ⚠️ Limitaciones y Seguridad

* **PDFs Escaneados:** Actualmente requiere que el PDF tenga texto embebido. Si es una imagen (foto), se requeriría añadir un servicio de **OCR (Azure AI Vision)**.
* **Seguridad:** Nunca subas el archivo `local.settings.json` al repositorio. Se recomienda usar **Azure Key Vault** para los secretos en entornos de producción.
* **Markdown en Telegram:** Caracteres especiales como `_` o `*` pueden causar errores si no se escapan correctamente.

---

## 🗺️ Roadmap

* [ ] Añadir web scraping, actualmente se basa en la subida manual de PDFs.
* [ ] Añadir soporte para múltiples grupos de Telegram.
* [ ] Implementar OCR para menús escaneados mediante fotos.
* [ ] Crear un dashboard simple en Power BI para historial de menús.