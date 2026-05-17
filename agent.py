import os
import json
import time
import requests
from datetime import datetime

ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY")
TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID")
WP_URL = os.environ.get("WP_URL")
WP_USER = os.environ.get("WP_USER")
WP_APP_PASSWORD = os.environ.get("WP_APP_PASSWORD")
AMAZON_TRACKING_ID = os.environ.get("AMAZON_TRACKING_ID")


KEYWORDS = [
    "Bester Bürostuhl unter 200 Euro",
    "Höhenverstellbarer Schreibtisch Test",
    "Bestes Monitor für Home Office",
    "Webcam Test Homeoffice",
    "Schreibtischlampe LED Test",
    "Stehpult Vergleich 2026",
    "Bestes Headset Home Office",
    "Ergonomische Maus Test",
    "Tastatur Home Office Test",
    "Laptop Ständer Test",
    "Tischlampe Büro Test",
    "Drucker Home Office Test",
    "Schreibtisch Organizer Test",
    "Bester Schreibtischstuhl Test",
    "UPS Unterbrechungsfreie Stromversorgung Test",
]


def send_telegram(message):
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    requests.post(url, json={
        "chat_id": TELEGRAM_CHAT_ID,
        "text": message,
        "parse_mode": "HTML"
    })


def ask_approval(topic, outline):
    message = (
        f"<b>Heimbuero Agent — Approval Request</b>\n\n"
        f"I want to write and publish this article:\n\n"
        f"<b>Topic:</b> {topic}\n\n"
        f"<b>Outline:</b>\n{outline}\n\n"
        f"Reply <b>YES</b> to approve or <b>NO</b> to skip."
    )
    send_telegram(message)

    for _ in range(24):
        time.sleep(300)
        url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/getUpdates"
        response = requests.get(url, params={"offset": -1}).json()
        results = response.get("result", [])
        if results:
            last = results[-1]
            text = last.get("message", {}).get("text", "").strip().upper()
            if text == "YES":
                return True
            elif text == "NO":
                return False
    return False


def generate_article(keyword):
    response = requests.post(
        "https://api.anthropic.com/v1/messages",
        headers={
            "x-api-key": ANTHROPIC_API_KEY,
            "anthropic-version": "2023-06-01",
            "content-type": "application/json",
        },
        json={
            "model": "claude-sonnet-4-6",
            "max_tokens": 4000,
            "messages": [{
                "role": "user",
                "content": (
                    f"Schreibe einen ausführlichen deutschen SEO-Artikel zum Thema: '{keyword}'.\n\n"
                    f"Anforderungen:\n"
                    f"- Mindestens 1200 Wörter\n"
                    f"- SEO-optimiert für das Keyword\n"
                    f"- Struktur: H1 Titel, Einleitung, H2 Abschnitte, Fazit\n"
                    f"- Verwende immer das aktuelle Jahr {datetime.now().year}, niemals ältere Jahre\n"
                    f"- Praxisnahe Empfehlungen für Heimarbeiter in Deutschland\n"
                    f"- Füge 3-5 Amazon.de Produktlinks ein mit diesem Format: "
                    f"<a href='https://www.amazon.de/s?k=SUCHBEGRIFF&tag={AMAZON_TRACKING_ID}' rel='nofollow' target='_blank'>Produktname auf Amazon ansehen</a>\n"
                    f"- Ersetze SUCHBEGRIFF mit dem passenden deutschen Suchbegriff für das Produkt\n"
                    f"- Professioneller aber freundlicher Ton\n"
                    f"- HTML Format mit korrekten Heading-Tags\n"
                    f"- Kein Markdown, keine Code-Blöcke, kein ```html Tag am Anfang, kein ``` am Ende."
                )            }]
        }
    )
    data = response.json()
    return data["content"][0]["text"]


