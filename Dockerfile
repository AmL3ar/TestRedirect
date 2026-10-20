FROM python:3.12-slim-bookworm AS builder

ENV PIP_DISABLE_PIP_VERSION_CHECK=1
RUN python -m venv /opt/venv
COPY requirements.lock /tmp/requirements.lock
RUN /opt/venv/bin/pip install --no-cache-dir -r /tmp/requirements.lock

FROM python:3.12-slim-bookworm AS runtime

ENV PATH="/opt/venv/bin:$PATH" \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

RUN groupadd --gid 10001 app && useradd --uid 10001 --gid app --create-home app
WORKDIR /app
RUN chown app:app /app
COPY --from=builder /opt/venv /opt/venv
COPY --chown=app:app app ./app
COPY --chown=app:app migrations ./migrations
COPY --chown=app:app examples ./examples
COPY --chown=app:app alembic.ini pyproject.toml ./
USER app
EXPOSE 8000
CMD ["uvicorn", "app.api:create_app", "--factory", "--host", "0.0.0.0", "--port", "8000"]

FROM runtime AS test

USER root
COPY requirements-dev.lock /tmp/requirements-dev.lock
RUN pip install --no-cache-dir -r /tmp/requirements-dev.lock
COPY --chown=app:app tests ./tests
USER app
CMD ["pytest", "-q"]
