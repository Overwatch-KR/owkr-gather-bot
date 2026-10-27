FROM python:3.13-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1
WORKDIR /app
COPY pyproject.toml README.md ./
COPY src ./src
RUN python -m pip install .
COPY migrations ./migrations
COPY scripts/backup.py ./scripts/backup.py
# Config and templates are supplied from the host; secrets never enter the image.
STOPSIGNAL SIGINT
CMD ["python", "-m", "src.main"]
