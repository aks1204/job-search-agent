FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PYTHONPATH=/app \
    EMBEDDING_CACHE_DIR=/opt/models

RUN useradd --create-home --uid 1000 app \
 && mkdir -p /opt/models /var/lib/job-agent \
 && chown app:app /opt/models /var/lib/job-agent

WORKDIR /app
COPY requirements.txt requirements-dev.txt ./
RUN pip install -r requirements.txt -r requirements-dev.txt

# Bake the embedding model into the image so containers start without a download.
USER app
RUN python -c "from fastembed import TextEmbedding; TextEmbedding(model_name='BAAI/bge-small-en-v1.5', cache_dir='/opt/models')"

COPY --chown=app:app . .
EXPOSE 8000
