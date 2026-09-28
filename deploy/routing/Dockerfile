FROM python:3.12-slim

WORKDIR /app
COPY pyproject.toml /app/
COPY backend /app/backend
ENV PYTHONPATH=/app

USER 65532:65532
ENTRYPOINT ["python", "-m", "backend.routing_gateway"]
