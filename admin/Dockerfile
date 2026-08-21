FROM python:3.12-slim

# The panel shells out for everything that touches the cluster:
#   docker  — stack deploy / service update, for components and for the three
#             infrastructure stacks. The CLI, not the daemon. Without it every
#             deploy button fails at runtime with "No such file or directory".
#   ssh     — the only way to reach the host itself (ufw), since the container
#             shares nothing with the host's network stack.
#   envsubst — bin/stack-deploy renders the infrastructure stacks with it.
ARG DOCKER_CLI_VERSION=27.3.1
RUN apt-get update \
 && apt-get install -y --no-install-recommends gettext-base curl ca-certificates openssh-client \
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

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY . .

EXPOSE 3000

# ONE worker on purpose. Login lockout counters and the per-process password
# salt live in memory, so a second worker would give an attacker a second
# lockout budget. Threads cover concurrency; this panel serves one operator.
CMD ["gunicorn", "--bind", "0.0.0.0:3000", "--workers", "1", "--threads", "4", \
     "--access-logfile", "-", "--error-logfile", "-", "app:app"]
