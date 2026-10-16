"""Servidor web (FastAPI) que recibe los mensajes de Telegram vía webhook.

Telegram -> POST /webhook -> grafo LangGraph -> respuesta al cliente (+ aviso al sector)
"""

import hmac
import json
import logging
import os
import time
from collections import defaultdict, deque
from contextlib import asynccontextmanager

import httpx
from fastapi import FastAPI, Header, Request, Response

from app.graph import cargar_modelo, grafo

logging.basicConfig(level=logging.INFO)
# httpx registra cada URL pedida, y la URL de la API de Telegram contiene el token del bot:
# subimos su nivel para que el token nunca quede escrito en los logs.
logging.getLogger("httpx").setLevel(logging.WARNING)
log = logging.getLogger("telegram-router")

TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN", "")
WEBHOOK_SECRET = os.environ.get("WEBHOOK_SECRET", "")
# Opcional: JSON con el chat_id del grupo de cada sector, ej. {"ventas": -100123, "soporte": -100456}
SECTOR_CHAT_IDS: dict[str, int] = json.loads(os.environ.get("SECTOR_CHAT_IDS", "{}") or "{}")

# Límite de mensajes por usuario, para que nadie pueda saturar el servidor (y la cuenta de GCP).
MAX_MENSAJES_POR_MINUTO = 5

API_URL = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}"

_ultimos_mensajes: dict[int, deque] = defaultdict(deque)


@asynccontextmanager
async def lifespan(_: FastAPI):
    if not WEBHOOK_SECRET:
        log.error("Falta WEBHOOK_SECRET: el webhook va a rechazar todos los pedidos.")
    cargar_modelo()  # el primer mensaje no tiene que esperar la carga del modelo
    yield


app = FastAPI(title="Clasificador de consultas", lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)


async def enviar_mensaje(chat_id: int, texto: str, markdown: bool = False) -> None:
    payload = {"chat_id": chat_id, "text": texto}
    if markdown:
        payload["parse_mode"] = "Markdown"
    async with httpx.AsyncClient(timeout=15) as client:
        r = await client.post(f"{API_URL}/sendMessage", json=payload)
        if r.status_code != 200:
            log.error("Error enviando a Telegram (%s): %s", r.status_code, r.text[:200])


def _excede_limite(chat_id: int) -> bool:
    ahora = time.monotonic()
    tiempos = _ultimos_mensajes[chat_id]
    while tiempos and ahora - tiempos[0] > 60:
        tiempos.popleft()
    if len(tiempos) >= MAX_MENSAJES_POR_MINUTO:
        return True
    tiempos.append(ahora)
    return False


@app.get("/")
def salud():
    return {"ok": True}


@app.post("/webhook")
async def webhook(
    request: Request,
    x_telegram_bot_api_secret_token: str | None = Header(default=None),
):
    # Telegram envía este header porque configuramos secret_token en setWebhook. Sin secreto
    # configurado se rechaza todo (nunca queda abierto por error).
    if not WEBHOOK_SECRET or not hmac.compare_digest(
        (x_telegram_bot_api_secret_token or "").encode(), WEBHOOK_SECRET.encode()
    ):
        return Response(status_code=403)

    # Siempre devolvemos 200: si no, Telegram reintenta el mismo update una y otra vez.
    try:
        await procesar_update(await request.json())
    except Exception:
        log.exception("Error procesando el update")
    return {"ok": True}


async def procesar_update(update: dict) -> None:
    mensaje = update.get("message")
    # Solo atendemos chats privados: así el bot no clasifica lo que se habla en los grupos de sector.
    if not mensaje or "text" not in mensaje or mensaje["chat"].get("type") != "private":
        return

    chat_id = mensaje["chat"]["id"]
    texto = mensaje["text"]

    if _excede_limite(chat_id):
        log.warning("Límite de mensajes superado por el chat %s", chat_id)
        return

    if texto.startswith("/start"):
        await enviar_mensaje(
            chat_id,
            "¡Hola! 👋 Contanos tu consulta y la derivamos al área correspondiente "
            "(ventas, facturación, soporte, reclamos).",
        )
        return

    remitente = mensaje.get("from", {})
    usuario = f"{remitente.get('first_name', '')} (@{remitente.get('username', 's/usuario')}, id {remitente.get('id')})"

    resultado = await grafo.ainvoke({"mensaje": texto, "usuario": usuario})
    # No se registra el texto del cliente en los logs (datos personales), solo el resultado.
    log.info(
        "Clasificado como %s (confianza %.2f, urgencia %.2f): %s",
        resultado["sector"], resultado.get("confianza", 0), resultado.get("urgencia", 0), resultado.get("detalle"),
    )

    await enviar_mensaje(chat_id, resultado["respuesta"], markdown=True)

    destino = SECTOR_CHAT_IDS.get(resultado["sector"])
    if destino:
        await enviar_mensaje(destino, resultado["derivacion"])
