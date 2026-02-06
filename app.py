import os
import re
import json
import hashlib
from datetime import datetime, timedelta, timezone

import requests
import psycopg
from psycopg import Binary
from flask import Flask, request, Response, render_template_string


# ================= APP =================

app = Flask(__name__)


# ================= CONFIG =================

VERIFY_TOKEN = os.getenv("VERIFY_TOKEN", "change-me")
WHATSAPP_TOKEN = os.getenv("WHATSAPP_TOKEN", "")
GRAPH_API_VERSION = os.getenv("GRAPH_API_VERSION", "v20.0")

DATABASE_URL = os.getenv("DATABASE_URL", "")

APP_TITLE = os.getenv("APP_TITLE", "DailyDeals - Seu grupo de Promoções")

DAYS_TO_SHOW = int(os.getenv("DAYS_TO_SHOW", "7"))
MAX_ITEMS = int(os.getenv("MAX_ITEMS", "200"))

URL_RE = re.compile(r"(https?://[^\s<>()\"']+)", re.IGNORECASE)

# ========================================


HTML = r"""
<!doctype html>
<html lang="pt-BR">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width,initial-scale=1" />
  <title>{{ title }}</title>
  <style>
    :root { --bg:#0b0b0f; --card:#141422; --text:#f3f3f7; --muted:#b9b9c6; --accent:#ffd400; }
    body { margin:0; font-family:system-ui,-apple-system,Segoe UI,Roboto,Ubuntu; background:var(--bg); color:var(--text); }
    header { padding:24px 16px; position:sticky; top:0; background:linear-gradient(180deg, rgba(11,11,15,0.95), rgba(11,11,15,0.75)); backdrop-filter: blur(8px); border-bottom:1px solid rgba(255,255,255,0.06); }
    .wrap { max-width:1100px; margin:0 auto; }
    h1 { margin:0; font-size:22px; letter-spacing:.2px; }
    .sub { margin-top:6px; color:var(--muted); font-size:13px; }
    .grid { padding:18px 16px 32px; display:grid; gap:14px; grid-template-columns: repeat(auto-fill, minmax(220px, 1fr)); }
    a.card { text-decoration:none; color:inherit; background:var(--card); border:1px solid rgba(255,255,255,0.07); border-radius:18px; overflow:hidden; display:block; transition: transform .12s ease, border-color .12s ease; }
    a.card:hover { transform: translateY(-2px); border-color: rgba(255,212,0,0.35); }
    .img { width:100%; aspect-ratio: 1 / 1; background:#0f0f17; display:flex; align-items:center; justify-content:center; }
    .img img { width:100%; height:100%; object-fit:cover; display:block; }
    .meta { padding:12px 12px 14px; }
    .title { font-weight:700; font-size:14px; line-height:1.25; }
    .small { margin-top:6px; font-size:12px; color:var(--muted); display:flex; justify-content:space-between; gap:10px; }
    .badge { display:inline-block; padding:3px 8px; border-radius:999px; background: rgba(255,212,0,0.13); color: var(--accent); font-size:11px; font-weight:600; }
    footer { padding:18px 16px 30px; color:var(--muted); font-size:12px; text-align:center; }
    .empty { padding:40px 16px; color:var(--muted); text-align:center; }
  </style>
</head>
<body>
  <header>
    <div class="wrap">
      <h1>{{ title }}</h1>
      <div class="sub">Últimos {{ days }} dias • {{ count }} itens</div>
    </div>
  </header>

  {% if items %}
    <div class="wrap">
      <div class="grid">
        {% for it in items %}
          <a class="card" href="{{ it.url }}" target="_blank" rel="noopener noreferrer">
            <div class="img">
              <img src="/img/{{ it.id }}" alt="Promoção">
            </div>
            <div class="meta">
              <div class="title">{{ it.card_title }}</div>
              <div class="small">
                <span class="badge">{{ it.source }}</span>
                <span>{{ it.created_at }}</span>
              </div>
            </div>
          </a>
        {% endfor %}
      </div>
    </div>
  {% else %}
    <div class="empty">Nenhuma imagem com link nos últimos {{ days }} dias.</div>
  {% endif %}

  <footer>DailyDeals • WhatsApp Cloud API</footer>
</body>
</html>
"""


# ================= DB =================

def db_conn():
    if not DATABASE_URL:
        raise RuntimeError("DATABASE_URL não configurada.")
    return psycopg.connect(DATABASE_URL)


def sha256(s: str) -> str:
    return hashlib.sha256(s.encode("utf-8")).hexdigest()


def init_db():
    conn = db_conn()
    try:
        with conn.cursor() as cur:
            cur.execute("CREATE SCHEMA IF NOT EXISTS whatsapp;")

            cur.execute("""
                CREATE TABLE IF NOT EXISTS whatsapp.deals (
                  id BIGSERIAL PRIMARY KEY,
                  wa_from TEXT NULL,
                  wa_name TEXT NULL,
                  msg_time TIMESTAMPTZ NULL,
                  url TEXT NULL,
                  url_hash CHAR(64) NOT NULL UNIQUE,
                  image_bytes BYTEA NULL,
                  image_mime TEXT NULL,
                  created_at TIMESTAMPTZ DEFAULT now()
                );
            """)
        conn.commit()
    finally:
        conn.close()


