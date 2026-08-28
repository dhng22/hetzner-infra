FROM python:3.12-slim

# No docker CLI, unlike the autoscaler. This process reads Swarm and writes
# NODES (drain, label, remove) — all of which docker-py does correctly. The CLI
# is only needed for `service update --constraint-rm`, where docker-py's
# Service.update() resets every field you omit, and changing service specs is
# the autoscaler's job, not this one's.
RUN useradd -m -u 10002 overseer
WORKDIR /app

COPY overseer/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# The build context is the REPOSITORY ROOT, not overseer/, because `signals` is
# shared with the autoscaler and dataguard. A copy per image is the drift this
# repo keeps being bitten by: a query fixed in one process and not the other
# leaves two components disagreeing about the same service, silently.
COPY signals/ ./signals/
COPY overseer/overseer.py .

# 9210 is the metrics endpoint; 9211 receives node leases from a manager and is
# reachable on the monitoring overlay only, never published.
EXPOSE 9210 9211
CMD ["python", "-u", "overseer.py"]
