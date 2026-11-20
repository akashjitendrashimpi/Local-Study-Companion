FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PUBLIC_MODE=true \
    BROWSER_HEADLESS=true \
    HOST=0.0.0.0

WORKDIR /app

COPY requirements.txt .
RUN apt-get update \
    && apt-get install -y --no-install-recommends libatomic1 \
    && rm -rf /var/lib/apt/lists/* \
    && pip install --no-cache-dir -r requirements.txt \
    && python -m playwright install --with-deps chromium

COPY web_app.py agent.py ./

EXPOSE 10000

CMD ["python", "web_app.py"]
