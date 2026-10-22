FROM python:3.12-slim
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
WORKDIR /srv
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY app ./app
RUN useradd -m appuser && chown -R appuser /srv
USER appuser
EXPOSE 8015
# One worker: the ingestion scheduler runs inside the process. Scale out by running the scheduler separately.
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8015"]
