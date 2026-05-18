import os
import json
import time
import requests
from datetime import datetime

# ── Environment variables ──────────────────────────────────────────────────────
ANTHROPIC_API_KEY   = os.environ.get("ANTHROPIC_API_KEY")
TELEGRAM_TOKEN      = os.environ.get("TELEGRAM_TOKEN")
TELEGRAM_CHAT_ID    = os.environ.get("TELEGRAM_CHAT_ID")
WP_URL              = os.environ.get("WP_URL")
WP_USER             = os.environ.get("WP_USER")
WP_APP_PASSWORD     = os.environ.get("WP_APP_PASSWORD")
AMAZON_TRACKING_ID  = os.environ.get("AMAZON_TRACKING_ID")
UNSPLASH_ACCESS_KEY = os.environ.get("UNSPLASH_ACCESS_KEY")
SERPAPI_KEY         = os.environ.get("SERPAPI_KEY")

MAX_ITERATIONS = 20

# ── Agent state — content never passes through Claude, stored here ─────────────
class AgentState:
    def __init__(self):
        self.image_html          = ""
        self.article_html        = ""
        self.chosen_keyword      = ""
        self.chosen_outline      = ""
        self.competitor_insights = ""

STATE = AgentState()

# ── Tool definitions ───────────────────────────────────────────────────────────
TOOLS = [
    {
        "name": "get_published_articles",
        "description": "Fetches all published article titles and slugs from WordPress.",
        "input_schema": {"type": "object", "properties": {}, "required": []}
    },
    {
        "name": "search_google_trends",
        "description": "Searches Google Trends for rising home office topics in Germany.",
        "input_schema": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "Seed topic e.g. 'Homeoffice Möbel'"}
            },
            "required": ["query"]
        }
    },
    {
        "name": "search_google",
        "description": "Searches Google.de and returns top results, People Also Ask, and related searches.",
        "input_schema": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "Search query for Google.de"}
            },
            "required": ["query"]
        }
    },
    {
        "name": "get_related_searches",
        "description": "Gets Google autocomplete suggestions for a keyword.",
        "input_schema": {
            "type": "object",
            "properties": {
                "keyword": {"type": "string"}
            },
            "required": ["keyword"]
        }
    },
    {
        "name": "set_chosen_keyword",
        "description": (
            "Call this once you have decided which keyword to target. "
            "Stores your choice and reasoning in agent state."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "keyword":              {"type": "string", "description": "The keyword you chose to write about."},
                "reason":               {"type": "string", "description": "Why you chose this keyword (trend data, competition gap)."},
                "competitor_insights":  {"type": "string", "description": "What competitors are missing that we should cover."}
            },
            "required": ["keyword", "reason"]
        }
    },
    {
        "name": "write_outline",
        "description": (
            "Generates a 5-point outline for the chosen keyword. "
            "Uses competitor insights stored in state. Returns outline text."
        ),
        "input_schema": {"type": "object", "properties": {}, "required": []}
    },
    {
        "name": "send_approval_request",
        "description": (
            "Sends the keyword, reason, and outline to the owner on Telegram for approval. "
            "Then waits for YES or NO reply. Returns approved: true/false."
        ),
        "input_schema": {"type": "object", "properties": {}, "required": []}
    },
    {
        "name": "fetch_image",
        "description": "Fetches a header image from Unsplash for the chosen keyword. Stores in state.",
        "input_schema": {"type": "object", "properties": {}, "required": []}
    },
    {
        "name": "write_article",
        "description": (
            "Writes the full SEO article for the chosen keyword following the outline. "
            "Stores article HTML in state. Returns word count and success status."
        ),
        "input_schema": {"type": "object", "properties": {}, "required": []}
    },
    {
        "name": "publish_article",
        "description": (
            "Combines image + article from state and publishes to WordPress. "
            "Then pings Google for indexing. Returns the live URL."
        ),
        "input_schema": {"type": "object", "properties": {}, "required": []}
    },
    {
        "name": "send_telegram_message",
        "description": "Sends a plain notification message to the owner on Telegram.",
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

def get_published_articles():
    try:
        r = requests.get(
            f"{WP_URL}/wp-json/wp/v2/posts",
            params={"per_page": 100, "status": "publish"},
            timeout=15
        )
        if r.status_code == 200:
            posts = r.json()
            return {
                "titles": [p["title"]["rendered"] for p in posts],
                "slugs":  [p["slug"] for p in posts],
                "count":  len(posts)
            }
        return {"error": f"WordPress {r.status_code}", "titles": [], "slugs": []}
    except Exception as e:
        return {"error": str(e), "titles": [], "slugs": []}


def search_google_trends(query):
    try:
        r = requests.get("https://serpapi.com/search", params={
            "engine": "google_trends", "q": query,
            "geo": "DE", "hl": "de", "api_key": SERPAPI_KEY
        }, timeout=20)
        data = r.json()
        result = {}
        if "related_queries" in data:
            rq = data["related_queries"]
            result["rising"] = [q.get("query") for q in rq.get("rising", [])[:8]]
            result["top"]    = [q.get("query") for q in rq.get("top", [])[:8]]
        if "interest_over_time" in data:
            timeline = data["interest_over_time"].get("timeline_data", [])
            if timeline:
                result["recent_values"] = [
                    {"date": t.get("date"), "value": t.get("values", [{}])[0].get("value")}
                    for t in timeline[-4:]
                ]
        return result or {"note": "No data", "query": query}
    except Exception as e:
        return {"error": str(e)}


def search_google(query):
    try:
        r = requests.get("https://serpapi.com/search", params={
            "engine": "google", "q": query,
            "location": "Germany", "hl": "de", "gl": "de",
            "num": "10", "api_key": SERPAPI_KEY
        }, timeout=20)
        data = r.json()
        return {
            "top_results": [
                {"title": x.get("title"), "snippet": x.get("snippet", "")[:200],
                 "domain": x.get("displayed_link", "")}
                for x in data.get("organic_results", [])[:6]
            ],
            "people_also_ask":  [q.get("question") for q in data.get("related_questions", [])[:6]],
            "related_searches": [s.get("query") for s in data.get("related_searches", [])[:6]]
        }
    except Exception as e:
        return {"error": str(e)}


def get_related_searches(keyword):
    try:
        r = requests.get("https://serpapi.com/search", params={
            "engine": "google_autocomplete", "q": keyword,
            "hl": "de", "gl": "de", "api_key": SERPAPI_KEY
        }, timeout=15)
        data = r.json()
        return {"suggestions": [s.get("value") for s in data.get("suggestions", [])[:10]]}
    except Exception as e:
        return {"error": str(e)}


def set_chosen_keyword(keyword, reason, competitor_insights=""):
    STATE.chosen_keyword      = keyword
    STATE.competitor_insights = competitor_insights
    print(f"[State] Keyword set: {keyword}")
    return {"success": True, "keyword": keyword, "reason": reason}


def write_outline():
    try:
        r = requests.post(
            "https://api.anthropic.com/v1/messages",
            headers={"x-api-key": ANTHROPIC_API_KEY,
                     "anthropic-version": "2023-06-01",
                     "content-type": "application/json"},
            json={
                "model": "claude-sonnet-4-6",
                "max_tokens": 600,
                "messages": [{
                    "role": "user",
                    "content": (
                        f"Erstelle eine SEO-optimierte Gliederung (5 Hauptpunkte) für einen deutschen "
                        f"Artikel zum Thema: '{STATE.chosen_keyword}'.\n"
                        f"Zielgruppe: Heimarbeiter in Deutschland.\n"
                        f"Konkurrenzlücken zu füllen: {STATE.competitor_insights}\n\n"
                        f"Gib nur die 5 Gliederungspunkte aus, nichts weiter."
                    )
                }]
            },
            timeout=30
        )
        data = r.json()
        if "content" in data:
            STATE.chosen_outline = data["content"][0]["text"]
            return {"outline": STATE.chosen_outline, "success": True}
        return {"error": data.get("error", {}).get("message", "Unknown"), "success": False}
    except Exception as e:
        return {"error": str(e), "success": False}


def send_approval_request():
    message = (
        f"<b>Heimbuero Agent — Approval Request</b>\n\n"
        f"<b>Keyword:</b> {STATE.chosen_keyword}\n\n"
        f"<b>Why this keyword:</b>\n{STATE.competitor_insights or 'Trending in Germany'}\n\n"
        f"<b>Outline:</b>\n{STATE.chosen_outline}\n\n"
        f"Reply <b>YES</b> to approve or <b>NO</b> to skip."
    )
    try:
        requests.post(
            f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage",
            json={"chat_id": TELEGRAM_CHAT_ID, "text": message, "parse_mode": "HTML"},
            timeout=10
        )
    except Exception as e:
        return {"error": str(e)}

    # Wait for reply
    for _ in range(24):
        time.sleep(300)
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


def fetch_image():
    try:
        r = requests.get(
            "https://api.unsplash.com/search/photos",
            params={"query": STATE.chosen_keyword, "per_page": 1, "orientation": "landscape"},
            headers={"Authorization": f"Client-ID {UNSPLASH_ACCESS_KEY}"},
            timeout=10
        )
        data = r.json()
        if data.get("results"):
            photo          = data["results"][0]
            img_url        = photo["urls"]["regular"]
            photographer   = photo["user"]["name"]
            ph_url         = photo["user"]["links"]["html"]
            STATE.image_html = (
                f'<figure style="margin:0 0 2rem 0;">'
                f'<img src="{img_url}" alt="{STATE.chosen_keyword}" '
                f'style="width:100%;height:400px;object-fit:cover;border-radius:8px;">'
                f'<figcaption style="font-size:12px;color:#666;margin-top:6px;">'
                f'Foto: <a href="{ph_url}?utm_source=heimbuero_test&utm_medium=referral" '
                f'target="_blank">{photographer}</a> on '
                f'<a href="https://unsplash.com/?utm_source=heimbuero_test&utm_medium=referral" '
                f'target="_blank">Unsplash</a></figcaption></figure>'
            )
            return {"success": True, "photographer": photographer}
        STATE.image_html = ""
        return {"success": False, "reason": "no results"}
    except Exception as e:
        STATE.image_html = ""
        return {"success": False, "error": str(e)}


def write_article():
    try:
        r = requests.post(
            "https://api.anthropic.com/v1/messages",
            headers={"x-api-key": ANTHROPIC_API_KEY,
                     "anthropic-version": "2023-06-01",
                     "content-type": "application/json"},
            json={
                "model": "claude-sonnet-4-6",
                "max_tokens": 8000,
                "messages": [{
                    "role": "user",
                    "content": (
                        f"Schreibe einen ausführlichen deutschen SEO-Artikel zum Thema: '{STATE.chosen_keyword}'.\n\n"
                        f"Gliederung:\n{STATE.chosen_outline}\n\n"
                        f"Anforderungen:\n"
                        f"- Mindestens 1200 Wörter\n"
                        f"- SEO-optimiert, Keyword natürlich eingebaut\n"
                        f"- Verwende das Jahr {datetime.now().year}\n"
                        f"- Praxisnahe Tipps für Heimarbeiter in Deutschland\n"
                        f"- Beantworte häufige Fragen der Leser\n"
                        f"- Decke diese Aspekte ab die Konkurrenten vernachlässigen: {STATE.competitor_insights}\n"
                        f"- Füge 3-5 Amazon.de Produktlinks ein:\n"
                        f"  <a href='https://www.amazon.de/s?k=SUCHBEGRIFF&tag={AMAZON_TRACKING_ID}' "
                        f"rel='nofollow' target='_blank'>Produktname auf Amazon ansehen</a>\n"
                        f"- Ersetze SUCHBEGRIFF mit passendem deutschen Begriff\n"
                        f"- Professioneller aber freundlicher Ton\n"
                        f"- Reines HTML mit H1/H2/H3 Tags\n"
                        f"- KEIN Markdown, KEINE Code-Blöcke, KEIN ```html"
                        f"- Kein Affiliate-Disclaimer, kein Hinweis-Text am Ende des Artikels"
                    )
                }]
            },
            timeout=120
        )
        data = r.json()
        if "content" in data:
            STATE.article_html = data["content"][0]["text"]
            word_count = len(STATE.article_html.split())
            return {"success": True, "word_count": word_count}
        return {"error": data.get("error", {}).get("message", "Unknown"), "success": False}
    except Exception as e:
        return {"error": str(e), "success": False}


def publish_article():
    # Combine in Python — never passes through Claude
    full_content = STATE.image_html + STATE.article_html

    if not full_content.strip():
        return {"success": False, "error": "No content to publish — write_article must be called first"}

    try:
        r = requests.post(
            f"{WP_URL}/wp-json/wp/v2/posts",
            auth=(WP_USER, WP_APP_PASSWORD),
            json={
                "title":      STATE.chosen_keyword,
                "content":    full_content,
                "status":     "publish",
                "categories": [4]
            },
            timeout=30
        )
        if r.status_code == 201:
            link = r.json().get("link", "")
            # Ping Google
            try:
                requests.post(
                    "https://indexing.googleapis.com/v3/urlNotifications:publish",
                    headers={"Content-Type": "application/json"},
                    json={"url": link, "type": "URL_UPDATED"},
                    timeout=10
                )
            except Exception:
                pass
            return {"success": True, "link": link}
        return {"success": False, "status": r.status_code, "body": r.text[:300]}
    except Exception as e:
        return {"success": False, "error": str(e)}


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
        if name == "get_published_articles":     return get_published_articles()
        if name == "search_google_trends":       return search_google_trends(**inputs)
        if name == "search_google":              return search_google(**inputs)
        if name == "get_related_searches":       return get_related_searches(**inputs)
        if name == "set_chosen_keyword":         return set_chosen_keyword(**inputs)
        if name == "write_outline":              return write_outline()
        if name == "send_approval_request":      return send_approval_request()
        if name == "fetch_image":                return fetch_image()
        if name == "write_article":              return write_article()
        if name == "publish_article":            return publish_article()
        if name == "send_telegram_message":      return send_telegram_message(**inputs)
        return {"error": f"Unknown tool: {name}"}
    except Exception as e:
        return {"error": f"{name} failed: {str(e)}"}


# ── System prompt ──────────────────────────────────────────────────────────────

SYSTEM_PROMPT = f"""You are an autonomous SEO affiliate agent for heimbuero-test.de,
a German home office product review website.

Today: {datetime.now().strftime('%d.%m.%Y')}
Niche: Home office products for German workers.

IMPORTANT: You never pass article content between tools. Content is stored 
automatically in agent state. Just call the tools in the right order.

YOUR EXACT WORKFLOW — follow this order:

PHASE 1 — RESEARCH:
1. get_published_articles — see what is already live
2. search_google_trends("Homeoffice") — find rising topics
3. search_google_trends("Büro Zubehör") — find more opportunities
4. get_related_searches for the most promising trend
5. search_google on the best candidate to analyse competition
6. set_chosen_keyword — commit to your choice with reason and competitor_insights

PHASE 2 — APPROVAL:
7. write_outline — generates outline using stored keyword
8. send_approval_request — sends keyword + outline to owner, waits for YES/NO

PHASE 3 — PUBLISH (only if approved=true):
9. fetch_image — gets header image for stored keyword
10. write_article — writes full article using stored keyword + outline
11. publish_article — combines image + article and publishes to WordPress
12. send_telegram_message — notify owner of success with the URL

If approved=false: send_telegram_message confirming skip, then stop.

RULES:
- Always complete Phase 1 fully before choosing a keyword
- Never skip set_chosen_keyword — it stores your choice for other tools
- Never try to pass article text as a parameter — publish_article handles everything
- Pick keywords with buying intent: Test, Vergleich, bester, unter X Euro
- Avoid already published topics"""


# ── Main loop ──────────────────────────────────────────────────────────────────

def run_agent():
    print(f"[{datetime.now()}] Agent starting...")

    messages = [{
        "role":    "user",
        "content": (
            f"Run your full workflow. Today is {datetime.now().strftime('%d.%m.%Y %H:%M')}. "
            f"Start with research phase."
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
                "model":    "claude-sonnet-4-6",
                "max_tokens": 1000,
                "system":   SYSTEM_PROMPT,
                "tools":    TOOLS,
                "messages": messages
            },
            timeout=60
        ).json()

        if "error" in response:
            msg = response["error"].get("message", "Unknown")
            print(f"API error: {msg}")
            send_telegram_message(f"<b>Agent error:</b> {msg}")
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

    print(f"\n[{datetime.now()}] Agent finished.")


if __name__ == "__main__":
    run_agent()