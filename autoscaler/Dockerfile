FROM python:3.12-slim

# The docker CLI, for one job docker-py cannot do safely: moving the app
# service's placement constraint between manager-only and worker-only mode.
#
# docker-py's Service.update() sends whatever you pass it and RESETS every
# field you leave out — networks, update_config, labels, secrets, the lot. A
# partial spec update against a live production service is how you silently
# lose the rolling-update policy. `docker service update --constraint-add /
# --constraint-rm` is surgical: it touches the constraint list and nothing
# else, and it honours the update_config already on the service.
ARG DOCKER_CLI_VERSION=27.3.1
RUN apt-get update \
 && apt-get install -y --no-install-recommends curl ca-certificates \
 && arch="$(dpkg --print-architecture)" \
 && case "$arch" in \
      amd64) dockerarch=x86_64 ;; \
      arm64) dockerarch=aarch64 ;; \
      *) echo "unsupported architecture: $arch" >&2; exit 1 ;; \
    esac \
 && curl -fsSL "https://download.docker.com/linux/static/stable/${dockerarch}/docker-${DOCKER_CLI_VERSION}.tgz" \
    | tar -xz -C /usr/local/bin --strip-components=1 docker/docker \
 && docker --version \
 && apt-get purge -y curl && apt-get autoremove -y \
 && rm -rf /var/lib/apt/lists/*

RUN useradd -m -u 10001 scaler
WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY autoscaler.py .

# Runs as root only because it needs the docker socket. If you prefer, add the
# scaler user to the host's docker group GID and drop back with USER scaler.
EXPOSE 9200
CMD ["python", "-u", "autoscaler.py"]
