FROM python:3.12-slim

WORKDIR /app

# Install Kerberos system libraries (required by requests-kerberos / gssapi)
# krb5-multidev provides the krb5-config binary needed during pip install
# krb5-user provides klist for runtime diagnostics
RUN apt-get update && apt-get install -y --no-install-recommends \
    gcc \
    pkg-config \
    python3-dev \
    libkrb5-dev \
    krb5-multidev \
    libgssapi-krb5-2 \
    krb5-user && \
    rm -rf /var/lib/apt/lists/*

# Verify krb5-config is available before pip install
RUN which krb5-config || (echo "ERROR: krb5-config not found in PATH" && exit 1)

# Install dependencies
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy application
COPY . .

# Non-root user for security
RUN useradd -m appuser && chown -R appuser:appuser /app
USER appuser

EXPOSE 8080

# Ensure Python output is not buffered (so logs appear in kubectl logs)
ENV PYTHONUNBUFFERED=1

# Use gunicorn with gevent for production
CMD ["gunicorn", "-k", "geventwebsocket.gunicorn.workers.GeventWebSocketWorker", "-w", "2", "--worker-connections", "200", "--timeout", "300", "--keep-alive", "120", "--graceful-timeout", "30", "--max-requests", "500", "--max-requests-jitter", "50", "-b", "0.0.0.0:8080", "app:app"]
