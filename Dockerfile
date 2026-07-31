FROM python:3.12-slim-bookworm

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

RUN apt-get update \
    && apt-get install --no-install-recommends -y ffmpeg curl tzdata \
    && rm -rf /var/lib/apt/lists/*

RUN groupadd --gid 10001 nostalgiabox \
    && useradd --uid 10001 --gid nostalgiabox --create-home nostalgiabox

WORKDIR /app
COPY pyproject.toml README.md LICENSE ./
COPY nostalgiabox ./nostalgiabox
RUN pip install --no-cache-dir '.[server]'

USER nostalgiabox
EXPOSE 8080
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD curl --fail --silent http://127.0.0.1:8080/api/v1/health || exit 1

CMD ["nostalgiabox-server"]
