# YouTube Downloader Bot

A minimal Rust Telegram YouTube downloader inspired by the modular architecture of Rostam.

## Features

- YouTube / YouTube Shorts URL detection
- Metadata lookup with yt-dlp
- Quality selection
- Video + audio merge through FFmpeg
- Upload to Telegram
- Temporary file cleanup
- No database or Redis in this first version

## Railway

1. Push this repository to GitHub.
2. Create a Railway service from the GitHub repository.
3. Railway detects the Dockerfile and builds the image.
4. Add an environment variable:

`BOT_TOKEN=YOUR_TELEGRAM_BOT_TOKEN`

5. Deploy.

## Important

This is a first test version. It intentionally does not include Rostam's Cookie Pool, Redis, database, advanced progress reporting, or other media providers.
