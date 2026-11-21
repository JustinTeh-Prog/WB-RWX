FROM python:3.13-slim@sha256:e48f59cdba69a3cbd9ed2cf0f348d90d45a8d350d98134cf1070bc106ee9ed90
RUN apt-get update && apt-get install -y --no-install-recommends git ca-certificates && rm -rf /var/lib/apt/lists/*
RUN git init -q /app && cd /app && git remote add origin https://github.com/tobymao/sqlglot.git \
 && git fetch -q --depth 1 origin eb11aec4be8cb2122afe713a4816662c3f0fb2fa && git checkout -q FETCH_HEAD \
 && rm -rf /app/.git && chmod -R a+rX /app
COPY selector/ /sel/
RUN chmod -R a+rX /sel && python -c "import gzip,pickle; pickle.load(gzip.open('/sel/index.pkl.gz'))"
ENV REPO_DIR=/app DATA_DIR=/data PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
CMD ["python", "/sel/select.py"]