def generate_outline(keyword):
    response = requests.post(
        "https://api.anthropic.com/v1/messages",
        headers={
            "x-api-key": ANTHROPIC_API_KEY,
            "anthropic-version": "2023-06-01",
            "content-type": "application/json",
        },
        json={
            "model": "claude-sonnet-4-6",
            "max_tokens": 500,
            "messages": [{
                "role": "user",
                "content": (
                    f"Erstelle eine kurze Gliederung (5 Punkte) für einen deutschen Artikel zum Thema: '{keyword}'. "
                    f"Nur die Gliederungspunkte, kein weiterer Text."
                )
            }]
        }
    )
    data = response.json()
    return data["content"][0]["text"]


def publish_to_wordpress(title, content):
    response = requests.post(
        f"{WP_URL}/wp-json/wp/v2/posts",
        auth=(WP_USER, WP_APP_PASSWORD),
        json={
            "title": title,
            "content": content,
            "status": "publish",
            "categories": [],
            "tags": [],
        }
    )
    return response.status_code == 201, response.json().get("link", "")


def get_published_titles():
    response = requests.get(
        f"{WP_URL}/wp-json/wp/v2/posts",
        params={"per_page": 100, "status": "publish"}
    )
    if response.status_code == 200:
        return [post["title"]["rendered"] for post in response.json()]
    return []


def get_image_for_article(keyword):
    response = requests.get(
        "https://api.unsplash.com/search/photos",
        params={
            "query": keyword,
            "per_page": 1,
            "orientation": "landscape"
        },
        headers={"Authorization": f"Client-ID {os.environ.get('UNSPLASH_ACCESS_KEY')}"}
    )
    data = response.json()
    if data.get("results"):
        photo = data["results"][0]
        img_url = photo["urls"]["regular"]
        photographer = photo["user"]["name"]
        photographer_url = photo["user"]["links"]["html"]
        return f'''<figure style="margin:0 0 2rem 0;">
<img src="{img_url}" alt="{keyword}" style="width:100%;height:400px;object-fit:cover;border-radius:8px;">
<figcaption style="font-size:12px;color:#666;margin-top:6px;">
Foto: <a href="{photographer_url}?utm_source=heimbuero_test&utm_medium=referral" target="_blank">{photographer}</a> on <a href="https://unsplash.com/?utm_source=heimbuero_test&utm_medium=referral" target="_blank">Unsplash</a>
</figcaption>
</figure>'''
    return ""


def run_agent():
    send_telegram(
        "<b>Heimbuero Agent starting up</b>\n\n"
        f"Date: {datetime.now().strftime('%d.%m.%Y %H:%M')}\n"
        "Scanning for next article to write..."
    )

    published = get_published_titles()
    keyword = None

    for kw in KEYWORDS:
        already_done = any(kw.lower() in title.lower() for title in published)
        if not already_done:
            keyword = kw
            break

    if not keyword:
        send_telegram(
            "<b>Heimbuero Agent</b>\n\n"
            "All planned articles have been published. "
            "Add more keywords to the list to continue."
        )
        return

    send_telegram(f"<b>Selected keyword:</b> {keyword}\n\nGenerating outline...")

    outline = generate_outline(keyword)
    approved = ask_approval(keyword, outline)

    if not approved:
        send_telegram(f"Skipped: {keyword}. Will try next time.")
        return

    send_telegram(f"Approved! Writing full article for: <b>{keyword}</b>...")
    image_html = get_image_for_article(keyword)
    content = image_html + generate_article(keyword)



    success, link = publish_to_wordpress(keyword, content)

    if success:
        send_telegram(
            f"<b>Article published successfully!</b>\n\n"
            f"<b>Title:</b> {keyword}\n"
            f"<b>URL:</b> {link}\n\n"
            f"Google will index this within 1-7 days."
        )
    else:
        send_telegram(
            f"<b>Failed to publish article.</b>\n"
            f"Topic: {keyword}\n"
            f"Check your WordPress credentials."
        )


if __name__ == "__main__":
    run_agent()