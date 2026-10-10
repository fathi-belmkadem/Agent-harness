FROM python:3.12-slim-bookworm

WORKDIR /app

# Node/npx is needed to run stdio MCP servers distributed as npm packages
# (e.g. the brave-search server configured in config.yaml). sqlite3 is the
# CLI shell, not a runtime dependency — Python's own sqlite3 module (with
# FTS5) already works via the base image's libsqlite3; this is just so the
# long-term memory database at /app/.memory can be inspected by hand with
# `docker exec -it <container> sqlite3 /app/.memory/memory.sqlite3`.
# fonts-dejavu-core gives the export_pdf tool a Unicode-coverage TTF font —
# without one, PDF generation only supports Latin-1 and fails on anything
# else (e.g. an em-dash) that agent-written text commonly contains.
RUN apt-get update && apt-get install -y --no-install-recommends nodejs npm sqlite3 fonts-dejavu-core \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY harness/ harness/
COPY skills/ skills/
COPY config.yaml .
COPY entrypoint.sh .
RUN chmod +x entrypoint.sh

RUN mkdir -p /app/workspace /app/.transcripts /app/.memory
VOLUME ["/app/workspace", "/app/.transcripts", "/app/.memory"]

ENV PYTHONPATH=/app
ENV PYTHONUNBUFFERED=1

ENTRYPOINT ["./entrypoint.sh"]
