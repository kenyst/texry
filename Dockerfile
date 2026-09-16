FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1
WORKDIR /srv

# Весь код лежит в одном архиве: Docker сам распаковывает его при ADD
ADD texry-src.tar.gz /srv/
RUN pip install -r requirements.txt

RUN useradd -m texry && mkdir -p /srv/data && chown -R texry /srv/data
USER texry

EXPOSE 8080
CMD ["sh", "-c", "exec uvicorn app.main:app --host 0.0.0.0 --port ${PORT:-8080} --proxy-headers --forwarded-allow-ips '*'"]
