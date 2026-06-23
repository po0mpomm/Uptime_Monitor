FROM python:3.11-slim

WORKDIR /app

# Install dependencies first (better layer caching)
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy shared app package and worker
COPY ./app ./app
COPY ./worker ./worker

# Default command runs the API server.
# The worker service in docker-compose.yml overrides this with:
#   command: python -m worker.main
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
