FROM python:3.14-slim AS base

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

RUN apt-get update \
    && apt-get install --no-install-recommends -y aria2 ffmpeg tini \
    && rm -rf /var/lib/apt/lists/*


WORKDIR /app
COPY requirements.txt .
RUN python -m pip install --no-cache-dir -r requirements.txt

FROM base AS test
COPY requirements-dev.txt .
RUN python -m pip install --no-cache-dir -r requirements-dev.txt
COPY bot.py cove_attribution.py conftest.py ./
COPY tests/ tests/
CMD ["pytest", "-q"]

FROM base AS runtime
RUN groupadd --gid 1000 cove \
    && useradd --uid 1000 --gid 1000 --home-dir /data --no-create-home --shell /usr/sbin/nologin cove \
    && mkdir /data \
    && chown cove:cove /data
COPY bot.py cove_attribution.py ./
ENV HOME=/data
USER 1000:1000
ENTRYPOINT ["tini", "--"]
CMD ["python", "bot.py"]
