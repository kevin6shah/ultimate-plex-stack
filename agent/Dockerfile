FROM mcr.microsoft.com/playwright/python:v1.49.1-noble

WORKDIR /var/task

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app ./app

ENV PYTHONUNBUFFERED=1
ENTRYPOINT ["python", "-m", "awslambdaric"]
CMD ["app.main.handler"]
