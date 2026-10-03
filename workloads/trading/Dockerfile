FROM python:3.12-slim
RUN pip install --no-cache-dir prometheus_client==0.21.1 && useradd -r -u 10001 app
WORKDIR /app
COPY app.py .
USER 10001
EXPOSE 8080
CMD ["python", "app.py"]
