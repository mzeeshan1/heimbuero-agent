import os
import json
import re
import time
import tempfile
import requests
import subprocess
import textwrap
from datetime import datetime
from io import BytesIO

# ── Environment variables ──────────────────────────────────────────────────────
ANTHROPIC_API_KEY     = os.environ.get("ANTHROPIC_API_KEY")
TELEGRAM_TOKEN        = os.environ.get("TELEGRAM_TOKEN")
TELEGRAM_CHAT_ID      = os.environ.get("TELEGRAM_CHAT_ID")
ELEVENLABS_API_KEY    = os.environ.get("ELEVENLABS_API_KEY")
ELEVENLABS_VOICE_ID   = os.environ.get("ELEVENLABS_VOICE_ID", "GoXyzBapJk3AoCJoMQl9")
PEXELS_API_KEY        = os.environ.get("PEXELS_API_KEY")
YOUTUBE_CLIENT_ID     = os.environ.get("YOUTUBE_CLIENT_ID")
YOUTUBE_CLIENT_SECRET = os.environ.get("YOUTUBE_CLIENT_SECRET")
YOUTUBE_TOKEN_JSON    = os.environ.get("YOUTUBE_TOKEN_JSON")
WP_URL                = os.environ.get("WP_URL")

MAX_ITERATIONS = 18
TEMP_DIR       = tempfile.mkdtemp()


# ── Agent state ────────────────────────────────────────────────────────────────
class VideoState:
    def __init__(self):
        self.article_title   = ""
        self.article_url     = ""
        self.article_content = ""
        self.affiliate_links = []
        self.script          = ""
        self.audio_path      = ""
        self.audio_duration  = 0.0
        self.video_clips     = []
        self.final_video     = ""
        self.thumbnail_path  = ""

STATE = VideoState()


# ── Tool definitions ───────────────────────────────────────────────────────────
TOOLS = [
    {
        "name": "get_latest_articles",
        "description": "Fetches the 10 most recently published articles from WordPress.",
        "input_schema": {"type": "object", "properties": {}, "required": []}
    },
    {
        "name": "fetch_article_content",
        "description": "Fetches the full content and all external links of a specific WordPress article.",
        "input_schema": {
            "type": "object",
            "properties": {
                "article_url":   {"type": "string"},
                "article_title": {"type": "string"}
            },
            "required": ["article_url", "article_title"]
        }
    },
    {
        "name": "generate_video_script",
        "description": "Generates a 60-90 second German video script from the stored article. Stores in state.",
        "input_schema": {"type": "object", "properties": {}, "required": []}
    },
    {
        "name": "send_approval_request",
        "description": "Sends script to owner on Telegram for approval. Waits for YES or NO.",
        "input_schema": {"type": "object", "properties": {}, "required": []}
    },
    {
        "name": "generate_voiceover",
        "description": "Converts stored script to German audio using ElevenLabs. Saves audio file. Returns duration in seconds.",
        "input_schema": {"type": "object", "properties": {}, "required": []}
    },
    {
        "name": "fetch_video_clips",
        "description": "Fetches and downloads relevant stock video clips from Pexels. Must fully complete before assemble_video is called.",
        "input_schema": {
            "type": "object",
            "properties": {
                "search_query": {"type": "string", "description": "English search term e.g. 'home office desk'"}
            },
            "required": ["search_query"]
        }
    },
    {
        "name": "assemble_video",
        "description": "Combines downloaded video clips with voiceover and subtitles into final MP4. ONLY call after BOTH generate_voiceover AND fetch_video_clips have returned success:true.",
        "input_schema": {"type": "object", "properties": {}, "required": []}
    },
    {
        "name": "generate_thumbnail",
        "description": "Generates a catchy branded YouTube thumbnail. Call after assemble_video. If it fails proceed to upload anyway.",
        "input_schema": {"type": "object", "properties": {}, "required": []}
    },
    {
        "name": "upload_to_youtube",
        "description": "Uploads the final video and thumbnail to YouTube with full metadata including article links. Returns the live YouTube URL.",
        "input_schema": {"type": "object", "properties": {}, "required": []}
    },
    {
        "name": "send_telegram_message",
        "description": "Sends a notification message to the owner on Telegram.",
        "input_schema": {
            "type": "object",
            "properties": {
                "message": {"type": "string"}
            },
            "required": ["message"]
        }
    }
]


