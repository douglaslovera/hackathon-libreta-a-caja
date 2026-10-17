FROM python:3.12-slim
WORKDIR /app
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY core ./core
COPY canales ./canales
COPY db ./db
COPY main.py .
EXPOSE 8000
CMD ["python", "main.py"]
