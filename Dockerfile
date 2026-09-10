FROM python:3.12-slim-bookworm AS build

ENV PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /build

RUN python -m venv /opt/venv
ENV PATH="/opt/venv/bin:${PATH}"

COPY pyproject.toml *.md ./
COPY src ./src

RUN python -m pip install --no-cache-dir .


FROM python:3.12-slim-bookworm AS runtime

ENV PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PATH="/opt/venv/bin:${PATH}" \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app

RUN groupadd --gid 10001 cxplorer \
    && useradd --uid 10001 --gid cxplorer --create-home \
        --home-dir /home/cxplorer --shell /usr/sbin/nologin cxplorer

COPY --from=build /opt/venv /opt/venv

COPY --chown=cxplorer:cxplorer \
    app_config.env id_vendor.env ai_vendors.env ai_tasks.env ./

USER cxplorer

ENV LISTEN_PORT=8000
EXPOSE 8000

# Prevents Python from writing pyc files to disc (equivalent to python -B option)
ENV PYTHONDONTWRITEBYTECODE=1
# Prevents Python from buffering stdout and stderr (equivalent to python -u option)
ENV PYTHONUNBUFFERED=1

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD ["python", "-c", "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/api/health', timeout=3).close()"]

# Jobs and reports are process-local, so the container runs one application worker.
CMD ["python", "-m", "uvicorn", "cxplorer.main:create_app", "--factory", "--host", "0.0.0.0", "--port", "8000"]
