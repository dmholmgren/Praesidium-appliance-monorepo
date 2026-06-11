FROM python:3.12-slim

WORKDIR /app

# System dependencies
# python-ldap needs libldap2-dev + libsasl2-dev
# cryptography needs libffi-dev + libssl-dev
# OCR pipeline: tesseract-ocr + poppler-utils (pdftoppm)
# Document conversion: libreoffice headless (DOCX → PDF preview)
# Legacy doc formats: antiword (old .doc)
# PST extraction: pst-utils (readpst) for Outlook PST ingestion
RUN apt-get update && apt-get install -y --no-install-recommends \
    curl gcc g++ make pkg-config \
    libldap2-dev libsasl2-dev \
    libffi-dev libssl-dev \
    tesseract-ocr \
    poppler-utils \
    libreoffice \
    antiword \
    pandoc \
    pst-utils \
    default-jre-headless \
    docker-cli \
    && rm -rf /var/lib/apt/lists/*

# Timeslips Database Connector
RUN apt-get update && apt-get install -y --no-install-recommends libfbclient2 && rm -rf /var/lib/apt/lists/*

# Python dependencies
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Application code
COPY . .

ENV PYTHONPATH=/app
ENV PYTHONUNBUFFERED=1

EXPOSE 8000

CMD ["uvicorn", "app:app", "--host", "0.0.0.0", "--port", "8000"]
