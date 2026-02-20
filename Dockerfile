FROM python:3.11-slim

# Set environment variables
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

# Create non-root user
RUN useradd -m -u 1000 -s /bin/bash oogway && \
    mkdir -p /app/data && \
    chown -R oogway:oogway /app

WORKDIR /app

# Install dependencies as root
COPY --chown=oogway:oogway requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy application code
COPY --chown=oogway:oogway . .

# Switch to non-root user
USER oogway

# Expose port for health checks (if needed)
EXPOSE 8000

# Run the application
CMD ["python", "-m", "oogway.bot"]
