use std::{
    collections::HashMap,
    env,
    path::{Path, PathBuf},
    sync::Arc,
    time::{SystemTime, UNIX_EPOCH},
};

use teloxide::{
    dispatching::UpdateFilterExt,
    prelude::*,
    types::{CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, InputFile},
};
use tokio::{fs, process::Command, sync::Mutex};

type Jobs = Arc<Mutex<HashMap<String, String>>>;

#[derive(Debug, serde::Deserialize)]
struct VideoInfo {
    title: Option<String>,
    duration: Option<f64>,
    #[serde(default)]
    formats: Vec<FormatInfo>,
}

#[derive(Debug, serde::Deserialize)]
struct FormatInfo {
    height: Option<u32>,
    vcodec: Option<String>,
}

#[tokio::main]
async fn main() {
    pretty_env_logger::init();

    let token = env::var("BOT_TOKEN")
        .expect("BOT_TOKEN is missing");

    let download_dir = PathBuf::from("downloads");
    fs::create_dir_all(&download_dir)
        .await
        .expect("Cannot create downloads directory");

    let bot = Bot::new(token);
    let jobs: Jobs = Arc::new(Mutex::new(HashMap::new()));

    let messages = Update::filter_message()
        .filter_map(|msg: Message| async move {
            msg.text().map(|text| (msg, text.to_string()))
        })
        .endpoint(handle_message);

    let callbacks = Update::filter_callback_query()
        .endpoint(handle_callback);

    Dispatcher::builder(
        bot,
        dptree::entry()
            .branch(messages)
            .branch(callbacks),
    )
    .dependencies(dptree::deps![jobs, download_dir])
    .enable_ctrlc_handler()
    .build()
    .dispatch()
    .await;
}

async fn handle_message(
    bot: Bot,
    msg: Message,
    text: String,
    jobs: Jobs,
    download_dir: PathBuf,
) -> ResponseResult<()> {
    let text = text.trim();

    if text == "/start" {
        bot.send_message(
            msg.chat.id,
            "سلام 👋\n\nلینک YouTube را بفرست تا کیفیت‌های موجود را نشان بدهم.",
        )
        .await?;
        return Ok(());
    }

    if text == "/help" {
        bot.send_message(
            msg.chat.id,
            "فعلاً فقط YouTube پشتیبانی می‌شود.\n\nیک لینک YouTube یا Shorts ارسال کن.",
        )
        .await?;
        return Ok(());
    }

    let Some(url) = extract_youtube_url(text) else {
        bot.send_message(
            msg.chat.id,
            "❌ لینک YouTube معتبر پیدا نشد.",
        )
        .await?;
        return Ok(());
    };

    let status = bot
        .send_message(msg.chat.id, "🔎 در حال دریافت اطلاعات YouTube...")
        .await?;

    match fetch_video_info(&url).await {
        Ok(info) => {
            let title = info.title.unwrap_or_else(|| "YouTube Video".into());
            let duration = format_duration(info.duration.unwrap_or(0.0));
            let qualities = build_qualities(&info);

            if qualities.is_empty() {
                bot.edit_message_text(
                    msg.chat.id,
                    status.id,
                    "❌ کیفیت قابل دانلودی پیدا نشد.",
                )
                .await?;
                return Ok(());
            }

            let job_id = new_job_id();
            jobs.lock().await.insert(job_id.clone(), url);

            let rows = qualities
                .into_iter()
                .map(|(label, format)| {
                    vec![InlineKeyboardButton::callback(
                        label,
                        format!("ydl:{job_id}:{format}"),
                    )]
                })
                .collect::<Vec<_>>();

            bot.edit_message_text(
                msg.chat.id,
                status.id,
                format!(
                    "🎬 {}\n⏱ {}\n\nکیفیت را انتخاب کن:",
                    title, duration
                ),
            )
            .reply_markup(InlineKeyboardMarkup::new(rows))
            .await?;
        }

        Err(err) => {
            log::error!("yt-dlp metadata error: {err}");
            bot.edit_message_text(
                msg.chat.id,
                status.id,
                "❌ نتوانستم اطلاعات ویدیو را بگیرم.\n\nممکن است yt-dlp یا محدودیت YouTube علت مشکل باشد.",
            )
            .await?;
        }
    }

    let _ = download_dir;
    Ok(())
}

