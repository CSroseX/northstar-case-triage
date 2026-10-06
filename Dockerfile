FROM python:3.12-slim

# Never write .pyc, never buffer logs (so container logs appear immediately).
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PORT=8080

WORKDIR /app

# Dependencies first so code edits do not invalidate the layer.
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY app/ ./app/

# Run unprivileged.
RUN useradd --create-home --uid 10001 appuser
USER appuser

EXPOSE 8080

# No secrets are baked into the image: credentials arrive via `docker run --env-file`
# or -e at run time. .env is excluded by .dockerignore.
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8080"]
