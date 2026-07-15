ARG PYTHON_VERSION=3.13
FROM python:${PYTHON_VERSION}-slim

ENV PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_NO_CACHE_DIR=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app

COPY pyproject.toml README.md LICENSE ./
COPY src ./src

RUN python -m pip install . \
    && useradd --create-home --shell /usr/sbin/nologin appuser

USER appuser

ENTRYPOINT ["crag"]
CMD ["--help"]
