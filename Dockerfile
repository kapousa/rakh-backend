# RAKH Backend — FastAPI service
FROM python:3.11-slim

# System deps: build tools for pandas/cryptography wheels, and fonts config
# for the Arabic PDF rendering (Amiri fonts are bundled in app/fonts/, but
# fontconfig/libfreetype are needed by matplotlib's chart rendering).
RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential \
    libfreetype6-dev \
    libpng-dev \
    fontconfig \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# Non-root user — don't run the app as root in production
RUN useradd --create-home --shell /bin/bash rakh \
    && chown -R rakh:rakh /app
USER rakh

EXPOSE 8000

# Basic container-level health check hitting the app's own /api/health route
HEALTHCHECK --interval=30s --timeout=5s --start-period=15s --retries=3 \
    CMD python -c "import httpx; httpx.get('http://localhost:8000/api/health').raise_for_status()" || exit 1

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
