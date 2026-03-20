FROM python:3.12-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

EXPOSE 8080

CMD ["gunicorn", "-k", "geventwebsocket.gunicorn.workers.GeventWebSocketWorker", "-w", "2", "--worker-connections", "200", "--timeout", "300", "--keep-alive", "120", "--graceful-timeout", "30", "--max-requests", "500", "--max-requests-jitter", "50", "-b", "0.0.0.0:8080", "app:app"]
