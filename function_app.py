import os
import io
import json
import hashlib
import logging
from datetime import datetime, timezone, timedelta

import azure.functions as func
from azure.storage.blob import BlobServiceClient, ContentSettings
from pypdf import PdfReader

import msal
import requests
from openai import AzureOpenAI

# =========================
# CONFIG
# =========================
CONTAINER_NAME = os.getenv("MENUS_CONTAINER", "menu")

GRAPH_TENANT_ID = os.getenv("GRAPH_TENANT_ID")
GRAPH_CLIENT_ID = os.getenv("GRAPH_CLIENT_ID")
GRAPH_CLIENT_SECRET = os.getenv("GRAPH_CLIENT_SECRET")
GRAPH_SENDER_UPN = os.getenv("GRAPH_SENDER_UPN")
GRAPH_RECIPIENTS = os.getenv("GRAPH_RECIPIENTS", "")  # "a@a.com;b@b.com"

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")

# Force sending even if PDF hash is the same (useful for testing)
FORCE_SEND = os.getenv("FORCE_SEND", "false").lower() == "true"

app = func.FunctionApp()


# =========================
# HELPERS: STORAGE
# =========================
def blob_service() -> BlobServiceClient:
    conn = os.environ["AzureWebJobsStorage"]
    return BlobServiceClient.from_connection_string(conn)


def sha256(b: bytes) -> str:
    return hashlib.sha256(b).hexdigest()


def list_latest_pdf_blob(bsc: BlobServiceClient, prefix: str) -> tuple[str, bytes]:
    container = bsc.get_container_client(CONTAINER_NAME)
    blobs = list(container.list_blobs(name_starts_with=prefix))
    pdfs = [b for b in blobs if b.name.lower().endswith(".pdf")]
    if not pdfs:
        raise RuntimeError(f"No hay PDFs en {CONTAINER_NAME}/{prefix}")

    pdfs.sort(key=lambda b: b.last_modified, reverse=True)
    latest = pdfs[0]
    bc = bsc.get_blob_client(CONTAINER_NAME, latest.name)
    return latest.name, bc.download_blob().readall()


def read_json_blob(bsc: BlobServiceClient, blob_name: str) -> dict:
    bc = bsc.get_blob_client(CONTAINER_NAME, blob_name)
    try:
        data = bc.download_blob().readall()
        return json.loads(data.decode("utf-8"))
    except Exception:
        return {}


def write_json_blob(bsc: BlobServiceClient, blob_name: str, obj: dict):
    bc = bsc.get_blob_client(CONTAINER_NAME, blob_name)
    bc.upload_blob(
        json.dumps(obj, ensure_ascii=False).encode("utf-8"),
        overwrite=True,
        content_settings=ContentSettings(content_type="application/json"),
    )


def get_latest_pdf_for_prefix(bsc: BlobServiceClient, prefix: str) -> tuple[str, bytes]:
    if not prefix.endswith("/"):
        prefix += "/"
    return list_latest_pdf_blob(bsc, prefix=prefix)


# =========================
# HELPERS: PDF -> TEXT
# =========================
def extract_text_from_pdf(pdf_bytes: bytes) -> str:
    reader = PdfReader(io.BytesIO(pdf_bytes))
    parts = []
    for page in reader.pages:
        parts.append(page.extract_text() or "")
    text = "\n".join(parts).strip()
    if not text:
        raise RuntimeError("No se pudo extraer texto del PDF (posible escaneado).")
    return text


# =========================
# HELPERS: DATES
# =========================
def next_week_range_es(today=None) -> tuple[str, str]:
    """
    Devuelve (start, end) de la semana siguiente (lunes a viernes) en formato dd/mm/yyyy.
    """
    if today is None:
        today = datetime.now(timezone.utc).astimezone()

    # Monday=0 ... Sunday=6
    days_until_next_monday = (7 - today.weekday()) % 7
    if days_until_next_monday == 0:
        days_until_next_monday = 7  # si hoy es lunes, "próxima semana" es la siguiente

    next_monday = today.replace(hour=0, minute=0, second=0, microsecond=0) + timedelta(days=days_until_next_monday)
    next_friday = next_monday + timedelta(days=4)

    return next_monday.strftime("%d/%m/%Y"), next_friday.strftime("%d/%m/%Y")


