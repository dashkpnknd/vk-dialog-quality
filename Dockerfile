FROM python:3.11-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY main.py .
COPY quality ./quality
ENV PYTHONUNBUFFERED=1 DATABASE_PATH=/data/quality.sqlite3
CMD ["python", "main.py", "bot"]
