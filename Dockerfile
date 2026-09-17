# syntax=docker/dockerfile:1
#
# JobDoc — single Dockerfile that runs correctly on BOTH CPU-only and GPU Hugging Face Spaces.
#
# WHY A CUDA BASE IMAGE?
#   So the container already has the CUDA runtime libraries when a GPU tier is selected in
#   Space Settings. On a CPU-only Space the very same image still runs perfectly — GPU is
#   detected at RUNTIME only (utils/gpu_utils.py) and the app falls back to software ffmpeg
#   and cloud STT automatically. JobDoc uses GPU exclusively to accelerate the local MEDIA
#   pipeline (ffmpeg NVDEC/NVENC + frame extraction) and an optional local Whisper backend —
#   never for computer vision.
#
# CRITICAL HUGGING FACE RULE (followed here):
#   No GPU/CUDA command runs during `docker build` (no nvidia-smi, no torch.cuda, no
#   ffmpeg -hwaccel probe). HF build machines have NO GPU, so any such command would fail
#   or report False. ALL detection happens after the container starts.
FROM nvidia/cuda:12.1.0-base-ubuntu22.04

ENV DEBIAN_FRONTEND=noninteractive \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PORT=7860

# System dependencies:
#   * Python 3.11 (deadsnakes)
#   * ffmpeg — Ubuntu's build supports NVDEC/NVENC when the runtime GPU + drivers are present;
#     it transparently runs software decode/encode otherwise.
#   * WeasyPrint runtime libs (Pango / Cairo / GDK-Pixbuf) so PDF export works on Linux.
#   * build tooling for any wheels that need compilation.
RUN apt-get update && apt-get install -y --no-install-recommends \
        software-properties-common \
    && add-apt-repository ppa:deadsnakes/ppa \
    && apt-get update && apt-get install -y --no-install-recommends \
        python3.11 python3.11-venv python3.11-dev python3-pip \
        ffmpeg \
        libpango-1.0-0 libpangocairo-1.0-0 libcairo2 libgdk-pixbuf-2.0-0 \
        libffi-dev libjpeg-dev libpng-dev shared-mime-info fonts-dejavu-core \
        build-essential curl ca-certificates git procps psmisc \
    && ln -sf /usr/bin/python3.11 /usr/local/bin/python \
    && ln -sf /usr/bin/python3.11 /usr/local/bin/python3 \
    && rm -rf /var/lib/apt/lists/*

RUN python -m pip install --upgrade pip setuptools wheel

WORKDIR /app

# Entrypoint script — copy + mark executable explicitly so it is always present and runnable
# (independent of the later `COPY . .`). start.sh frees port 7860 before binding gunicorn.
COPY start.sh /app/start.sh
RUN chmod +x /app/start.sh

# Install Python deps first for better layer caching. The cu121 extra index pulls the CUDA
# build of torch (installs on CPU build machines too — it simply won't use a GPU there).
# --break-system-packages: Ubuntu marks the system Python as "externally managed" (PEP 668).
# The distutils-installed apt blinker (1.4) has no RECORD, so pip cannot uninstall it to satisfy
# Flask's newer requirement — so we reinstall blinker first with --ignore-installed, then install
# the rest. This avoids the "Cannot uninstall blinker 1.4" build failure on HF.
COPY requirements.txt .
RUN python -m pip install --no-cache-dir --break-system-packages --ignore-installed blinker && \
    python -m pip install --no-cache-dir --break-system-packages -r requirements.txt \
    --extra-index-url https://download.pytorch.org/whl/cu121

# Application code.
COPY . .

# HF Spaces runs the container as a non-root user (uid 1000). Create it, own writable
# runtime directories, and route caches (e.g. Whisper model downloads) to its home.
RUN useradd -m -u 1000 user \
    && mkdir -p /app/uploads /app/reports_output /app/instance \
    && chmod +x /app/start.sh \
    && chown -R user:user /app
USER user
ENV HOME=/home/user \
    HF_HOME=/home/user/.cache/huggingface \
    XDG_CACHE_HOME=/home/user/.cache

EXPOSE 7860

# Gunicorn (via start.sh): a small pool of threaded workers. Pipeline stages are long-running
# and partly network-bound (Deepgram + OpenAI), so timeouts are generous and threads keep the
# status-polling endpoint responsive while a report generates. GPU detection runs at runtime
# inside these workers — never at build. start.sh first clears any lingering gunicorn / frees
# port 7860 so an HF "Restart" can re-bind cleanly, then exec's gunicorn as PID 1 for signals.
CMD ["sh", "/app/start.sh"]
