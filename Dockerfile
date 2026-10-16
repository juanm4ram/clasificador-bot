FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1 PIP_DISABLE_PIP_VERSION_CHECK=1
WORKDIR /srv

RUN pip install --upgrade pip setuptools

COPY requirements.txt .
# torch en su versión solo-CPU (mucho más liviana que la que trae soporte para GPU)
RUN pip install --index-url https://download.pytorch.org/whl/cpu torch==2.14.1 \
 && pip install -r requirements.txt

COPY app ./app

# El modelo se descarga al construir la imagen (versión fija, solo archivos safetensors)...
ENV MODELO_DIR=/srv/modelo
RUN python -c "from app.graph import descargar_modelo; descargar_modelo()"
# ...y en producción el servidor nunca descarga nada de internet.
ENV HF_HUB_OFFLINE=1 TRANSFORMERS_OFFLINE=1

# No correr como root
RUN useradd --create-home bot
USER bot

# Cloud Run define la variable PORT (por defecto 8080)
CMD exec uvicorn app.main:app --host 0.0.0.0 --port ${PORT:-8080} --no-server-header