# ── Tool implementations ───────────────────────────────────────────────────────

def get_latest_articles():
    try:
        r = requests.get(
            f"{WP_URL}/wp-json/wp/v2/posts",
            params={"per_page": 10, "status": "publish", "orderby": "date"},
            timeout=15
        )
        if r.status_code == 200:
            posts = r.json()
            return {
                "articles": [
                    {
                        "title":   p["title"]["rendered"],
                        "url":     p["link"],
                        "excerpt": p["excerpt"]["rendered"][:200]
                    }
                    for p in posts
                ]
            }
        return {"error": f"WordPress {r.status_code}", "articles": []}
    except Exception as e:
        return {"error": str(e), "articles": []}


def fetch_article_content(article_url, article_title):
    try:
        r = requests.get(
            f"{WP_URL}/wp-json/wp/v2/posts",
            params={"per_page": 1, "status": "publish", "search": article_title},
            timeout=15
        )
        if r.status_code == 200 and r.json():
            post = r.json()[0]
            raw  = post["content"]["rendered"]

            # Extract all external links from article HTML
            all_links = re.findall(r'href=[\'\"](https?://[^\'\"]+)[\'\"]', raw)

            # Skip internal and non-affiliate links
            skip = [
                "heimbuero-test.de", "unsplash.com", "wordpress.org",
                "wp-content", "wp-admin", "gravatar.com"
            ]
            unique_links = []
            seen = set()
            for link in all_links:
                if link not in seen and not any(s in link for s in skip):
                    seen.add(link)
                    unique_links.append(link)

            STATE.affiliate_links = unique_links[:10]

            # Plain text content
            text = re.sub(r"<[^>]+>", " ", raw)
            text = re.sub(r"\s+", " ", text).strip()[:3000]

            STATE.article_title   = article_title
            STATE.article_url     = article_url
            STATE.article_content = text

            return {
                "success":         True,
                "title":           article_title,
                "length":          len(text),
                "links_found":     len(unique_links)
            }
        return {"error": "Article not found", "success": False}
    except Exception as e:
        return {"error": str(e), "success": False}


def generate_video_script():
    try:
        r = requests.post(
            "https://api.anthropic.com/v1/messages",
            headers={"x-api-key": ANTHROPIC_API_KEY,
                     "anthropic-version": "2023-06-01",
                     "content-type": "application/json"},
            json={
                "model": "claude-sonnet-4-6",
                "max_tokens": 800,
                "messages": [{
                    "role": "user",
                    "content": (
                        f"Erstelle ein deutsches Video-Skript (60-90 Sekunden) basierend auf diesem Artikel:\n\n"
                        f"Titel: {STATE.article_title}\n"
                        f"Inhalt: {STATE.article_content[:1500]}\n\n"
                        f"Das Skript soll:\n"
                        f"- Mit einem starken Hook beginnen (Frage oder überraschende Aussage)\n"
                        f"- 3 wichtigste Erkenntnisse nennen\n"
                        f"- Konkrete Produktempfehlungen erwähnen\n"
                        f"- Mit Call-to-Action enden: 'Alle Links findest du in der Videobeschreibung und den vollständigen Test auf heimbuero-test.de'\n"
                        f"- Natürlich und gesprächig klingen\n"
                        f"- Maximal 200 Wörter\n"
                        f"- KEIN [Pause] oder Regieanweisungen — nur reiner Sprechtext\n\n"
                        f"Gib nur den Skripttext aus."
                    )
                }]
            },
            timeout=30
        )
        data = r.json()
        if "content" in data:
            STATE.script = data["content"][0]["text"]
            return {"success": True, "script": STATE.script, "word_count": len(STATE.script.split())}
        return {"error": "API error", "success": False}
    except Exception as e:
        return {"error": str(e), "success": False}


