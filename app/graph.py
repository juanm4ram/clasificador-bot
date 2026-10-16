"""Grafo LangGraph que clasifica el mensaje de un cliente y lo deriva a un sector.

La clasificación usa un modelo abierto de "zero-shot classification" (bge-m3-zeroshot-v2.0, licencia MIT)
que corre dentro del mismo servidor: no hay APIs externas ni costo por mensaje.

Flujo:
    START -> clasificar -> (enrutar) -> ventas | facturacion | soporte | reclamos | otros -> END
"""

import os
from functools import lru_cache
from pathlib import Path
from typing import TypedDict

from langgraph.graph import END, START, StateGraph

# --- Modelo -------------------------------------------------------------------
MODELO_REPO = "MoritzLaurer/bge-m3-zeroshot-v2.0"
# Versión fija (commit) del modelo: evita que un cambio en Hugging Face altere lo que corre en producción.
MODELO_REVISION = "9abf1c8aaeb82a2447809c20753ed0b106b76652"
# Solo se descargan estos archivos. Los pesos van en formato safetensors, que no ejecuta código al
# cargarse (a diferencia de los archivos pickle .bin/.pt, que no se descargan).
MODELO_ARCHIVOS = [
    "config.json",
    "model.safetensors",
    "tokenizer.json",
    "tokenizer_config.json",
    "special_tokens_map.json",
    "sentencepiece.bpe.model",
]
MODELO_DIR = Path(os.getenv("MODELO_DIR", Path(__file__).resolve().parent.parent / "modelo"))

# Mensajes más largos se recortan: acota el uso de CPU por mensaje (un texto enorme podría
# tardar decenas de segundos y saturar el servidor).
MAX_CARACTERES = 300

# --- Sectores -----------------------------------------------------------------
# Para agregar o quitar un sector basta con editar este diccionario.
# "hipotesis" son frases que completan "El cliente ..."; el modelo mide cuánto se cumple cada una y el
# sector toma el puntaje de su mejor frase. Agregar frases es la forma de mejorar la precisión.
SECTORES = {
    "ventas": {
        "nombre": "Ventas",
        "emoji": "🛒",
        "hipotesis": [
            "quiere comprar o contratar algo",
            "pregunta por precios, planes, cotizaciones o stock",
            "quiere saber cuánto cuesta un producto o un plan",
        ],
    },
    "facturacion": {
        "nombre": "Facturación",
        "emoji": "🧾",
        "hipotesis": [
            "habla de una factura, un cobro o una deuda",
            "pregunta por pagos o medios de pago",
            "hizo un pago o una transferencia",
        ],
    },
    "soporte": {
        "nombre": "Soporte técnico",
        "emoji": "🛠️",
        "hipotesis": [
            "tiene un problema técnico",
            "dice que algo no funciona, no carga o da error",
            "no puede ingresar a su cuenta",
            "olvidó su contraseña",
            "pide ayuda para usar o configurar el producto",
        ],
    },
    "reclamos": {
        "nombre": "Reclamos",
        "emoji": "📣",
        "hipotesis": [
            "se queja de la atención o del servicio",
            "quiere cancelar o dar de baja el servicio",
        ],
    },
    # "otros" no tiene hipótesis: recibe lo que no supera el umbral en ningún sector.
    "otros": {"nombre": "Atención general", "emoji": "💬", "hipotesis": []},
}
HIPOTESIS_URGENTE = "necesita una respuesta urgente o inmediata"
PLANTILLA = "El cliente {}."

# Si ningún sector llega a este puntaje, el mensaje va a "otros" (atención humana general).
UMBRAL_CONFIANZA = float(os.getenv("UMBRAL_CONFIANZA", "0.4"))


class Estado(TypedDict, total=False):
    mensaje: str
    usuario: str
    sector: str
    confianza: float
    urgencia: float  # probabilidad (0 a 1) de que el mensaje sea urgente
    detalle: str  # puntajes por sector o error, para logs y para el equipo
    respuesta: str  # texto para el cliente
    derivacion: str  # texto para el equipo del sector


