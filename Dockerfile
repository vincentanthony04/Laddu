FROM python:3.12-slim

WORKDIR /app

COPY requirements-runtime.txt .
RUN pip install --no-cache-dir -r requirements-runtime.txt

COPY backend/ ./backend/
COPY frontend/ ./frontend/

ENV PYTHONPATH=/app/backend
ENV PROJECT_LADDU_PORT=8086

EXPOSE 8086

# Data-plane services (Postgres, QuestDB) are provided separately via
# infra/compose/docker-compose.yml, not bundled in this image.
CMD ["python", "backend/main.py"]
