# Hokma in a container: the server and the CLI, nothing else.
# Build from the repository root:  docker build -f deploy/docker/Dockerfile -t hokma .
FROM python:3.12-slim AS build
COPY --from=ghcr.io/astral-sh/uv:0.11.2 /uv /usr/local/bin/uv
WORKDIR /app
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy UV_PYTHON_DOWNLOADS=never
COPY pyproject.toml uv.lock README.md LICENSE ./
COPY backend ./backend
COPY cli ./cli
COPY kit ./kit
COPY contracts ./contracts
RUN uv sync --frozen --no-dev

FROM python:3.12-slim
RUN useradd --create-home --shell /usr/sbin/nologin hokma
WORKDIR /app
COPY --from=build /app /app
ENV PATH="/app/.venv/bin:$PATH" HOKMA_DATA_DIR=/data
RUN mkdir -p /data && chown hokma:hokma /data
USER hokma
VOLUME ["/data"]
EXPOSE 8765
HEALTHCHECK --interval=30s --timeout=5s CMD ["hokma", "doctor"]
CMD ["hokma", "start", "--no-browser", "--port", "8765"]
