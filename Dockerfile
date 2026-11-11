# OPSMESH Dockerfile — production-ready container image.
# Uses pinned Python 3.12-slim, non-root execution, .dockerignore to prevent secrets.

FROM python:3.12-slim

# Create non-root user
RUN groupadd -r opsmesh && useradd -r -g opsmesh opsmesh

WORKDIR /app

# Copy requirements first (Docker layer caching)
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy the application (respects .dockerignore — .env, venv, __pycache__ excluded)
COPY --chown=opsmesh:opsmesh . .

# Switch to non-root user
USER opsmesh

# Expose the API port
EXPOSE 8000

# Default: start the API. Override with `python -m opsmesh.worker` for the worker.
CMD ["uvicorn", "opsmesh.api.app:create_app", "--factory", "--host", "0.0.0.0", "--port", "8000"]

# Health check (API only — worker doesn't expose HTTP)
HEALTHCHECK --interval=30s --timeout=5s --retries=3 --start-period=10s \
    CMD python -c "import httpx; r = httpx.get('http://localhost:8000/health'); exit(0 if r.status_code == 200 else 1)" || exit 1
