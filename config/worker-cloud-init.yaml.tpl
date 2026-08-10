#cloud-config
# =============================================================================
# WORKER NODE CLOUD-INIT TEMPLATE
# =============================================================================
# You never paste this by hand. The autoscaler reads this file, substitutes
# __SWARM_TOKEN__ and __MANAGER_IP__ at creation time, and passes it to the
# Hetzner API as user_data.
#
# NOTE: user_data is readable from the instance metadata service by anything
# running on the box. A worker join token can only ever join as a worker, and
# the autoscaler rotates it on every scale-down cycle — but if you later run
# untrusted workloads here, switch to fetching the token over the private
# network from a short-lived endpoint on the master instead.
# =============================================================================

package_update: true

packages:
  - curl
  - jq
  - ufw

write_files:
  - path: /opt/worker/join.sh
    permissions: "0755"
    content: |
      #!/usr/bin/env bash
      set -euo pipefail
      exec > >(tee -a /var/log/worker-join.log) 2>&1

      PRIVATE_IP="$(ip -4 -o addr show | awk '$4 ~ /^10\./ {print $4}' | cut -d/ -f1 | head -n1)"
      if [ -z "$PRIVATE_IP" ]; then
        echo "FATAL: no private IP; worker must be created inside the private network" >&2
        exit 1
      fi

      # firewall — swarm ports only reachable inside the private network
      ufw --force reset
      ufw default deny incoming
      ufw default allow outgoing
      ufw allow 22/tcp
      ufw allow from 10.0.0.0/8
      ufw --force enable

      curl -fsSL https://get.docker.com | sh
      systemctl enable --now docker

      # loki log driver so app containers ship logs with zero extra agents
      docker plugin install grafana/loki-docker-driver:3.1.1 \
        --alias loki --grant-all-permissions || true

      # retry join — the manager may briefly be busy
      for i in $(seq 1 30); do
        if docker swarm join --advertise-addr "$PRIVATE_IP" \
             --token "__SWARM_TOKEN__" "__MANAGER_IP__:2377"; then
          echo "joined swarm on attempt $i"
          exit 0
        fi
        echo "join attempt $i failed, retrying in 10s"
        sleep 10
      done
      echo "FATAL: could not join swarm" >&2
      exit 1

runcmd:
  - [ bash, -lc, "/opt/worker/join.sh" ]
