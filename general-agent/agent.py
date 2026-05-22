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
WP_CATEGORY_ID      = int(os.environ.get("WP_CATEGORY_ID", "4"))

MAX_ITERATIONS = 20

# ── Agent state — content never passes through Claude, stored here ─────────────
class AgentState:
    def __init__(self):
        self.image_html          = ""
        self.article_html        = ""
        self.chosen_keyword      = ""
        self.chosen_outline      = ""
        self.competitor_insights = ""
        self.meta_description    = ""

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

    last_update_id = 0
    try:
        r = requests.get(
            f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/getUpdates",
            params={"offset": -1}, timeout=10
        ).json()
        results = r.get("result", [])
        if results:
            last_update_id = results[-1]["update_id"]
    except Exception:
        pass

    try:
        requests.post(
            f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage",
            json={"chat_id": TELEGRAM_CHAT_ID, "text": message, "parse_mode": "HTML"},
            timeout=10
        )
    except Exception as e:
        print(f"Telegram send error: {e}")

    for _ in range(24):
        time.sleep(300)
        try:
            r = requests.get(
                f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/getUpdates",
                params={"offset": last_update_id + 1}, timeout=10
            ).json()
            for update in r.get("result", []):
                last_update_id = max(last_update_id, update["update_id"])
                text = update.get("message", {}).get("text", "").strip().upper()
                if text == "YES":
                    return {"approved": True}
                elif text == "NO":
                    return {"approved": False}
        except Exception as e:
            print(f"Poll error: {e}")
    return {"approved": False, "reason": "timeout"}


def fetch_image():
    try:
        # Translate common German words for better Unsplash results
        english_query = STATE.chosen_keyword \
            .replace("Bürostuhl", "office chair") \
            .replace("Schreibtischstuhl", "office chair") \
            .replace("Schreibtisch", "desk") \
            .replace("Schreibtischlampe", "desk lamp") \
            .replace("Tischlampe", "desk lamp") \
            .replace("Monitor", "monitor") \
            .replace("Drucker", "printer") \
            .replace("Headset", "headset") \
            .replace("Homeoffice", "home office") \
            .replace("Tastatur", "keyboard") \
            .replace("Maus", "mouse") \
            .replace("Webcam", "webcam") \
            .replace("Laptop", "laptop") \
            .replace("Ständer", "stand") \
            .replace("Höhenverstellbarer", "adjustable") \
            .replace("Ergonomische", "ergonomic") \
            .replace("Test", "") \
            .replace("Vergleich", "") \
            .replace("bester", "best") \
            .replace("Bestes", "best") \
            .replace("Bester", "best") \
            .strip()

        print(f"[Image] Searching Unsplash for: {english_query}")

        r = requests.get(
            "https://api.unsplash.com/search/photos",
            params={"query": english_query, "per_page": 1, "orientation": "landscape"},
            headers={"Authorization": f"Client-ID {UNSPLASH_ACCESS_KEY}"},
            timeout=10
        )
        data = r.json()
        if data.get("results"):
            photo        = data["results"][0]
            img_url      = photo["urls"]["regular"]
            photographer = photo["user"]["name"]
            ph_url       = photo["user"]["links"]["html"]
            STATE.image_html = (
                f'<figure style="margin:0 0 2rem 0;">'
                f'<img src="{img_url}" alt="{STATE.chosen_keyword} – Homeoffice Ratgeber {datetime.now().year}" '
                f'style="width:100%;height:400px;object-fit:cover;border-radius:8px;">'
                f'<figcaption style="font-size:12px;color:#666;margin-top:6px;">'
                f'Foto: <a href="{ph_url}?utm_source=heimbuero_test&utm_medium=referral" '
                f'target="_blank">{photographer}</a> on '
                f'<a href="https://unsplash.com/?utm_source=heimbuero_test&utm_medium=referral" '
                f'target="_blank">Unsplash</a></figcaption></figure>'
            )
            return {"success": True, "photographer": photographer, "query_used": english_query}
        STATE.image_html = ""
        return {"success": False, "reason": "no results", "query_used": english_query}
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
                        f"- Füge 3-4 Amazon.de Produktlinks ein:\n"
                        f"  <a href='https://www.amazon.de/s?k=SUCHBEGRIFF&tag={AMAZON_TRACKING_ID}' "
                        f"rel='nofollow' target='_blank'>Produktname auf Amazon ansehen</a>\n"
                        f"- Ersetze SUCHBEGRIFF mit passendem deutschen Begriff\n"
                        f"- Füge außerdem 1-2 OTTO Links ein wo es passt:\n"
                        f"  Für allgemeine Produkte: <a href='https://tidd.ly/4usYoRq' rel='nofollow' target='_blank'>Passende Produkte bei OTTO ansehen</a>\n"
                        f"  Für Büroausstattung: <a href='https://tidd.ly/4wW7IPw' rel='nofollow' target='_blank'>Bürobedarf bei OTTO Office ansehen</a>\n"
                        f"- Professioneller aber freundlicher Ton\n"
                        f"- Reines HTML mit H1/H2/H3 Tags\n"
                        f"- KEIN Markdown, KEINE Code-Blöcke, KEIN ```html\n"
                        f"- Kein Affiliate-Disclaimer, kein Hinweis-Text am Ende des Artikels\n"
                        f"- WICHTIG: Schreibe NIEMALS so als ob wir die Produkte persönlich getestet haben.\n"
                        f"  VERBOTEN: 'wir haben getestet', 'in unserem Test', 'haben wir ausprobiert',\n"
                        f"  'im Praxistest', 'unser Testsieger', 'unter realen Bedingungen getestet',\n"
                        f"  'ausführlich getestet', 'auf Basis unserer Tests', 'haben wir gemessen'.\n"
                        f"  STATTDESSEN verwende: 'laut Nutzerbewertungen', 'laut Hersteller',\n"
                        f"  'Nutzerbewertungen zeigen', 'basierend auf Produktdaten und Bewertungen',\n"
                        f"  'unsere Empfehlung', 'Preis-Leistungs-Tipp', 'laut Testberichten',\n"
                        f"  'in der Praxis berichten Nutzer', 'Experten empfehlen'."
                    )                
                }]
            },
            timeout=120
        )
        data = r.json()
        if "content" in data:
            STATE.article_html = data["content"][0]["text"]
            word_count = len(STATE.article_html.split())
            try:
                meta_r = requests.post(
                    "https://api.anthropic.com/v1/messages",
                    headers={"x-api-key": ANTHROPIC_API_KEY,
                             "anthropic-version": "2023-06-01",
                             "content-type": "application/json"},
                    json={
                        "model": "claude-haiku-4-5-20251001",
                        "max_tokens": 80,
                        "messages": [{
                            "role": "user",
                            "content": (
                                f"Schreibe eine Meta-Beschreibung auf Deutsch (120-155 Zeichen) für:\n"
                                f"Keyword: {STATE.chosen_keyword}\n"
                                f"Enthält das Keyword, beschreibt den Mehrwert. Nur die Beschreibung, kein Anführungszeichen."
                            )
                        }]
                    },
                    timeout=15
                )
                STATE.meta_description = meta_r.json().get("content", [{}])[0].get("text", "").strip()[:155]
            except Exception:
                STATE.meta_description = f"{STATE.chosen_keyword} – Test, Vergleich und Kaufberatung für das Homeoffice."
            return {"success": True, "word_count": word_count}
        return {"error": data.get("error", {}).get("message", "Unknown"), "success": False}
    except Exception as e:
        return {"error": str(e), "success": False}


