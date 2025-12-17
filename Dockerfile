FROM python:3.10-alpine AS builder

WORKDIR /install

COPY requirements.txt /requirements.txt

RUN pip install --prefix=/install --no-cache-dir -r /requirements.txt

FROM python:3.10-alpine

COPY --from=builder /install /usr/local

# Set Python path
ENV PYTHONPATH="/usr/local/lib/python3.10/site-packages"

WORKDIR /srv

COPY . .

# Create a volume mount point for config
VOLUME ["/config"]

# Default command - expects config.yaml to be mounted or provided
CMD ["python", "-m", "dali2mqtt.dali2mqtt", "--config=/config/config.yaml"]