async fn handle_callback(
    bot: Bot,
    query: CallbackQuery,
    jobs: Jobs,
    download_dir: PathBuf,
) -> ResponseResult<()> {
    let Some(data) = query.data.clone() else {
        return Ok(());
    };

    bot.answer_callback_query(query.id).await?;

    let parts: Vec<&str> = data.splitn(3, ':').collect();

    if parts.len() != 3 || parts[0] != "ydl" {
        return Ok(());
    }

    let job_id = parts[1];
    let format = parts[2];

    let Some(url) = jobs.lock().await.get(job_id).cloned() else {
        if let Some(message) = query.message {
            bot.send_message(
                message.chat().id,
                "❌ این درخواست منقضی شده است. دوباره لینک را ارسال کن.",
            )
            .await?;
        }
        return Ok(());
    };

    let Some(message) = query.message else {
        return Ok(());
    };

    let chat_id = message.chat().id;

    bot.edit_message_text(
        chat_id,
        message.id(),
        "⏬ دانلود شروع شد...\n\nلطفاً صبر کن.",
    )
    .await?;

    match download_video(&url, format, &download_dir).await {
        Ok(file) => {
            bot.edit_message_text(
                chat_id,
                message.id(),
                "📤 دانلود تمام شد؛ در حال ارسال...",
            )
            .await?;

            bot.send_document(chat_id, InputFile::file(file.clone()))
                .await?;

            if let Err(err) = fs::remove_file(&file).await {
                log::warn!("Could not remove temporary file: {err}");
            }

            bot.send_message(chat_id, "✅ انجام شد.")
                .await?;
        }

        Err(err) => {
            log::error!("Download error: {err}");

            bot.edit_message_text(
                chat_id,
                message.id(),
                format!("❌ دانلود ناموفق بود:\n\n{}", shorten_error(&err)),
            )
            .await?;
        }
    }

    jobs.lock().await.remove(job_id);
    Ok(())
}

fn extract_youtube_url(text: &str) -> Option<String> {
    for token in text.split_whitespace() {
        let url = token.trim_matches(|c: char| {
            matches!(c, '<' | '>' | '(' | ')' | '[' | ']' | '"' | '\'' | ',')
        });

        if url.starts_with("https://www.youtube.com/")
            || url.starts_with("https://youtube.com/")
            || url.starts_with("https://m.youtube.com/")
            || url.starts_with("https://youtu.be/")
            || url.starts_with("http://www.youtube.com/")
            || url.starts_with("http://youtube.com/")
            || url.starts_with("http://m.youtube.com/")
            || url.starts_with("http://youtu.be/")
        {
            return Some(url.to_string());
        }
    }

    None
}

async fn fetch_video_info(url: &str) -> Result<VideoInfo, String> {
    let output = Command::new("yt-dlp")
        .args([
            "--dump-single-json",
            "--no-download",
            "--no-playlist",
            "--no-warnings",
            url,
        ])
        .output()
        .await
        .map_err(|e| format!("yt-dlp اجرا نشد: {e}"))?;

    if !output.status.success() {
        return Err(String::from_utf8_lossy(&output.stderr).to_string());
    }

    serde_json::from_slice(&output.stdout)
        .map_err(|e| format!("JSON نامعتبر از yt-dlp: {e}"))
}

fn build_qualities(info: &VideoInfo) -> Vec<(String, String)> {
    let mut heights = Vec::new();

    for f in &info.formats {
        let Some(height) = f.height else { continue };
        if f.vcodec.as_deref() == Some("none") {
            continue;
        }

        if !heights.contains(&height) {
            heights.push(height);
        }
    }

    heights.sort_unstable();
    heights.dedup();

    heights
        .into_iter()
        .rev()
        .take(8)
        .map(|height| {
            (
                format!("🎬 {}p", height),
                format!("bestvideo[height<={height}]+bestaudio/best[height<={height}]"),
            )
        })
        .collect()
}

async fn download_video(
    url: &str,
    format: &str,
    download_dir: &Path,
) -> Result<PathBuf, String> {
    let timestamp = SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .map_err(|e| e.to_string())?
        .as_millis();

    let template = download_dir
        .join(format!("youtube_{timestamp}.%(ext)s"))
        .to_string_lossy()
        .to_string();

    let output = Command::new("yt-dlp")
        .args([
            "--no-playlist",
            "--newline",
            "--progress",
            "--merge-output-format",
            "mp4",
            "-f",
            format,
            "-o",
            &template,
            url,
        ])
        .output()
        .await
        .map_err(|e| format!("yt-dlp اجرا نشد: {e}"))?;

    if !output.status.success() {
        return Err(String::from_utf8_lossy(&output.stderr).to_string());
    }

    let prefix = format!("youtube_{timestamp}");

    let mut dir = fs::read_dir(download_dir)
        .await
        .map_err(|e| e.to_string())?;

    while let Some(entry) = dir.next_entry().await.map_err(|e| e.to_string())? {
        let path = entry.path();

        if path
            .file_name()
            .and_then(|x| x.to_str())
            .map(|name| name.starts_with(&prefix))
            .unwrap_or(false)
        {
            return Ok(path);
        }
    }

    Err("فایل خروجی پیدا نشد.".into())
}

fn new_job_id() -> String {
    let millis = SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .unwrap()
        .as_millis();

    format!("{:x}", millis)
}

fn format_duration(seconds: f64) -> String {
    let total = seconds.max(0.0) as u64;
    let h = total / 3600;
    let m = (total % 3600) / 60;
    let s = total % 60;

    if h > 0 {
        format!("{h:02}:{m:02}:{s:02}")
    } else {
        format!("{m:02}:{s:02}")
    }
}

fn shorten_error(error: &str) -> String {
    const MAX: usize = 1200;

    if error.len() <= MAX {
        error.to_string()
    } else {
        format!("{}...", &error[..MAX])
    }
}
