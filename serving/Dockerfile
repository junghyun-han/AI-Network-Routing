# Build from the repository root:
#   docker build -f serving/Dockerfile -t leo-router:latest .
FROM python:3.12-slim

WORKDIR /app

# CPU-only PyTorch first (kept in its own layer so app changes don't refetch it)
RUN pip install --no-cache-dir torch==2.3.1 --index-url https://download.pytorch.org/whl/cpu

COPY serving/requirements.txt /app/serving/requirements.txt
RUN pip install --no-cache-dir -r /app/serving/requirements.txt

# application code: the simulator physics + model and the service
COPY sim/ /app/sim/
COPY serving/ /app/serving/

ENV PYTHONPATH=/app \
    PYTHONUNBUFFERED=1 \
    MODEL_PATH=/app/serving/model/PPO-GCN_seed0.pt

EXPOSE 8000

# non-root runtime
RUN useradd -m appuser && chown -R appuser /app
USER appuser

HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://localhost:8000/health').status==200 else 1)"

CMD ["uvicorn", "serving.app:app", "--host", "0.0.0.0", "--port", "8000"]
