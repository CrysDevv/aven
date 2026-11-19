# syntax=docker/dockerfile:1

# ---------- Etapa 1: build de dependencias ----------
FROM python:3.12-slim AS builder

WORKDIR /build
COPY requirements.txt .
RUN pip install --no-cache-dir --user -r requirements.txt


# ---------- Etapa 2: imagen final ----------
FROM python:3.12-slim

# Usuario sin privilegios — mismo estándar de hardening que n8n Task Runners
# (ver Anexo de Seguridad). UID/GID explícitos, no dependemos del "nobody"
# del sistema porque puede variar entre distros base.
RUN groupadd -g 10001 aven && \
    useradd -u 10001 -g aven -M -s /usr/sbin/nologin aven

WORKDIR /app

COPY --from=builder /root/.local /home/aven/.local
COPY ./app ./app
COPY ./static ./static

RUN chown -R aven:aven /app /home/aven/.local

ENV PATH=/home/aven/.local/bin:$PATH \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

USER aven

EXPOSE 8000

HEALTHCHECK --interval=15s --timeout=5s --start-period=10s --retries=3 \
    CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://localhost:8000/api/health/ready',timeout=3).status==200 else 1)"

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
