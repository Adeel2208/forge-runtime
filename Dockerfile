# syntax=docker/dockerfile:1

# Build stage: resolve dependencies against a wheel, so the runtime image never
# carries a compiler or a build cache.
FROM python:3.12-slim AS build

ENV PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /build
COPY pyproject.toml README.md ./
COPY forge ./forge

RUN pip install --no-cache-dir build hatchling \
 && python -m build --wheel --outdir /dist


FROM python:3.12-slim AS runtime

# Run unprivileged. The sandbox is not built yet, so the process boundary is
# currently the only isolation a tool call has - see the scope note in README.
RUN groupadd --gid 10001 forge \
 && useradd --uid 10001 --gid forge --create-home --shell /usr/sbin/nologin forge

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    FORGE_DATABASE_URL=sqlite:////data/forge.db

WORKDIR /app
COPY --from=build /dist/*.whl /tmp/
RUN pip install --no-cache-dir /tmp/*.whl "forge-runtime[api]" \
 && rm -rf /tmp/*.whl

# The event log lives on a volume. Mount durable storage here: losing this
# directory loses every in-flight run's ability to resume.
RUN mkdir -p /data && chown forge:forge /data
VOLUME ["/data"]

COPY --chown=forge:forge cases ./cases

USER forge
EXPOSE 8080

# Liveness only - it touches nothing but the process, so a slow database
# cannot get a healthy container killed. Readiness is /readyz, which the
# orchestrator should poll separately.
HEALTHCHECK --interval=30s --timeout=3s --start-period=10s --retries=3 \
    CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8080/livez', timeout=2).status==200 else 1)"

# One worker per process on purpose: the SQLite backend has a single writer,
# and the supervisor claims runs by lease. Scale by running more containers
# against a shared Postgres once that backend lands (ADR-0004).
CMD ["uvicorn", "forge.api:create_app", "--factory", \
     "--host", "0.0.0.0", "--port", "8080", \
     "--timeout-graceful-shutdown", "30"]
