FROM python:3.12-slim
WORKDIR /app
RUN pip install --no-cache-dir starlette==0.41.3 uvicorn==0.34.0 sse-starlette==2.1.3 python-multipart==0.0.20 requests==2.32.3
COPY app.py .
EXPOSE 8100
CMD ["uvicorn", "app:app", "--host", "0.0.0.0", "--port", "8100"]
