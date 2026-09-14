# ---- Stage 1: build frontend ----
FROM node:22-slim AS frontend-build
WORKDIR /app/frontend
COPY frontend/package.json frontend/package-lock.json ./
RUN npm ci
COPY frontend/ ./
RUN npm run build

# ---- Stage 2: python runtime ----
FROM python:3.12-slim
WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY ticktick_notion_sync.py ./
COPY app/ ./app/
COPY --from=frontend-build /app/frontend/dist ./frontend/dist

# Cron mode entrypoint: run sync once and exit (hour-guarded via env)
# Web mode: overridden in render.yaml to uvicorn
CMD ["python", "ticktick_notion_sync.py"]