def descargar_modelo() -> Path:
    """Descarga el modelo (solo los archivos seguros, en la versión fija) si todavía no está en MODELO_DIR."""
    if not (MODELO_DIR / "model.safetensors").exists():
        from huggingface_hub import snapshot_download

        snapshot_download(
            MODELO_REPO, revision=MODELO_REVISION, allow_patterns=MODELO_ARCHIVOS, local_dir=MODELO_DIR
        )
    return MODELO_DIR


@lru_cache
def _clasificador():
    # Import diferido: importar el grafo no carga torch ni el modelo (~1 GB).
    from transformers import pipeline

    return pipeline(
        "zero-shot-classification",
        model=str(descargar_modelo()),
        model_kwargs={"use_safetensors": True},
        device=-1,  # CPU
    )


def cargar_modelo() -> None:
    """Carga el modelo en memoria por adelantado (se llama al arrancar el servidor)."""
    _clasificador()


# --- Nodos --------------------------------------------------------------------
def clasificar(estado: Estado) -> Estado:
    hipotesis = {h: clave for clave, s in SECTORES.items() for h in s["hipotesis"]}
    try:
        r = _clasificador()(
            estado["mensaje"][:MAX_CARACTERES],
            candidate_labels=[*hipotesis, HIPOTESIS_URGENTE],
            hypothesis_template=PLANTILLA,
            multi_label=True,  # cada frase se evalúa por separado (0 a 1)
        )
    except Exception as e:  # si el modelo falla, no dejamos al cliente sin respuesta
        return {"sector": "otros", "confianza": 0.0, "urgencia": 0.0, "detalle": f"Error al clasificar: {e}"}

    puntajes = dict(zip(r["labels"], r["scores"]))
    por_sector: dict[str, float] = {}
    for h, clave in hipotesis.items():
        por_sector[clave] = max(por_sector.get(clave, 0.0), puntajes[h])

    ranking = sorted(por_sector.items(), key=lambda kv: kv[1], reverse=True)
    sector, confianza = ranking[0]
    return {
        "sector": sector,
        "confianza": confianza,
        "urgencia": puntajes[HIPOTESIS_URGENTE],
        "detalle": ", ".join(f"{k} {v:.0%}" for k, v in ranking[:3]),
    }


def enrutar(estado: Estado) -> str:
    if estado.get("confianza", 0) < UMBRAL_CONFIANZA or estado.get("sector") not in SECTORES:
        return "otros"
    return estado["sector"]


def _nodo_sector(clave: str):
    sector = SECTORES[clave]

    def nodo(estado: Estado) -> Estado:
        respuesta = (
            f"{sector['emoji']} ¡Gracias por tu mensaje! Tu consulta fue derivada al área de "
            f"*{sector['nombre']}*. Un representante te va a responder a la brevedad."
        )
        urgente = "🔴 URGENTE\n" if estado.get("urgencia", 0) >= 0.5 else ""
        derivacion = (
            f"{urgente}{sector['emoji']} Nueva consulta para {sector['nombre']}\n"
            f"Cliente: {estado.get('usuario', 'desconocido')}\n"
            f"Confianza: {estado.get('confianza', 0):.0%}\n"
            f"Puntajes: {estado.get('detalle', '-')}\n\n"
            f"Mensaje:\n{estado['mensaje']}"
        )
        return {"sector": clave, "respuesta": respuesta, "derivacion": derivacion}

    return nodo


# --- Construcción del grafo ---------------------------------------------------
def construir_grafo():
    g = StateGraph(Estado)
    g.add_node("clasificar", clasificar)
    for clave in SECTORES:
        g.add_node(clave, _nodo_sector(clave))
        g.add_edge(clave, END)

    g.add_edge(START, "clasificar")
    g.add_conditional_edges("clasificar", enrutar, {clave: clave for clave in SECTORES})
    return g.compile()


grafo = construir_grafo()
