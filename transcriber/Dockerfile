# CPU-only (works everywhere)
FROM python:3.11-slim

# For GPU, switch to an image with CUDA + PyTorch preinstalled,
# or pip install torch with appropriate CUDA wheels.
RUN apt-get update && apt-get install -y ffmpeg git && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY requirements.txt .
RUN pip install -r requirements.txt
COPY app.py .
CMD ["uvicorn","app:app","--host","0.0.0.0","--port","8000"]
