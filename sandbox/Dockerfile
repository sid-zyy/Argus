# Sandbox image for testing patches: docker build -t incident-commander-sandbox sandbox/
# Runs with --network none, a 256 MB memory cap and 1 CPU (see tools/sandbox.py).
FROM python:3.11-slim
RUN pip install --no-cache-dir pytest==9.1.1
RUN useradd -m sandbox
USER sandbox
