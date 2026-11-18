FROM python:3.12-slim AS base
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PORT=8080
WORKDIR /srv
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY app ./app

# The checks: the image only builds if every test passes.
FROM base AS test
COPY requirements-dev.txt .
RUN pip install --no-cache-dir -r requirements-dev.txt
COPY tests ./tests
RUN python -m pytest -q -p no:cacheprovider tests && touch /tmp/tests-passed

FROM base
COPY --from=test /tmp/tests-passed /tmp/tests-passed
ENV BASE_PATH=/elevation STORE=sqlite DATABASE_PATH=/data/elevation.db
RUN useradd --uid 1000 app && mkdir -p /data && chown app /data
USER app
VOLUME /data
EXPOSE 8080
# Caddy (Docker) or the Google load balancer (Cloud Run) terminates HTTPS in front of it.
CMD ["sh", "-c", "exec uvicorn app.main:app --host 0.0.0.0 --port ${PORT} --proxy-headers --forwarded-allow-ips='*'"]
