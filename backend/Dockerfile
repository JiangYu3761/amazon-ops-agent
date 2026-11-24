FROM python:3.12-slim
WORKDIR /app
COPY backend/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY backend/app ./app
COPY db /app/db
COPY frontend/catalog.json /app/catalog.json
COPY frontend/workbench-growth.json frontend/workbench-operations.json frontend/workbench-workspace.json /app/workbench/
ENV WORKBENCH_DEFINITION_DIR=/app/workbench
ENV CATALOG_FILE=/app/catalog.json
CMD ["sh", "-ec", "python -m app.migrate && exec uvicorn app.main:app --host 0.0.0.0 --port 8000"]