def save_deal(wa_from, wa_name, msg_time, url, image_bytes, image_mime):

    url_hash = sha256(f"{wa_from}|{wa_name}|{msg_time}|{url}")

    conn = db_conn()
    try:
        with conn.cursor() as cur:
            cur.execute("""
                INSERT INTO whatsapp.deals
                  (wa_from, wa_name, msg_time, url, url_hash, image_bytes, image_mime)
                VALUES (%s,%s,%s,%s,%s,%s,%s)
                ON CONFLICT (url_hash) DO NOTHING
            """, (
                wa_from,
                wa_name,
                msg_time,
                url,
                url_hash,
                Binary(image_bytes) if image_bytes else None,
                image_mime
            ))
        conn.commit()
    finally:
        conn.close()


# ================= HELPERS =================

def infer_source(url: str) -> str:
    u = (url or "").lower()
    if "amazon." in u:
        return "Amazon"
    if "mercadolivre" in u or "meli." in u:
        return "Mercado Livre"
    if "shopee." in u:
        return "Shopee"
    return "Link"


def make_card_title(url: str) -> str:
    try:
        return f"Oferta em {url.split('/')[2]}"
    except Exception:
        return "Oferta"


def download_media(media_id: str) -> tuple[bytes, str]:

    if not WHATSAPP_TOKEN:
        raise RuntimeError("WHATSAPP_TOKEN não configurado")

    headers = {"Authorization": f"Bearer {WHATSAPP_TOKEN}"}

    meta = requests.get(
        f"https://graph.facebook.com/{GRAPH_API_VERSION}/{media_id}",
        headers=headers,
        timeout=25
    )
    meta.raise_for_status()

    j = meta.json()

    media_url = j.get("url")
    mime = j.get("mime_type", "application/octet-stream")

    if not media_url:
        raise RuntimeError("Media URL não retornada")

    r = requests.get(media_url, headers=headers, timeout=40)
    r.raise_for_status()

    return r.content, mime


# ================= ROUTES =================

@app.get("/health")
def health():
    return "ok", 200


@app.get("/webhook")
def webhook_verify():

    mode = request.args.get("hub.mode")
    token = request.args.get("hub.verify_token")
    challenge = request.args.get("hub.challenge")

    if mode == "subscribe" and token == VERIFY_TOKEN:
        return challenge or "", 200

    return "forbidden", 403


@app.post("/webhook")
def webhook_receive():

    payload = request.get_json(force=True, silent=True) or {}

    try:
        entries = payload.get("entry", [])

        for entry in entries:

            for ch in entry.get("changes", []):

                value = ch.get("value", {}) or {}

                messages = value.get("messages", []) or []
                contacts = value.get("contacts", []) or []

                wa_name = None
                if contacts:
                    wa_name = (contacts[0].get("profile") or {}).get("name")

                for m in messages:

                    wa_from = m.get("from")

                    ts = m.get("timestamp")
                    msg_time = (
                        datetime.fromtimestamp(int(ts), tz=timezone.utc)
                        if ts else datetime.now(timezone.utc)
                    )

                    mtype = m.get("type")

                    text = ""
                    image_bytes = None
                    image_mime = None

                    if mtype == "text":
                        text = (m.get("text") or {}).get("body") or ""

                    elif mtype == "image":

                        img = m.get("image") or {}

                        text = img.get("caption") or ""

                        media_id = img.get("id")

                        if media_id:
                            image_bytes, image_mime = download_media(media_id)

                    urls = URL_RE.findall(text or "")

                    for url in urls:

                        save_deal(
                            wa_from,
                            wa_name,
                            msg_time,
                            url,
                            image_bytes,
                            image_mime
                        )

    except Exception as ex:

        print("Erro no webhook:", ex)
        print(json.dumps(payload)[:2000])

    return "ok", 200


@app.get("/")
def marketplace():

    since = datetime.now(timezone.utc) - timedelta(days=DAYS_TO_SHOW)

    conn = db_conn()
    try:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT id, url, created_at
                FROM whatsapp.deals
                WHERE image_bytes IS NOT NULL
                  AND url IS NOT NULL
                  AND created_at >= %s
                ORDER BY created_at DESC
                LIMIT %s
            """, (since, MAX_ITEMS))

            rows = cur.fetchall()
    finally:
        conn.close()

    items = []

    for id_, url, created_at in rows:

        items.append({
            "id": id_,
            "url": url,
            "created_at": created_at.astimezone(timezone.utc).strftime("%d/%m %H:%M"),
            "source": infer_source(url),
            "card_title": make_card_title(url),
        })

    return render_template_string(
        HTML,
        title=APP_TITLE,
        items=items,
        count=len(items),
        days=DAYS_TO_SHOW
    )


@app.get("/img/<int:item_id>")
def img(item_id: int):

    conn = db_conn()
    try:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT image_bytes, image_mime
                FROM whatsapp.deals
                WHERE id = %s
            """, (item_id,))

            row = cur.fetchone()

            if not row or row[0] is None:
                return Response(status=404)

            image_bytes = bytes(row[0])
            mime = row[1] or "application/octet-stream"

    finally:
        conn.close()

    return Response(image_bytes, mimetype=mime)


# ================= INIT =================

init_db()
