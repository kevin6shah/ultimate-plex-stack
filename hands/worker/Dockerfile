FROM mcr.microsoft.com/playwright/python:v1.49.1-jammy

WORKDIR /opt/friday

ENV PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

COPY hands/worker/requirements.txt /tmp/requirements.txt
RUN pip install -r /tmp/requirements.txt \
    && rm -rf /root/.cache /tmp/*

COPY agent/app ./app
COPY hands/worker/runner.py ./runner.py

CMD ["python", "/opt/friday/runner.py"]
