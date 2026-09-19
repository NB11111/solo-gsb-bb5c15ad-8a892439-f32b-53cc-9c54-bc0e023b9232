FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

WORKDIR /srv/app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app ./app

# SQLite 数据目录（可挂卷持久化）
ENV APP_DATABASE_URL=sqlite:////data/cpc.db
RUN mkdir -p /data && useradd -r -u 10001 cpc && chown -R cpc /data
USER cpc

EXPOSE 8000
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
