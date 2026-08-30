# Stage 1: Build Frontend Assets
FROM node:22-slim AS frontend-builder
WORKDIR /app
COPY package.json package-lock.json tsconfig.json vite.config.ts ./
COPY careerradar/web/frontend-src/ ./careerradar/web/frontend-src/
RUN npm ci && npm run build

# Stage 2: Python Runtime
FROM python:3.12-slim
WORKDIR /app

# Install git for uv git dependencies (e.g. JobSpy) and ca-certificates
RUN apt-get update && apt-get install -y --no-install-recommends \
    git \
    ca-certificates \
    && rm -rf /var/lib/apt/lists/*

COPY --from=ghcr.io/astral-sh/uv:latest /uv /bin/uv

# Install Python dependencies (layer cached)
COPY pyproject.toml uv.lock README.md ./
RUN uv sync --frozen --no-dev --no-install-project

# Copy application source code and configurations
COPY careerradar/ ./careerradar/
COPY data/ ./data/
COPY templates/ ./templates/
COPY config.yaml ./config.yaml
COPY --from=frontend-builder /app/careerradar/web/frontend/dist/ ./careerradar/web/frontend/dist/

# Install the careerradar package
RUN uv sync --frozen --no-dev

ENV PATH="/app/.venv/bin:$PATH"
ENV PYTHONUNBUFFERED=1

EXPOSE 8010
CMD ["careerradar", "start", "--host", "0.0.0.0", "--port", "8010"]
