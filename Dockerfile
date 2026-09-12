FROM python:3.11-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    ROLE=api

WORKDIR /app
COPY requirements.docker.txt .
RUN pip install --no-cache-dir -r requirements.docker.txt

# NOTE: engine code ships but never boots here (ROLE=api). The worker
# (worker.py) runs on the machine that owns Chrome + the Google profile.
COPY flow_api.py flow_server.py flow_store.py worker.py gen.py menu.py flow.py ./

RUN mkdir -p outputs sessions logs
EXPOSE 8787
CMD ["sh", "-c", "python -u flow_server.py --host 0.0.0.0 --port ${PORT:-8787}"]