# =========================
# HELPERS: AZURE OPENAI (menu -> JSON + cenas)
# =========================
def build_weekly_menu_with_openai(raw_text: str, target_week: str) -> dict:
    endpoint = os.environ["AZURE_OPENAI_ENDPOINT"]
    api_key = os.environ["AZURE_OPENAI_KEY"]
    deployment = os.environ["AZURE_OPENAI_DEPLOYMENT"]
    api_version = os.environ.get("AZURE_OPENAI_API_VERSION", "2025-01-01-preview")

    client = AzureOpenAI(
        api_version=api_version,
        azure_endpoint=endpoint,
        api_key=api_key,
    )

    system_msg = "Devuelve SIEMPRE un JSON válido, sin texto adicional."
    user_msg = f"""
Devuelve un JSON con este esquema EXACTO:
{{
  "week_label": "string",
  "days": [{{"day":"Lunes","items":["..."]}}],
  "summary_email": "string",
  "summary_whatsapp": "string",
  "dinners": [
    {{
      "day":"Lunes",
      "dinner":"string",
      "notes":"string"
    }}
  ]
}}

OBJETIVO:
- Extrae SOLO el menú correspondiente a: {target_week}
- Si el PDF contiene varias semanas, IGNORA las demás.
- Si no está esa semana, intenta extraer la semana más próxima posterior.
- Pon el dia de la semana para cada día (Lunes, Martes, etc) de la {target_week} y los platos separados en una lista.
- los niños normalmente comen poca legumbre, incrementar su consumo es bueno, por lo que si el menú del mediodía no tiene legumbre, propon una cena con legumbre (ej: lentejas con verduras).

CENAS (muy importante):
- Actúa como nutricionista pediátrico.
- Propón UNA cena por día (lunes a viernes) para menores de 8 años.
- Cenas fáciles (máx 30 min), sabores suaves, ingredientes normales.
- La cena debe complementar lo comido al mediodía:
- Si al mediodía hubo legumbre o plato pesado → cena ligera.
- Si al mediodía hubo pescado → cena con verdura + huevo o carne blanca (o viceversa).
- Incluye verdura y proteína; añade fruta o yogur de postre si encaja.
- Evita fritos, picantes, “sabores raros”, recetas complicadas.
- Si el PDF tiene información de cenas, úsala como base pero adáptala a las recomendaciones anteriores.
- Si el PDF no tiene cenas, propónlas igualmente basándote en el menú del mediodía. 

Reglas:
- No inventes platos del comedor: si faltan datos, pon lo que haya.
- Sí puedes proponer cenas aunque falte detalle del comedor (usa prudencia).
- No incluyas texto fuera del JSON.
- no incluyas platos que no correspondan a la semana objetivo.
- Si el PDF tiene texto adicional (introducciones, notas, etc) al final, ignóralo.

TEXTO PDF:
{raw_text[:12000]}
"""

    resp = client.chat.completions.create(
        model=deployment,  # deployment name
        temperature=0.2,
        response_format={"type": "json_object"},
        messages=[
            {"role": "system", "content": system_msg},
            {"role": "user", "content": user_msg},
        ],
    )

    return json.loads(resp.choices[0].message.content)


def render_menu_block_md(title: str, menu: dict) -> str:
    lines = [f"🍽️ *{title}*", f"*{menu.get('week_label','')}*"]
    for d in menu.get("days", []):
        items = " · ".join(d.get("items", []))
        lines.append(f"- *{d.get('day','')}*: {items}")
    return "\n".join(lines)


def render_dinners_block_md(menu: dict) -> str:
    lines = ["\n🍲 *Sugerencias de cena (≤8 años)*"]
    for d in menu.get("dinners", []):
        dinner = d.get("dinner", "")
        notes = (d.get("notes", "") or "").strip()
        if notes:
            lines.append(f"- *{d.get('day','')}*: {dinner} _( {notes} )_")
        else:
            lines.append(f"- *{d.get('day','')}*: {dinner}")
    return "\n".join(lines)


def render_menu_block_html(title: str, menu: dict, source_blob: str) -> str:
    days_html = "".join(
        f"<li><b>{d.get('day','')}:</b> " + " · ".join(d.get("items", [])) + "</li>"
        for d in menu.get("days", [])
    )

    dinners_html = "".join(
        f"<li><b>{d.get('day','')}:</b> {d.get('dinner','')}<br/><small>{(d.get('notes','') or '')}</small></li>"
        for d in menu.get("dinners", [])
    )

    return f"""
<h3>{title}</h3>
<p><b>{menu.get('week_label','')}</b></p>
<p>{menu.get('summary_email','')}</p>
<ul>{days_html}</ul>
<h4>Sugerencias de cena (≤8 años)</h4>
<ul>{dinners_html}</ul>
<p><small>Fuente: {source_blob}</small></p>
"""


# =========================
# HELPERS: GRAPH EMAIL
# =========================
def get_graph_token() -> str:
    if not (GRAPH_TENANT_ID and GRAPH_CLIENT_ID and GRAPH_CLIENT_SECRET):
        raise RuntimeError("Faltan GRAPH_TENANT_ID / GRAPH_CLIENT_ID / GRAPH_CLIENT_SECRET.")

    authority = f"https://login.microsoftonline.com/{GRAPH_TENANT_ID}"
    app_msal = msal.ConfidentialClientApplication(
        GRAPH_CLIENT_ID,
        authority=authority,
        client_credential=GRAPH_CLIENT_SECRET,
    )
    result = app_msal.acquire_token_for_client(scopes=["https://graph.microsoft.com/.default"])
    if "access_token" not in result:
        raise RuntimeError(f"No token Graph: {result.get('error')} - {result.get('error_description')}")
    return result["access_token"]


