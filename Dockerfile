FROM python:3.12-slim
LABEL org.opencontainers.image.title="Manifest" \
      org.opencontainers.image.description="Shows which Docker containers across your Portainer environments have a newer image" \
      org.opencontainers.image.version="1.0.0"
WORKDIR /srv
ENV PYTHONUNBUFFERED=1
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY app ./app
RUN useradd -r -u 10001 monitor && mkdir /data && chown monitor /data
USER monitor
EXPOSE 8080
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8080"]
