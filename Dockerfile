# =============================================================================
# Multi-stage build for production
# =============================================================================


# -----------------------------------------------------------------------------
# Stage 1 — Builder
# -----------------------------------------------------------------------------
    FROM python:3.11-slim AS builder

    WORKDIR /build
    
    RUN apt-get update \
        && apt-get install -y --no-install-recommends \
            gcc \
            libpango-1.0-0 \
            libpangoft2-1.0-0 \
        && rm -rf /var/lib/apt/lists/*
    
    COPY pyproject.toml ./
    COPY src/           ./src/
    
    RUN pip install --user --no-cache-dir .
    
    
    # -----------------------------------------------------------------------------
    # Stage 2 — Runtime
    # -----------------------------------------------------------------------------
    FROM python:3.11-slim AS runtime
    
    WORKDIR /app
    
    # System dependencies + font cache
    RUN apt-get update \
        && apt-get install -y --no-install-recommends \
            libpango-1.0-0 \
            libpangoft2-1.0-0 \
            fontconfig \
            gosu \
        && rm -rf /var/lib/apt/lists/* \
        && fc-cache -fv
    
    # Unprivileged user
    RUN useradd -m -u 1000 ff-user \
        && chown -R ff-user:ff-user /app
    
    # Copy installed packages from builder
    COPY --from=builder --chown=ff-user:ff-user /root/.local /home/ff-user/.local
    
    # Application source
    COPY --chown=ff-user:ff-user src/ ./src/
    
    # Entrypoint script
    COPY docker-entrypoint.sh /usr/local/bin/docker-entrypoint.sh
    RUN chmod +x /usr/local/bin/docker-entrypoint.sh
    
    # Environment
    ENV PATH=/home/ff-user/.local/bin:$PATH \
        PYTHONUNBUFFERED=1 \
        PYTHONDONTWRITEBYTECODE=1 \
        FC_CACHEDIR=/tmp
    
    # Health check
    HEALTHCHECK --interval=30s --timeout=3s --start-period=5s --retries=3 \
        CMD python -c "import urllib.request; urllib.request.urlopen('http://localhost:8000/health')" || exit 1
    
    EXPOSE 8000
    
    ENTRYPOINT ["docker-entrypoint.sh"]
    CMD ["python", "-m", "uvicorn", "main:app", \
         "--host", "0.0.0.0", \
         "--port", "8000", \
         "--app-dir", "/app/src"]