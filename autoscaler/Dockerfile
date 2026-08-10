FROM python:3.12-slim

RUN useradd -m -u 10001 scaler
WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY autoscaler.py .

# Runs as root only because it needs the docker socket. If you prefer, add the
# scaler user to the host's docker group GID and drop back with USER scaler.
EXPOSE 9200
CMD ["python", "-u", "autoscaler.py"]
