FROM python:3.12-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt psycopg[binary]==3.2.9
COPY sysdesign ./sysdesign
ARG GIT_SHA=unknown
ENV GIT_SHA=$GIT_SHA
RUN useradd -r -u 10001 app
USER app
EXPOSE 8000
CMD ["uvicorn", "sysdesign.asgi:app", "--host", "0.0.0.0", "--port", "8000"]
