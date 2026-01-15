# Dockerfile dla Revolut PIT-8C Converter
FROM python:3.12-slim

# Ustawienie zmiennych środowiskowych
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

# Katalog roboczy
WORKDIR /app

# Instalacja zależności systemowych
RUN apt-get update && \
    apt-get install -y --no-install-recommends \
    gcc \
    && rm -rf /var/lib/apt/lists/*

# Utworzenie użytkownika non-root dla bezpieczeństwa
RUN useradd --create-home --shell /bin/bash appuser

# Kopiowanie plików wymagań i instalacja zależności
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Kopiowanie kodu aplikacji
COPY main.py .
COPY revolut_to_pit8c.py .
COPY example_revolut_statement.csv .
COPY README.md .

# Utworzenie katalogu na logi z odpowiednimi uprawnieniami
RUN mkdir -p /app/logs && chown -R appuser:appuser /app

# Przełączenie na użytkownika non-root
USER appuser

# Port aplikacji
EXPOSE 8000

# Healthcheck
HEALTHCHECK --interval=30s --timeout=10s --start-period=5s --retries=3 \
    CMD python -c "import requests; requests.get('http://localhost:8000/health', timeout=5)" || exit 1

# Komenda uruchomieniowa
CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8000"]
