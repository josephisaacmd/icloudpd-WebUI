FROM python:3.12-slim

WORKDIR /srv/space-manager

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app ./app

ENV SPACE_MANAGER_CONFIG=/data/accounts.toml \
    SPACE_MANAGER_DB=/data/space-manager.db \
    SPACE_MANAGER_DRY_RUN=true

VOLUME /data
EXPOSE 8090

CMD ["uvicorn", "--factory", "app.main:create_app", "--host", "0.0.0.0", "--port", "8090"]
