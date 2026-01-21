FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app

RUN apt-get update && apt-get install -y --no-install-recommends \
    ca-certificates \
 && rm -rf /var/lib/apt/lists/*

COPY requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir -r /app/requirements.txt

COPY . /app

# Render injects $PORT at runtime. Bind Streamlit to it.
# Keep the app under /sms.
CMD ["sh", "-lc", "streamlit run apps/fullcalendar_app.py --server.address 0.0.0.0 --server.port ${PORT:-8501} --server.baseUrlPath /sms --server.headless true --browser.gatherUsageStats false"]
