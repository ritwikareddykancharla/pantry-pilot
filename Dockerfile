# Container-build alternative for Amazon Bedrock AgentCore Runtime (ARM64).
FROM --platform=linux/arm64 python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PANTRYPILOT_STATE_DIR=/tmp/pantrypilot

WORKDIR /app

COPY requirements.txt pyproject.toml ./
RUN pip install --no-cache-dir -r requirements.txt

COPY main.py ./
COPY src ./src
COPY data ./data
RUN pip install --no-cache-dir -e . \
    && useradd --create-home --uid 1000 pantry \
    && mkdir -p /tmp/pantrypilot \
    && chown -R pantry:pantry /app /tmp/pantrypilot

USER pantry
EXPOSE 8080
CMD ["python", "main.py"]
