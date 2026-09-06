FROM python:3.12-slim

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY fake_metrics.py .

EXPOSE 8000
CMD ["python", "fake_metrics.py"]
