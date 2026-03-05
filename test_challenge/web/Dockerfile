FROM python:3.11-slim

RUN apt-get update && apt-get install -y --no-install-recommends \
    iputils-ping curl wget netcat-openbsd \
    && rm -rf /var/lib/apt/lists/*

RUN pip install flask --no-cache-dir

WORKDIR /app
COPY app.py .

EXPOSE 5000
CMD ["python", "app.py"]
