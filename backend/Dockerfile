FROM python:3.12-slim
WORKDIR /app
COPY backend/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY backend/app ./app
COPY frontend/catalog.json /app/catalog.json
ENV CATALOG_FILE=/app/catalog.json
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]

