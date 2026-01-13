FROM python:3.12-slim-bookworm

WORKDIR /app

# Install system dependencies needed for xmlsec and cryptography
RUN apt-get update && apt-get install -y \
    gcc \
    curl \
    libxml2-dev \
    libxmlsec1-dev \
    libxmlsec1-openssl \
    pkg-config \
    && rm -rf /var/lib/apt/lists/*

# Copy requirements first for better Docker layer caching
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy application code
COPY *.py .

# Copy migrations directory
COPY migrations/ migrations/

# Copy templates directory for PDF receipts
COPY templates/ templates/

# Copy fonts directory for PDF generation
COPY fonts/ fonts/

# Make migration script executable
RUN chmod +x migrate.py

# Set default environment variables for Gunicorn
ENV GUNICORN_WORKERS=2
ENV GUNICORN_TIMEOUT=60

# Create non-root user
RUN useradd -m -u 1000 appuser && chown -R appuser:appuser /app
USER appuser

EXPOSE 8000

# Health check configuration
HEALTHCHECK --interval=60s --timeout=10s --retries=3 --start-period=10s \
    CMD curl -f http://localhost:8000/health || exit 1

CMD ["sh", "-c", "python migrate.py && gunicorn --bind 0.0.0.0:8000 --workers $GUNICORN_WORKERS --timeout $GUNICORN_TIMEOUT app:app"]
