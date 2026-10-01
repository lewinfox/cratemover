# syntax=docker/dockerfile:1
# A small image: Alpine Python, only the runtime wheels, and an audio-only ffmpeg.
ARG PYTHON_IMAGE=python:3.13-alpine3.22
ARG UV_IMAGE=ghcr.io/astral-sh/uv:0.8

FROM ${UV_IMAGE} AS uv

# --- ffmpeg: decoders for DJ formats, the filters waveforms need, LAME for MP3 ---------
FROM ${PYTHON_IMAGE} AS ffmpeg
ARG FFMPEG_VERSION=7.1.1
RUN apk add --no-cache build-base pkgconf curl xz lame-dev
WORKDIR /src
RUN curl -fsSL "https://ffmpeg.org/releases/ffmpeg-${FFMPEG_VERSION}.tar.xz" | tar -xJ --strip-components=1
RUN ./configure --prefix=/opt/ffmpeg \
        --disable-everything --disable-autodetect --disable-doc --disable-debug \
        --disable-network --disable-ffplay --disable-ffprobe --disable-x86asm \
        --enable-small --enable-libmp3lame \
        --enable-protocol=file,pipe \
        --enable-demuxer=mp3,flac,ogg,mov,wav,w64,aiff,asf,aac,matroska \
        --enable-muxer=mp3,pcm_f32le \
        --enable-decoder='mp3*,flac,aac,aac_latm,alac,vorbis,opus,pcm_*,wmav1,wmav2,wmapro' \
        --enable-encoder=libmp3lame,pcm_f32le \
        --enable-parser=mpegaudio,flac,aac,vorbis,opus \
        --enable-filter=aresample,aformat,asplit,amerge,lowpass,highpass,anull,abuffer,abuffersink,format \
    && make -j"$(nproc)" && make install && strip /opt/ffmpeg/bin/ffmpeg

# --- Python dependencies ----------------------------------------------------------------
FROM ${PYTHON_IMAGE} AS build
COPY --from=uv /uv /usr/local/bin/uv
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy UV_PYTHON_DOWNLOADS=0
WORKDIR /app
RUN --mount=type=cache,target=/root/.cache/uv \
    --mount=type=bind,source=uv.lock,target=uv.lock \
    --mount=type=bind,source=pyproject.toml,target=pyproject.toml \
    uv sync --locked --no-install-project --no-dev --extra web --extra rekordbox
COPY pyproject.toml uv.lock README.md LICENSE ./
COPY src ./src
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --locked --no-dev --extra web --extra rekordbox --no-editable \
    && find .venv -name "*.pyi" -delete \
    && rm -rf .venv/lib/python*/site-packages/pip* .venv/lib/python*/site-packages/*/tests

# --- runtime ---------------------------------------------------------------------------
FROM ${PYTHON_IMAGE}
RUN apk add --no-cache lame-libs \
    && rm -rf /usr/local/lib/python3*/ensurepip /usr/local/lib/python3*/idlelib \
       /usr/local/lib/python3*/tkinter /usr/local/lib/python3*/turtledemo /usr/local/lib/python3*/test
COPY --from=ffmpeg /opt/ffmpeg/bin/ffmpeg /usr/local/bin/ffmpeg
COPY --from=build /app/.venv /app/.venv
ENV PATH="/app/.venv/bin:$PATH" \
    PYTHONUNBUFFERED=1 \
    EXPORT_DIR=/export \
    UPLOAD_DIR=/tmp/uploads
EXPOSE 8000
ENTRYPOINT ["cratemover"]
CMD ["serve", "--host", "0.0.0.0", "--port", "8000"]
