# Workbench image (hosted demo). Build: docker build -t tangible-workbench .
FROM python:3.12-slim

COPY --from=ghcr.io/astral-sh/uv:0.10.12 /uv /usr/local/bin/uv

ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never \
    PYTHONUNBUFFERED=1 \
    PATH="/app/.venv/bin:$PATH"

WORKDIR /app

# Dependencies first, so code changes don't reinstall them.
COPY pyproject.toml uv.lock ./
RUN uv sync --locked --no-dev --no-install-project

COPY src ./src
COPY policies ./policies
RUN uv sync --locked --no-dev

# Runs as root: Railway mounts volumes root-owned, and the evidence store (/data) is one.
ENV TANGIBLE_HOST=0.0.0.0 \
    TANGIBLE_EVIDENCE_DIR=/data/evidence

EXPOSE 8000
CMD ["tangible-workbench"]
