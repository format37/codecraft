FROM python:3.12-slim
RUN apt-get update && apt-get install -y --no-install-recommends git ca-certificates fonts-dejavu-core \
    && rm -rf /var/lib/apt/lists/*
COPY requirements.txt /tmp/requirements.txt
RUN pip install --no-cache-dir -r /tmp/requirements.txt
ENV PYTHONPATH=/app/server CW_ROOT=/app PYTHONUNBUFFERED=1 HOME=/tmp
WORKDIR /app
ENTRYPOINT ["python", "-m", "codecraft"]
