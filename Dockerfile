FROM python:3.13-slim@sha256:e48f59cdba69a3cbd9ed2cf0f348d90d45a8d350d98134cf1070bc106ee9ed90
COPY selector/ /sel/
# The pinned source tree: fetched from the project's repository (with retries); if that is not
# possible, the snapshot of the same commit shipped in selector/tree.tar.gz is used instead.
RUN (apt-get update && apt-get install -y --no-install-recommends git ca-certificates && rm -rf /var/lib/apt/lists/*) || echo "apt unavailable"
RUN set -e; ok=0; \
    for i in 1 2 3; do \
      rm -rf /app; \
      if command -v git >/dev/null 2>&1 && git init -q /app && cd /app \
         && git remote add origin https://github.com/tobymao/sqlglot.git \
         && git fetch -q --depth 1 origin eb11aec4be8cb2122afe713a4816662c3f0fb2fa \
         && git checkout -q FETCH_HEAD; then ok=1; break; fi; \
      cd /; sleep 3; \
    done; \
    if [ "$ok" = 0 ]; then echo "using bundled snapshot"; rm -rf /app; mkdir -p /app; tar -xzf /sel/tree.tar.gz -C /app; fi; \
    rm -rf /app/.git; chmod -R a+rX /app /sel; \
    test -f /app/sqlglot/parser.py && test -f /app/tests/helpers.py
RUN python -c "import gzip,pickle; pickle.load(gzip.open('/sel/index.pkl.gz'))"
ENV REPO_DIR=/app DATA_DIR=/data PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
CMD ["python", "/sel/select.py"]
