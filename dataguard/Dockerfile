FROM python:3.12-slim

# The docker CLI, for the one thing docker-py cannot do safely: changing a
# member service's replica count and node constraint without resetting every
# other field of its spec. docker-py's Service.update() sends what you give it
# and clears the rest — networks, secrets, update policy, the lot — and a
# partial spec update against a running database is not a mistake you find out
# about quickly.
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

WORKDIR /app

COPY dataguard/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# The build context is the REPOSITORY ROOT, because `signals` is shared with the
# overseer and the autoscaler. Three copies of one library is three places for a
# fix to land in two of them.
COPY signals/ ./signals/
COPY pki/ ./pki/
COPY dataguard/dataguard.py dataguard/engines.py dataguard/plan.py ./

# Runs as root because it holds the docker socket and writes a certificate
# authority's private key to a bind mount owned by root.
#
# 9203 is metrics. 9202 receives dispatch and is bound to the MONITORING address
# only — never 0.0.0.0 — because this is the one infrastructure service also
# attached to `edge`, where every application lives.
EXPOSE 9202 9203
CMD ["python", "-u", "dataguard.py"]