def _ping_search_engines():
    sitemap = f"{WP_URL}/sitemap.xml"
    for ping_url in [
        f"https://www.google.com/ping?sitemap={sitemap}",
        f"https://www.bing.com/ping?sitemap={sitemap}"
    ]:
        try:
            requests.get(ping_url, timeout=5)
        except Exception:
            pass


def publish_article():
    schema = {
        "@context": "https://schema.org",
        "@type": "Article",
        "headline": STATE.chosen_keyword,
        "description": STATE.meta_description or STATE.chosen_keyword,
        "datePublished": datetime.now().strftime("%Y-%m-%d"),
        "publisher": {"@type": "Organization", "name": "HeimBüro Test", "url": WP_URL or "https://heimbuero-test.de"}
    }
    schema_script = f'\n<script type="application/ld+json">{json.dumps(schema, ensure_ascii=False)}</script>'
    full_content = STATE.image_html + STATE.article_html + schema_script

    if not STATE.article_html.strip():
        return {"success": False, "error": "No content to publish — write_article must be called first"}

    try:
        r = requests.post(
            f"{WP_URL}/wp-json/wp/v2/posts",
            auth=(WP_USER, WP_APP_PASSWORD),
            json={
                "title":      STATE.chosen_keyword,
                "content":    full_content,
                "status":     "publish",
                "categories": [WP_CATEGORY_ID],
                "meta":       {"_yoast_wpseo_metadesc": STATE.meta_description}
            },
            timeout=30
        )
        if r.status_code == 201:
            link = r.json().get("link", "")
            _ping_search_engines()
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

def _system_prompt():
    return f"""You are an autonomous SEO affiliate agent for heimbuero-test.de,
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
- Avoid already published topics
- Keep the chosen keyword (used as article title) ≤60 characters, primary keyword first"""


# ── Main loop ──────────────────────────────────────────────────────────────────

def run_agent():
    print(f"[{datetime.now()}] Agent starting...")
    system_prompt = _system_prompt()

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
                     "anthropic-beta": "prompt-caching-2024-07-31",
                     "content-type": "application/json"},
            json={
                "model":    "claude-sonnet-4-6",
                "max_tokens": 1000,
                "system":   [{"type": "text", "text": system_prompt, "cache_control": {"type": "ephemeral"}}],
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