def send_approval_request():
    message = (
        f"<b>Video Agent — Freigabe erforderlich</b>\n\n"
        f"<b>Artikel:</b> {STATE.article_title}\n"
        f"<b>URL:</b> {STATE.article_url}\n\n"
        f"<b>Video-Skript ({len(STATE.script.split())} Wörter):</b>\n{STATE.script}\n\n"
        f"Mit <b>YES</b> bestätigen oder <b>NO</b> ablehnen."
    )
    try:
        requests.post(
            f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage",
            json={"chat_id": TELEGRAM_CHAT_ID, "text": message, "parse_mode": "HTML"},
            timeout=10
        )
    except Exception as e:
        return {"error": str(e)}

    for _ in range(24):
        time.sleep(30)
        try:
            r = requests.get(
                f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/getUpdates",
                params={"offset": -1}, timeout=10
            ).json()
            results = r.get("result", [])
            if results:
                text = results[-1].get("message", {}).get("text", "").strip().upper()
                if text == "YES":
                    return {"approved": True}
                elif text == "NO":
                    return {"approved": False}
        except Exception as e:
            print(f"Poll error: {e}")
    return {"approved": False, "reason": "timeout"}


def generate_voiceover():
    try:
        audio_path = os.path.join(TEMP_DIR, "voiceover.mp3")
        r = requests.post(
            f"https://api.elevenlabs.io/v1/text-to-speech/{ELEVENLABS_VOICE_ID}",
            headers={"xi-api-key": ELEVENLABS_API_KEY, "Content-Type": "application/json"},
            json={
                "text":     STATE.script,
                "model_id": "eleven_multilingual_v2",
                "voice_settings": {
                    "stability":         0.5,
                    "similarity_boost":  0.8,
                    "style":             0.2,
                    "use_speaker_boost": True
                }
            },
            timeout=60
        )
        if r.status_code == 200:
            with open(audio_path, "wb") as f:
                f.write(r.content)
            STATE.audio_path     = audio_path
            duration             = os.path.getsize(audio_path) / 16000
            STATE.audio_duration = duration
            print(f"[Voiceover] Saved {os.path.getsize(audio_path)} bytes, ~{duration:.1f}s")
            return {"success": True, "audio_path": audio_path, "duration_seconds": round(duration)}
        return {"error": f"ElevenLabs {r.status_code}: {r.text[:200]}", "success": False}
    except Exception as e:
        return {"error": str(e), "success": False}


def fetch_video_clips(search_query):
    try:
        r = requests.get(
            "https://api.pexels.com/videos/search",
            headers={"Authorization": PEXELS_API_KEY},
            params={"query": search_query, "per_page": 5,
                    "orientation": "landscape", "size": "medium"},
            timeout=20
        )
        if r.status_code != 200:
            return {"error": f"Pexels {r.status_code}", "success": False}

        videos = r.json().get("videos", [])
        if not videos:
            return {"error": "No videos found", "success": False}

        downloaded = []
        for i, video in enumerate(videos[:4]):
            files = sorted(
                [f for f in video.get("video_files", []) if f.get("width", 0) >= 1280],
                key=lambda x: x.get("width", 0)
            )
            if not files:
                files = video.get("video_files", [])
            if not files:
                continue

            video_url = files[0]["link"]
            clip_path = os.path.join(TEMP_DIR, f"clip_{i}.mp4")

            vr = requests.get(video_url, timeout=90, stream=True)
            if vr.status_code == 200:
                with open(clip_path, "wb") as f:
                    for chunk in vr.iter_content(chunk_size=8192):
                        f.write(chunk)
                file_size = os.path.getsize(clip_path)
                if file_size > 10000:
                    downloaded.append(clip_path)
                    print(f"[Video] Downloaded clip {i+1}: {file_size//1024}KB")

        STATE.video_clips = downloaded
        return {"success": True, "clips_downloaded": len(downloaded), "paths": downloaded}
    except Exception as e:
        return {"error": str(e), "success": False}


