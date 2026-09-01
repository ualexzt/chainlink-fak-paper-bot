FROM python:3.13-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PYTHONPATH=/app

RUN groupadd --gid 1000 paper \
    && useradd --uid 1000 --gid paper --create-home --shell /usr/sbin/nologin paper \
    && install -d -o paper -g paper /app /data

WORKDIR /app
COPY requirements.txt /app/requirements.txt
RUN python -m pip install --no-cache-dir --requirement /app/requirements.txt

COPY --chown=paper:paper paper_bot /app/paper_bot
COPY --chown=paper:paper scripts /app/scripts
COPY --chown=paper:paper tests /app/tests

USER paper
CMD ["python", "-m", "paper_bot.cli", "engine"]