def send_email(subject: str, body_html: str):
    if not GRAPH_SENDER_UPN:
        raise RuntimeError("Falta GRAPH_SENDER_UPN.")
    recipients = [e.strip() for e in GRAPH_RECIPIENTS.split(";") if e.strip()]
    if not recipients:
        raise RuntimeError("GRAPH_RECIPIENTS vacío. Usa 'a@a.com;b@b.com'.")

    token = get_graph_token()
    url = f"https://graph.microsoft.com/v1.0/users/{GRAPH_SENDER_UPN}/sendMail"
    payload = {
        "message": {
            "subject": subject,
            "body": {"contentType": "HTML", "content": body_html},
            "toRecipients": [{"emailAddress": {"address": r}} for r in recipients],
        },
        "saveToSentItems": "true"
    }

    r = requests.post(
        url,
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
        json=payload,
        timeout=30
    )
    if r.status_code not in (200, 202):
        raise RuntimeError(f"Graph sendMail falló ({r.status_code}): {r.text}")


# =========================
# HELPERS: TELEGRAM
# =========================
def send_telegram(message: str):
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        raise RuntimeError("Falta TELEGRAM_BOT_TOKEN o TELEGRAM_CHAT_ID.")

    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = {"chat_id": TELEGRAM_CHAT_ID, "text": message, "parse_mode": "Markdown"}
    r = requests.post(url, json=payload, timeout=20)
    if r.status_code != 200:
        raise RuntimeError(f"Telegram sendMessage falló ({r.status_code}): {r.text}")


# =========================
# TIMER A (opcional)
# =========================
@app.timer_trigger(schedule="0 0 7 * * *", arg_name="dailyTimer", run_on_startup=False, use_monitor=True)
def daily_check(dailyTimer: func.TimerRequest) -> None:
    logging.warning(">>> DAILY CHECK: ok (no hace nada).")


# =========================
# TIMER B: resumen semanal (domingo)
# =========================
# Domingo 08:00 UTC (España suele ser 09:00/10:00 según horario)
@app.timer_trigger(schedule="0 0 8 * * 0", arg_name="weeklyTimer", run_on_startup=True, use_monitor=True)
def weekly_menu_digest(weeklyTimer: func.TimerRequest) -> None:
    logging.warning(">>> WEEKLY DIGEST: start")

    bsc = blob_service()

    # Prefijos -> nombres para el mensaje
    menus_map = {
        "infantil": "Miravalles",
        "kids": "Kids Garden",
    }

    # Semana siguiente (L-V)
    start, end = next_week_range_es()
    target_week = f"Semana del {start} al {end}"
    logging.warning(f">>> Target week: {target_week}")

    md_blocks: list[str] = []
    html_blocks: list[str] = []
    anything_new = False

    for prefix, display_name in menus_map.items():
        try:
            blob_name, pdf_bytes = get_latest_pdf_for_prefix(bsc, prefix)
            pdf_hash = sha256(pdf_bytes)

            # Estado independiente por prefijo
            state_blob = f"state/weekly_{prefix}.json"
            state = read_json_blob(bsc, state_blob)

            
        same_pdf = state.get("last_pdf_hash") == pdf_hash

        menu = None
        if same_pdf:
            # Reutiliza el menú ya calculado si existe
            menu = state.get("last_menu")
            if menu:
                logging.warning(f">>> {prefix}: PDF igual, reutilizo last_menu del state.")
            else:
                logging.warning(f">>> {prefix}: PDF igual pero no hay last_menu en state; recalculo IA.")
        else:
            logging.warning(f">>> {prefix}: PDF nuevo, recalculo IA.")

        if not menu:
            raw_text = extract_text_from_pdf(pdf_bytes)
            menu = build_weekly_menu_with_openai(raw_text, target_week)

        # Construir bloques para enviar SIEMPRE en el semanal
        md_blocks.append(
            render_menu_block_md(display_name, menu) + "\n" + render_dinners_block_md(menu)
        )
        html_blocks.append(render_menu_block_html(display_name, menu, blob_name))

        # Guardar state (incluyendo last_menu)
        write_json_blob(bsc, state_blob, {
            "last_pdf_hash": pdf_hash,
            "last_pdf_blob": blob_name,
            "updated_utc": datetime.now(timezone.utc).isoformat(),
            "week_label": menu.get("week_label", ""),
            "last_menu": menu
        })

        anything_new = True  # En semanal, siempre habrá algo que enviar


        except Exception as e:
            logging.exception(f">>> Error procesando {prefix}: {e}")

    if not anything_new:
        logging.warning(">>> No hay menús nuevos que enviar.")
        return

    telegram_msg = "\n\n".join(md_blocks)
    email_html = "<hr/>".join(html_blocks)

    # Envíos (independientes)
    try:
        send_telegram(telegram_msg)
        logging.warning(">>> WEEKLY DIGEST: telegram enviado OK.")
    except Exception as e:
        logging.exception(f">>> WEEKLY DIGEST: fallo enviando telegram: {e}")

    try:
        send_email("Menús semanales – Miravalles / Kids Garden", email_html)
        logging.warning(">>> WEEKLY DIGEST: email enviado OK.")
    except Exception as e:
        logging.exception(f">>> WEEKLY DIGEST: fallo enviando email: {e}")