def _fmt_time(seconds):
    h  = int(seconds // 3600)
    m  = int((seconds % 3600) // 60)
    s  = int(seconds % 60)
    ms = int((seconds - int(seconds)) * 1000)
    return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"


def assemble_video():
    if not STATE.audio_path or not os.path.exists(STATE.audio_path):
        return {"error": "No audio file. generate_voiceover must complete first.", "success": False}

    audio_size = os.path.getsize(STATE.audio_path)
    if audio_size < 10000:
        return {"error": "Audio file too small.", "success": False}

    valid_clips = [c for c in STATE.video_clips if os.path.exists(c) and os.path.getsize(c) > 10000]
    if len(valid_clips) < 2:
        return {"error": f"Only {len(valid_clips)} valid clips — need at least 2.", "success": False}
    STATE.video_clips = valid_clips

    try:
        audio_duration = STATE.audio_duration if STATE.audio_duration > 0 else audio_size / 16000
        print(f"[Assemble] Audio: {audio_duration:.1f}s, clips: {len(valid_clips)}")

        clip_duration = audio_duration / len(valid_clips)

        # Trim each clip
        trimmed_clips = []
        for i, clip in enumerate(valid_clips):
            trimmed = os.path.join(TEMP_DIR, f"trimmed_{i}.mp4")
            subprocess.run([
                "ffmpeg", "-y", "-i", clip,
                "-t", str(clip_duration),
                "-vf", "scale=1920:1080:force_original_aspect_ratio=decrease,pad=1920:1080:(ow-iw)/2:(oh-ih)/2",
                "-r", "25", "-c:v", "libx264", "-preset", "fast",
                "-an", trimmed
            ], capture_output=True, timeout=120)
            if os.path.exists(trimmed) and os.path.getsize(trimmed) > 1000:
                trimmed_clips.append(trimmed)
                print(f"[Assemble] Trimmed clip {i+1} to {clip_duration:.1f}s")

        if not trimmed_clips:
            return {"error": "All clip trimming failed", "success": False}

        # Loop clips to cover full audio duration
        single_pass = len(trimmed_clips) * clip_duration
        loops_needed = int(audio_duration / single_pass) + 2
        print(f"[Assemble] Looping {len(trimmed_clips)} clips x{loops_needed} to cover {audio_duration:.1f}s")

        concat_file  = os.path.join(TEMP_DIR, "concat.txt")
        concat_video = os.path.join(TEMP_DIR, "concat_video.mp4")
        with open(concat_file, "w") as f:
            for _ in range(loops_needed):
                for clip in trimmed_clips:
                    f.write(f"file '{clip}'\n")

        subprocess.run([
            "ffmpeg", "-y", "-f", "concat", "-safe", "0",
            "-i", concat_file,
            "-t", str(audio_duration),
            "-c", "copy", concat_video
        ], capture_output=True, timeout=300)

        if not os.path.exists(concat_video) or os.path.getsize(concat_video) < 1000:
            return {"error": "Video concatenation failed", "success": False}

        # Generate subtitles
        srt_path = os.path.join(TEMP_DIR, "subtitles.srt")
        words    = STATE.script.split()
        chunk    = 8
        chunks   = [" ".join(words[i:i+chunk]) for i in range(0, len(words), chunk)]
        dur_each = audio_duration / len(chunks) if chunks else 3.0

        with open(srt_path, "w", encoding="utf-8") as f:
            for idx, text in enumerate(chunks):
                start = idx * dur_each
                end   = min(start + dur_each, audio_duration)
                f.write(f"{idx+1}\n")
                f.write(f"{_fmt_time(start)} --> {_fmt_time(end)}\n")
                f.write(f"{text}\n\n")

        # Final merge
        final_path = os.path.join(TEMP_DIR, "final_video.mp4")
        result = subprocess.run([
            "ffmpeg", "-y",
            "-i", concat_video,
            "-i", STATE.audio_path,
            "-vf", f"subtitles={srt_path}:force_style='FontSize=22,PrimaryColour=&HFFFFFF,OutlineColour=&H000000,Outline=2,Alignment=2'",
            "-c:v", "libx264", "-preset", "fast",
            "-c:a", "aac",
            "-t", str(audio_duration),
            final_path
        ], capture_output=True, timeout=300)

        if os.path.exists(final_path) and os.path.getsize(final_path) > 10000:
            STATE.final_video = final_path
            size_mb = os.path.getsize(final_path) / (1024 * 1024)
            print(f"[Assemble] Final video: {size_mb:.1f}MB, {audio_duration:.1f}s")
            return {"success": True, "video_path": final_path, "size_mb": round(size_mb, 1), "duration_seconds": round(audio_duration)}

        stderr = result.stderr.decode("utf-8", errors="ignore")[-500:]
        return {"error": f"FFmpeg merge failed: {stderr}", "success": False}

    except Exception as e:
        return {"error": str(e), "success": False}


def generate_thumbnail():
    try:
        from PIL import Image, ImageDraw, ImageFont

        W, H = 1280, 720
        img  = Image.new("RGB", (W, H), (10, 15, 30))
        draw = ImageDraw.Draw(img)

        # Try to fetch background photo from Pexels
        bg_query = STATE.article_title.split()[0] if STATE.article_title else "home office"
        try:
            r = requests.get(
                "https://api.pexels.com/photos/search",
                headers={"Authorization": PEXELS_API_KEY},
                params={"query": bg_query + " office", "per_page": 1, "orientation": "landscape"},
                timeout=10
            )
            if r.status_code == 200 and r.json().get("photos"):
                photo_url = r.json()["photos"][0]["src"]["large"]
                pr = requests.get(photo_url, timeout=20)
                if pr.status_code == 200:
                    bg   = Image.open(BytesIO(pr.content)).convert("RGB")
                    bg   = bg.resize((W, H))
                    dark = Image.new("RGB", (W, H), (0, 0, 0))
                    img  = Image.blend(bg, dark, alpha=0.6)
                    draw = ImageDraw.Draw(img)
                    print("[Thumbnail] Background fetched")
        except Exception as e:
            print(f"[Thumbnail] Background failed: {e}")

        # Blue left accent bar
        draw.rectangle([0, 0, 14, H], fill=(37, 99, 235))

        # Fonts
        font_path = "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf"
        font_reg  = "/usr/share/fonts/truetype/liberation/LiberationSans-Regular.ttf"
        try:
            font_big   = ImageFont.truetype(font_path, 76)
            font_med   = ImageFont.truetype(font_path, 44)
            font_small = ImageFont.truetype(font_reg,  30)
        except Exception:
            font_big = font_med = font_small = ImageFont.load_default()

        # Title text
        title = STATE.article_title[:70]
        lines = textwrap.wrap(title, width=24)[:2]
        y     = 160
        for line in lines:
            draw.text((92, y + 4), line, font=font_big, fill=(0, 0, 0))
            draw.text((88, y),     line, font=font_big, fill=(255, 255, 255))
            y += 96

        # Blue underline
        draw.rectangle([88, y + 8, min(88 + len(lines[0]) * 40, W - 60), y + 14], fill=(37, 99, 235))

        # Subtitle
        draw.text((88, y + 28), "Vollständiger Test auf heimbuero-test.de", font=font_small, fill=(150, 200, 255))

        # Brand badge
        bx = W - 400
        by = H - 82
        draw.rectangle([bx, by, W - 20, H - 20], fill=(37, 99, 235))
        draw.text((bx + 16, by + 8), "HEIMBUERO TEST", font=font_med, fill=(255, 255, 255))

        thumb_path = os.path.join(TEMP_DIR, "thumbnail.jpg")
        img.save(thumb_path, "JPEG", quality=95)
        STATE.thumbnail_path = thumb_path
        print(f"[Thumbnail] Saved: {thumb_path}")
        return {"success": True, "path": thumb_path}

    except Exception as e:
        return {"error": str(e), "success": False}


def upload_to_youtube():
    try:
        from google.oauth2.credentials import Credentials
        from google.auth.transport.requests import Request
        from googleapiclient.discovery import build
        from googleapiclient.http import MediaFileUpload

        if not STATE.final_video or not os.path.exists(STATE.final_video):
            return {"error": "No video file to upload", "success": False}

        if not YOUTUBE_TOKEN_JSON:
            return {"error": "YOUTUBE_TOKEN_JSON not set", "success": False}

        token_data = json.loads(YOUTUBE_TOKEN_JSON)
        creds = Credentials(
            token=token_data.get("token"),
            refresh_token=token_data.get("refresh_token"),
            token_uri="https://oauth2.googleapis.com/token",
            client_id=YOUTUBE_CLIENT_ID,
            client_secret=YOUTUBE_CLIENT_SECRET,
            scopes=["https://www.googleapis.com/auth/youtube.upload"]
        )

        try:
            creds.refresh(Request())
            print("[YouTube] Token refreshed")
        except Exception as e:
            print(f"[YouTube] Token refresh error: {e}")

        youtube = build("youtube", "v3", credentials=creds)

        # Build links section — each URL on its own line
        links_section = ""
        if STATE.affiliate_links:
            links_section = "🛒 LINKS AUS DEM ARTIKEL:\n"
            for link in STATE.affiliate_links:
                links_section += f"▶ {link}\n"

        # Keep script short to avoid truncation
        short_script = " ".join(STATE.script.split()[:80])

        title = f"{STATE.article_title} | Heimbuero Test"

        # Each URL on its own line so YouTube makes them clickable
        description = (
            f"{short_script}\n"
            f"\n"
            f"📖 Vollständiger Test:\n"
            f"{STATE.article_url}\n"
            f"\n"
            f"🏠 Mehr Homeoffice-Tipps:\n"
            f"https://heimbuero-test.de\n"
            f"\n"
            f"{links_section}"
            f"\n"
            f"#Homeoffice #Büro #Test #Deutschland #Heimarbeit"
        )

        tags = [
            "Homeoffice", "Büro", "Test", "Vergleich", "Deutschland",
            "Heimarbeit", "Bürostuhl", "Monitor", "Schreibtisch",
            STATE.article_title
        ]

        body = {
            "snippet": {
                "title":           title[:100],
                "description":     description[:5000],
                "tags":            tags,
                "categoryId":      "28",
                "defaultLanguage": "de"
            },
            "status": {
                "privacyStatus":           "public",
                "selfDeclaredMadeForKids": False
            }
        }

        media = MediaFileUpload(
            STATE.final_video,
            mimetype="video/mp4",
            resumable=True,
            chunksize=1024 * 1024 * 5
        )

        request  = youtube.videos().insert(part="snippet,status", body=body, media_body=media)
        response = None
        while response is None:
            status, response = request.next_chunk()
            if status:
                print(f"[YouTube] Upload: {int(status.progress() * 100)}%")

        video_id  = response["id"]
        video_url = f"https://youtube.com/watch?v={video_id}"
        print(f"[YouTube] Live: {video_url}")

        # Upload thumbnail if available
        if STATE.thumbnail_path and os.path.exists(STATE.thumbnail_path):
            try:
                youtube.thumbnails().set(
                    videoId=video_id,
                    media_body=MediaFileUpload(STATE.thumbnail_path, mimetype="image/jpeg")
                ).execute()
                print("[YouTube] Thumbnail uploaded")
            except Exception as e:
                print(f"[YouTube] Thumbnail failed: {e}")

        return {"success": True, "video_id": video_id, "url": video_url}

    except Exception as e:
        return {"error": str(e), "success": False}

def send_telegram_message(message):
    try:
        r = requests.post(
            f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage",
            json={"chat_id": TELEGRAM_CHAT_ID, "text": message, "parse_mode": "HTML"},
            timeout=10
        )
        return {"success": r.status_code == 200}
    except Exception as e:
        return {"error": str(e)}


# ── Tool dispatcher ────────────────────────────────────────────────────────────

def dispatch_tool(name, inputs):
    try:
        if name == "get_latest_articles":    return get_latest_articles()
        if name == "fetch_article_content":  return fetch_article_content(**inputs)
        if name == "generate_video_script":  return generate_video_script()
        if name == "send_approval_request":  return send_approval_request()
        if name == "generate_voiceover":     return generate_voiceover()
        if name == "fetch_video_clips":      return fetch_video_clips(**inputs)
        if name == "assemble_video":         return assemble_video()
        if name == "generate_thumbnail":     return generate_thumbnail()
        if name == "upload_to_youtube":      return upload_to_youtube()
        if name == "send_telegram_message":  return send_telegram_message(**inputs)
        return {"error": f"Unknown tool: {name}"}
    except Exception as e:
        return {"error": f"{name} failed: {str(e)}"}


# ── System prompt ──────────────────────────────────────────────────────────────

SYSTEM_PROMPT = f"""You are an autonomous video creation agent for heimbuero-test.de,
a German home office product review website.

Today: {datetime.now().strftime('%d.%m.%Y')}
Goal: Take a published article, create a full-length German video, and upload to YouTube.

YOUR EXACT WORKFLOW — follow this order strictly:

1. get_latest_articles
2. Pick the most recent article
3. fetch_article_content
4. generate_video_script
5. send_approval_request — wait for YES or NO
6. If approved, execute in this EXACT order:
   a. generate_voiceover — wait for success:true before continuing
   b. fetch_video_clips — wait for success:true before continuing
   c. assemble_video — ONLY call after BOTH a and b return success:true
   d. generate_thumbnail — call after assemble_video (if it fails, continue anyway)
   e. upload_to_youtube — call after generate_thumbnail attempt
   f. send_telegram_message — notify owner with YouTube URL
7. If not approved: send_telegram_message confirming skip, stop.

CRITICAL RULES:
- NEVER call assemble_video until BOTH generate_voiceover AND fetch_video_clips return success:true
- fetch_video_clips search query MUST be in English
- Always fetch at least 3-4 video clips
- generate_thumbnail failure is NOT a reason to stop — proceed to upload_to_youtube anyway
- Never upload without owner approval
- Retry failed tools once before notifying owner"""


# ── Main loop ──────────────────────────────────────────────────────────────────

def run_agent():
    print(f"[{datetime.now()}] Video Agent starting...")

    messages = [{
        "role":    "user",
        "content": (
            f"Run your full video creation workflow. "
            f"Today is {datetime.now().strftime('%d.%m.%Y %H:%M')}. "
            f"Start by fetching the latest articles."
        )
    }]

    for iteration in range(MAX_ITERATIONS):
        print(f"\n[Iteration {iteration + 1}/{MAX_ITERATIONS}]")

        response = requests.post(
            "https://api.anthropic.com/v1/messages",
            headers={"x-api-key": ANTHROPIC_API_KEY,
                     "anthropic-version": "2023-06-01",
                     "content-type": "application/json"},
            json={
                "model":      "claude-sonnet-4-6",
                "max_tokens": 1000,
                "system":     SYSTEM_PROMPT,
                "tools":      TOOLS,
                "messages":   messages
            },
            timeout=60
        ).json()

        if "error" in response:
            msg = response["error"].get("message", "Unknown")
            print(f"API error: {msg}")
            send_telegram_message(f"<b>Video Agent error:</b> {msg}")
            break

        stop_reason = response.get("stop_reason")
        content     = response.get("content", [])
        messages.append({"role": "assistant", "content": content})

        for block in content:
            if block.get("type") == "text" and block["text"].strip():
                print(f"[Agent] {block['text'][:300]}")

        if stop_reason == "end_turn":
            print("[Agent] Done.")
            break

        tool_results = []
        for block in content:
            if block.get("type") == "tool_use":
                name    = block["name"]
                inputs  = block.get("input", {})
                tool_id = block["id"]

                print(f"  → {name}({str(inputs)[:80]})")
                result = dispatch_tool(name, inputs)
                print(f"  ← {str(result)[:150]}")

                tool_results.append({
                    "type":        "tool_result",
                    "tool_use_id": tool_id,
                    "content":     json.dumps(result, ensure_ascii=False)
                })

        if tool_results:
            messages.append({"role": "user", "content": tool_results})
        else:
            print(f"Unexpected stop: {stop_reason}")
            break

    print(f"\n[{datetime.now()}] Video Agent finished.")


if __name__ == "__main__":
    run_agent()