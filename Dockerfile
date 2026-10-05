FROM python:3.12-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# SQLite 数据库目录（Compose 中挂载卷以持久化）
ENV DB_PATH=/data/rundown.db
RUN mkdir -p /data

EXPOSE 8000

CMD ["python", "app.py"]
