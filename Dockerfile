FROM python:3.11-slim

WORKDIR /app

# openssh-client provides the ssh binary used by paramiko for key handling
RUN apt-get update && apt-get install -y --no-install-recommends \
    openssh-client \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY *.py .
COPY templates/ templates/

# /data/reports : persistent report storage (mount a volume here)
# /data/ssh     : SSH keys (mount ~/.ssh here read-only)
RUN mkdir -p /data/reports /data/ssh

EXPOSE 5000

ENV REPORTS_DIR=/data/reports

CMD ["python3", "server.py"]
