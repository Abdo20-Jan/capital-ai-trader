# Backend + painel (Railway, Render, Fly.io ou qualquer Docker)
FROM python:3.12-slim
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 DATA_DIR=/data
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY . .
RUN mkdir -p /data
EXPOSE 8000
# 'serve' escuta em 0.0.0.0:$PORT e EXIGE a variável APP_PASSWORD
CMD ["python", "run.py", "serve"]
