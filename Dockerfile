FROM python:3.11-slim

WORKDIR /app

# Install system deps for Pillow / onnxruntime
RUN apt-get update && apt-get install -y --no-install-recommends \
    libgl1-mesa-glx libglib2.0-0 libsm6 libxrender1 libxext6 \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY main.py processor.py gdrive.py ./

# ONNX thread limits (also settable via Railway env vars)
ENV OMP_NUM_THREADS=2
ENV ORT_THREADS=2
ENV PORT=8080

EXPOSE 8080

CMD ["python", "-u", "main.py"]
