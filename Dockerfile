FROM python:3.13-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

COPY . .

RUN pip install --upgrade pip \
    && pip install ".[server]"

EXPOSE 18790

CMD ["python", "-m", "treg"]
