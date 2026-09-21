# AIVoiceBench — minimal Docker image
# FFmpeg included for recording imports; optional models are external

FROM python:3.12-slim AS base

LABEL maintainer="AIVoiceBench"
LABEL description="AI Voice Terminal Evaluation — recording import, evidence ledger, timestamped transcript, deterministic metrics"
LABEL version="0.6.0-rc.4"

# The exact source commit this image was built from. Release builds pass it in
# so a published image is traceable to its release candidate commit.
ARG GIT_REVISION=unknown
LABEL org.opencontainers.image.revision=$GIT_REVISION

# Install only system packages needed for Python audio including FFmpeg
RUN apt-get update && apt-get install -y --no-install-recommends \
    gcc g++ ffmpeg \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Install Python dependencies
COPY requirements-dev.txt requirements-api.txt ./
RUN pip install --no-cache-dir -r requirements-api.txt

# Copy application code
COPY aivoicebench/ ./aivoicebench/
COPY schemas/ ./schemas/
COPY examples/ ./examples/
COPY docs/ ./docs/
COPY config/ ./config/

# Create default directories
RUN mkdir -p /data/recordings /data/output /data/cache

# Environment defaults
# LLM provider: "none" (default: semantic evaluation unavailable), "volcengine", or "openai"
# For volcengine: set ARK_API_KEY and AIVOICEBENCH_LLM_MODEL=ep-xxx
# For openai: set OPENAI_API_KEY and AIVOICEBENCH_LLM_MODEL=gpt-4o
ENV AIVOICEBENCH_OUTPUT=/data/output \
    AIVOICEBENCH_CACHE=/data/cache \
    AIVOICEBENCH_PROVIDERS_CONFIG=/app/config/providers.yaml \
    AIVOICEBENCH_STORAGE_CONFIG=/app/config/storage.yaml \
    AIVOICEBENCH_LLM_PROVIDER=none \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

EXPOSE 8000

# Run the Web API
CMD ["uvicorn", "aivoicebench.api:app", "--host", "0.0.0.0", "--port", "8000"]
