FROM python:3.12-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY __init__.py profiles.py simulator.py fake_metrics.py ./exporter/
EXPOSE 8000 9101 9102 9103 9104
CMD ["python", "-m", "exporter.fake_metrics"]
