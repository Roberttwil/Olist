# ==========================================
# Stage 1: Build dependencies
# ==========================================
FROM python:3.10-slim AS builder

WORKDIR /app

# Install compilation tools required for compiling package dependencies (like psycopg2 or numpy if wheels are missing)
RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential \
    libpq-dev \
    gcc \
    python3-dev \
    && rm -rf /var/lib/apt/lists/*

# Create virtual environment to isolate installed dependencies
RUN python -m venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH"

# Copy and install dependencies
COPY requirements.txt .
RUN pip install --no-cache-dir --upgrade pip && \
    pip install --no-cache-dir -r requirements.txt

# ==========================================
# Stage 2: Final runner image
# ==========================================
FROM python:3.10-slim AS runner

WORKDIR /app

# Install runtime PostgreSQL client library (libpq5) required by psycopg2
RUN apt-get update && apt-get install -y --no-install-recommends \
    libpq5 \
    && rm -rf /var/lib/apt/lists/*

# Copy virtual environment from builder stage
COPY --from=builder /opt/venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH"

# Copy project files required by the FastAPI service
COPY static/ ./static/
COPY ingest.py database.py agent.py main.py ./

# Expose FastAPI server port
EXPOSE 8000

# Set environment variables
ENV PYTHONUNBUFFERED=1

# Default command to run the FastAPI ASGI web server
CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8000"]
