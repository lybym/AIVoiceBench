# AIVoiceBench — minimal Docker image
# Python deps only; FFmpeg and models are external (mount or configure)

FROM python:3.12-slim AS base

LABEL maintainer="AIVoiceBench"
LABEL description="AI Voice Terminal Evaluation — Recording Import, Analysis, LLM Judge, Findings, Report"

# Install only system packages needed for Python audio (no FFmpeg)
RUN apt-get update && apt-get install -y --no-install-recommends \
    gcc g++ \
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
# LLM provider: "mock" (default), "volcengine", or "openai"
# For volcengine: set ARK_API_KEY and AIVOICEBENCH_LLM_MODEL=ep-xxx
# For openai: set OPENAI_API_KEY and AIVOICEBENCH_LLM_MODEL=gpt-4o
ENV AIVOICEBENCH_OUTPUT=/data/output \
    AIVOICEBENCH_CACHE=/data/cache \
    AIVOICEBENCH_LLM_PROVIDER=mock \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

EXPOSE 8000

# Run the Web API
CMD ["uvicorn", "aivoicebench.api:app", "--host", "0.0.0.0", "--port", "8000"]
