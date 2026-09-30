# syntax=docker/dockerfile:1
ARG PYTHON_IMAGE=python:3.13-slim-bookworm
ARG UV_IMAGE=ghcr.io/astral-sh/uv:0.8

FROM ${UV_IMAGE} AS uv

# --- build: install into a venv with uv ----------------------------------------------
FROM ${PYTHON_IMAGE} AS build
COPY --from=uv /uv /usr/local/bin/uv
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy UV_PYTHON_DOWNLOADS=0
WORKDIR /app
RUN --mount=type=cache,target=/root/.cache/uv \
    --mount=type=bind,source=uv.lock,target=uv.lock \
    --mount=type=bind,source=pyproject.toml,target=pyproject.toml \
    uv sync --locked --no-install-project --no-dev --extra web --extra rekordbox
COPY pyproject.toml uv.lock README.md LICENSE ./
COPY src ./src
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --locked --no-dev --extra web --extra rekordbox --no-editable

# --- runtime -------------------------------------------------------------------------
FROM ${PYTHON_IMAGE}
COPY --from=build /app/.venv /app/.venv
ENV PATH="/app/.venv/bin:$PATH" \
    PYTHONUNBUFFERED=1 \
    EXPORT_DIR=/export \
    UPLOAD_DIR=/tmp/uploads
EXPOSE 8000
ENTRYPOINT ["djconvert"]
CMD ["serve", "--host", "0.0.0.0", "--port", "8000"]
