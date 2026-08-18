FROM rust:1.88-bookworm AS builder

WORKDIR /app

COPY Cargo.toml Cargo.lock* ./
COPY src ./src

RUN cargo build --release

FROM debian:bookworm-slim

RUN apt-get update \
    && apt-get install -y --no-install-recommends \
       ffmpeg \
       python3 \
       python3-pip \
       ca-certificates \
    && pip3 install --break-system-packages --no-cache-dir -U yt-dlp \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY --from=builder /app/target/release/youtube-downloader-bot /app/youtube-downloader-bot

RUN mkdir -p /app/downloads

ENV RUST_LOG=info

CMD ["/app/youtube-downloader-bot"]
