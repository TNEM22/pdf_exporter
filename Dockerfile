FROM python:3.10-slim

# Install Chrome + required libs
RUN apt-get update && apt-get install -y \
    wget gnupg unzip curl \
    chromium chromium-driver \
    --no-install-recommends && \
    apt-get clean && rm -rf /var/lib/apt/lists/*

# Environment so selenium uses chromium + chromedriver
ENV CHROME_BIN=/usr/bin/chromium
ENV CHROMEDRIVER=/usr/bin/chromedriver

# Copy app files
WORKDIR /app
COPY . /app

# Install python packages
RUN pip install --no-cache-dir -r requirements.txt

# Expose port for Flask
EXPOSE 10000

# CMD ["python", "main.py"]

# -------------- IMPORTANT --------------
# Use GUNICORN in production instead of flask dev server
# CMD ["gunicorn", "--bind", "0.0.0.0:10000", "main:app"]
# Gunicorn: 1 worker, no threads → prevent multiple Chromes
CMD ["gunicorn", "--bind", "0.0.0.0:10000", "-w", "1", "--threads", "1", "--timeout", "0", "main:app"]
# ---------------------------------------