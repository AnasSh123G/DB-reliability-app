# One image: the Django API, the built frontend (a small Node server) and the served model.
# The database is NOT in the image: it lives on a volume mounted at /data (see deploy/compose.yaml).

FROM node:24-slim AS frontend
WORKDIR /app/bahn-buddy-predict
COPY bahn-buddy-predict/package.json bahn-buddy-predict/package-lock.json ./
RUN npm ci
COPY bahn-buddy-predict/ ./
RUN npm run build

FROM python:3.13-slim
COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv
# The frontend server runs on Node.
COPY --from=frontend /usr/local/bin/node /usr/local/bin/node
WORKDIR /app
COPY pyproject.toml uv.lock README.md ./
COPY src ./src
RUN uv sync --frozen --no-dev --extra web
COPY models ./models
COPY --from=frontend /app/bahn-buddy-predict/.output ./bahn-buddy-predict/.output
ENV DBREL_DB=/data/dbrel.sqlite \
    DBREL_DATA_DIR=/data \
    PATH="/app/.venv/bin:$PATH"
EXPOSE 8080
CMD ["dbrel", "serve", "--host", "0.0.0.0", "--port", "8080"]
