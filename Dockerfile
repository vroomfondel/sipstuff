ARG python_version=3.14
ARG debian_version=slim-trixie

# ─── Stage 1: Build PJSIP with Python bindings ─────────────────────────────
FROM python:${python_version}-${debian_version} AS pjsip-builder

ARG pjsip_version=2.16

RUN apt update && \
    apt -y install build-essential python3-dev swig \
        libasound2-dev libssl-dev libopus-dev wget && \
    pip install --no-cache-dir setuptools && \
    rm -rf /var/lib/apt/lists/*

# Snapshot existing libs so we can stage only what PJSIP adds
RUN ls /usr/local/lib/*.so* 2>/dev/null | sort > /tmp/_libs_before.txt || true

COPY dist_scripts/install_pjsip.sh /tmp/install_pjsip.sh
RUN PJSIP_VERSION=${pjsip_version} PYTHON=python3 /tmp/install_pjsip.sh --system

# Stage artifacts for multi-stage COPY
RUN mkdir -p /pjsip-libs /pjsip-python && \
    ls /usr/local/lib/*.so* 2>/dev/null | sort > /tmp/_libs_after.txt && \
    comm -13 /tmp/_libs_before.txt /tmp/_libs_after.txt | xargs -I{} cp -P {} /pjsip-libs/ && \
    python3 -c "\
import pjsua2, _pjsua2, os, shutil;\
dst='/pjsip-python';\
shutil.copy2(pjsua2.__file__, dst);\
shutil.copy2(_pjsua2.__file__, dst);\
print('Staged libs:', os.listdir('/pjsip-libs'));\
print('Staged python:', os.listdir(dst))"

# ─── Stage 2: Main image ───────────────────────────────────────────────────
FROM python:${python_version}-${debian_version}

# repeat without defaults in this build-stage
ARG python_version
ARG debian_version
ARG pjsip_version

# https://docs.docker.com/develop/develop-images/dockerfile_best-practices/

RUN apt update && \
    apt -y full-upgrade && \
    apt -y install htop procps iputils-ping locales vim tini bind9-dnsutils \
        libasound2t64 libssl3t64 libopus0 libpulse0 && \
    pip install --upgrade pip && \
    rm -rf /var/lib/apt/lists/*

# Build PortAudio from source with PulseAudio support (Debian's libportaudio2 is ALSA-only)
RUN apt update && \
    apt -y install --no-install-recommends build-essential cmake git libasound2-dev libpulse-dev && \
    git clone --depth 1 https://github.com/PortAudio/portaudio.git /tmp/portaudio && \
    cd /tmp/portaudio && \
    cmake -B build -DCMAKE_INSTALL_PREFIX=/usr -DPA_USE_PULSEAUDIO=ON -DPA_USE_ALSA=ON && \
    cmake --build build -j$(nproc) && \
    cmake --install build && \
    ldconfig && \
    rm -rf /tmp/portaudio && \
    apt -y purge --auto-remove build-essential cmake git libasound2-dev libpulse-dev && \
    rm -rf /var/lib/apt/lists/*

RUN sed -i -e 's/# de_DE.UTF-8 UTF-8/de_DE.UTF-8 UTF-8/' /etc/locale.gen && \
    locale-gen && \
    update-locale LC_ALL=de_DE.UTF-8 LANG=de_DE.UTF-8 && \
    rm -f /etc/localtime && \
    ln -s /usr/share/zoneinfo/Europe/Berlin /etc/localtime


# MULTIARCH-BUILD-INFO: https://itnext.io/building-multi-cpu-architecture-docker-images-for-arm-and-x86-1-the-basics-2fa97869a99b
ARG TARGETOS
ARG TARGETARCH
RUN echo "I'm building for $TARGETOS/$TARGETARCH"

# default UID and GID are the ones used for selenium in seleniarm/standalone-chromium:107.0

ARG UID=1200
ARG GID=1201
ARG UNAME=pythonuser
RUN groupadd -g ${GID} -o ${UNAME} && \
    useradd -m -u ${UID} -g ${GID} -o -s /bin/bash ${UNAME}

# PJSIP shared libraries from builder (all .so files that PJSIP added)
COPY --from=pjsip-builder /pjsip-libs/ /usr/local/lib/
# Python bindings staged to /pjsip-python/ in builder (avoids site-packages vs dist-packages path issues)
COPY --from=pjsip-builder /pjsip-python/ /tmp/pjsip-python/
RUN PYDIR=$(python3 -c "import site; print(site.getsitepackages()[0])") && \
    cp /tmp/pjsip-python/* "$PYDIR/" && \
    rm -rf /tmp/pjsip-python && \
    ldconfig

ENV PATH="/home/${UNAME}/.local/bin:$PATH"

WORKDIR /app

COPY --chown=${UID}:${GID} requirements.txt ./
COPY --chown=${UID}:${GID} README.md pyproject.toml ./
COPY --chown=${UID}:${GID} sipstuff ./sipstuff

# Install build-essential temporarily for compiling C extensions (e.g. numpy on arm64),
# pip install as pythonuser via runuser, then purge build tools — all in one layer.
RUN apt update && \
    apt -y install --no-install-recommends build-essential linux-libc-dev && \
    runuser -u ${UNAME} -- env PATH="/home/${UNAME}/.local/bin:$PATH" \
        pip3 install --no-cache-dir --upgrade -r ./requirements.txt && \
    runuser -u ${UNAME} -- env PATH="/home/${UNAME}/.local/bin:$PATH" \
        pip install --no-cache-dir -e . && \
    apt -y purge --auto-remove build-essential linux-libc-dev && \
    rm -rf /var/lib/apt/lists/*

# Optional: CUDA runtime libs for faster-whisper GPU inference
# Build with: docker build --build-arg INSTALL_CUDA=true ...
ARG install_cuda=false
RUN if [ "$install_cuda" = "true" ]; then \
    runuser -u ${UNAME} -- env PATH="/home/${UNAME}/.local/bin:$PATH" \
        pip install --no-cache-dir nvidia-cuda-runtime-cu12 nvidia-cublas-cu12 nvidia-cudnn-cu12 nvidia-curand-cu12 nvidia-cufft-cu12 nvidia-cusolver-cu12 nvidia-cusparse-cu12 nvidia-nvjitlink-cu12 && \
    if [ "$(uname -m)" = "x86_64" ]; then \
        runuser -u ${UNAME} -- env PATH="/home/${UNAME}/.local/bin:$PATH" \
            pip install --no-cache-dir onnxruntime-gpu && \
        echo "onnxruntime-gpu installed (x86_64)"; \
    else \
        echo "SKIP onnxruntime-gpu — not available for $(uname -m), using CPU onnxruntime"; \
    fi && \
    python3 -c "\
import subprocess, pathlib;\
res = subprocess.run(['find', '/home', '-path', '*/nvidia/*/lib', '-type', 'd'], capture_output=True, text=True);\
dirs = [d for d in res.stdout.strip().splitlines() if d];\
pathlib.Path('/etc/ld.so.conf.d/nvidia-pip.conf').write_text('\n'.join(dirs) + '\n') if dirs else None;\
print('nvidia lib dirs:', dirs)" && \
    ldconfig; \
    fi

# Optional: OpenVINO STT backend for Intel GPU / CPU inference
# Build with: docker build --build-arg INSTALL_OPENVINO=true ...
ARG install_openvino=false
RUN if [ "$install_openvino" = "true" ]; then \
    apt update && \
    apt -y install --no-install-recommends build-essential linux-libc-dev && \
    runuser -u ${UNAME} -- env PATH="/home/${UNAME}/.local/bin:$PATH" \
        pip install --no-cache-dir ".[openvino]" && \
    runuser -u ${UNAME} -- env PATH="/home/${UNAME}/.local/bin:$PATH" \
        pip uninstall -y torch torchvision torchaudio nncf && \
    apt -y purge --auto-remove build-essential linux-libc-dev && \
    rm -rf /var/lib/apt/lists/*; \
    fi

USER ${UNAME}



# set environment variables
ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1

#ENV PYTHONPATH=${PYTHONPATH:+${PYTHONPATH}:}/app:/app/mqttstuff
ENV PYTHONPATH=/app

ARG gh_ref=gh_ref_is_undefined
ENV GITHUB_REF=$gh_ref
ARG gh_sha=gh_sha_is_undefined
ENV GITHUB_SHA=$gh_sha
ARG buildtime=buildtime_is_undefined
ENV BUILDTIME=$buildtime

# https://hynek.me/articles/docker-signals/

# STOPSIGNAL SIGINT
# ENTRYPOINT ["/usr/bin/tini", "--"]

# ENV TINI_SUBREAPER=yes
# ENV TINI_KILL_PROCESS_GROUP=yes
# ENV TINI_VERBOSITY=3

ENTRYPOINT ["tini", "--"]
CMD ["tail", "-f", "/dev/null"]
# CMD ["python3", "main.py"]
