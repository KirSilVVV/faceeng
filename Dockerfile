FROM python:3.12-slim

WORKDIR /app

# Install dependencies
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Force rebuild on new commits (cache bust)
ARG CACHEBUST=1

# Copy application code
COPY . .

# Run the bot
CMD ["python", "main.py"]
