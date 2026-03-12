FROM python:3.11-slim

WORKDIR /app

# Install system deps for Pillow / onnxruntime
RUN apt-get update && apt-get install -y --no-install-recommends \
    libgl1-mesa-glx libglib2.0-0 libsm6 libxrender1 libxext6 \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app.py .

# Pre-download the default model so container start is faster
RUN python -c "from rembg import new_session; new_session('isnet-general-use')" || true

ENV PORT=8080
EXPOSE 8080

CMD ["python", "-u", "app.py"]
