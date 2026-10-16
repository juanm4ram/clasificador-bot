"""Pruebas locales, sin desplegar nada.

    python probar_local.py           -> clasifica mensajes de ejemplo (la primera vez descarga el modelo, ~1 GB)
    python probar_local.py --bot     -> corre el bot de Telegram en tu PC usando polling (sin webhook)
"""

import asyncio
import sys

from dotenv import load_dotenv

load_dotenv()

EJEMPLOS = [
    "Hola, ¿cuánto cuesta el plan premium para 10 usuarios?",
    "Me cobraron dos veces la factura de septiembre",
    "No puedo iniciar sesión, me dice contraseña incorrecta",
    "Es la tercera vez que llamo y nadie me resuelve nada, quiero dar de baja el servicio",
    "Buenas tardes",
]


async def clasificar_ejemplos():
    from app.graph import grafo

    for texto in EJEMPLOS:
        r = await grafo.ainvoke({"mensaje": texto, "usuario": "prueba"})
        print(
            f"[{r['sector']:<11}] confianza {r['confianza']:.0%}, urgencia {r['urgencia']:.0%}  {texto}\n"
            f"               -> {r['detalle']}\n"
        )


async def bot_polling():
    import httpx

    from app.main import API_URL, procesar_update

    async with httpx.AsyncClient(timeout=40) as client:
        # Si había un webhook configurado, getUpdates no funciona: lo quitamos.
        await client.post(f"{API_URL}/deleteWebhook")
        print("Bot escuchando (Ctrl+C para salir)...")
        offset = 0
        while True:
            r = await client.get(f"{API_URL}/getUpdates", params={"offset": offset, "timeout": 30})
            for update in r.json().get("result", []):
                offset = update["update_id"] + 1
                await procesar_update(update)


if __name__ == "__main__":
    asyncio.run(bot_polling() if "--bot" in sys.argv else clasificar_ejemplos())
