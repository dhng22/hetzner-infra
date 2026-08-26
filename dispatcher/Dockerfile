FROM python:3.12-slim

# No docker CLI and no Hetzner SDK, unlike the autoscaler. This process reads
# and reports; it changes nothing. Keeping it that way is most of the reason it
# was split out of a component that holds a token able to delete the fleet.
RUN useradd -m -u 10002 dispatcher
WORKDIR /app

COPY dispatcher/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# The build context is the REPOSITORY ROOT, not dispatcher/, because `signals`
# is shared with the autoscaler. A copy per image is the drift this repo keeps
# being bitten by: a query fixed in one process and not the other leaves two
# components disagreeing about whether the same service is slow, silently.
COPY signals/ ./signals/
COPY dispatcher/dispatcher.py .

EXPOSE 9210
CMD ["python", "-u", "dispatcher.py"]
