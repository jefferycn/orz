# orz 2.0 — Python 3.12, standard library only, no build step.
FROM python:3.12-slim

WORKDIR /app
COPY app/ ./app/
COPY orz.py ./orz.py
COPY tools/ ./tools/

# Default command is the watch daemon — not the CLI.
# The container monitors /media/anime and /media/tv-series on its own;
# `create` / `folder` / `scan` / `dry-run` / `refresh` are only for
# manual testing and troubleshooting (docker exec orz python3 orz.py ...).
ENV ORZ_WATCH_DIRS=/media/anime,/media/tv-series \
    ORZ_SCAN_INTERVAL=30 \
    ORZ_STABLE_SECONDS=5

CMD ["python3", "orz.py", "watch"]
