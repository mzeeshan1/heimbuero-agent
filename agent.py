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

MAX_ITERATIONS = 20  # More iterations needed for research phase

# ── Tool definitions ───────────────────────────────────────────────────────────
TOOLS = [
    {
        "name": "get_published_articles",
        "description": "Fetches all published article titles and URLs from WordPress to avoid duplicates.",
        "input_schema": {"type": "object", "properties": {}, "required": []}
    },
    {
        "name": "search_google_trends",
        "description": (
            "Searches Google Trends for trending topics in Germany related to home office. "
            "Returns rising and top queries to identify what Germans are searching for right now."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "The seed topic to find trends for e.g. 'Homeoffice Möbel'"
                }
            },
            "required": ["query"]
        }
    },
    {
        "name": "search_google",
        "description": (
            "Searches Google.de and returns real search results including titles, snippets, "
            "and People Also Ask questions. Use this to analyse competition and find keyword gaps."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "The search query to run on Google.de"
                }
            },
            "required": ["query"]
        }
    },
    {
        "name": "get_related_searches",
        "description": (
            "Gets related searches and autocomplete suggestions from Google.de "
            "for a given keyword. Useful for finding long-tail keyword opportunities."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "keyword": {
                    "type": "string",
                    "description": "The keyword to find related searches for"
                }
            },
            "required": ["keyword"]
        }
    },
    {
        "name": "send_telegram_message",
        "description": "Sends a message to the owner via Telegram.",
        "input_schema": {
            "type": "object",
            "properties": {
                "message": {"type": "string", "description": "HTML-formatted message to send."}
            },
            "required": ["message"]
        }
    },
    {
        "name": "wait_for_telegram_approval",
        "description": "Waits for owner to reply YES or NO on Telegram. Returns approved: true/false.",
        "input_schema": {
            "type": "object",
            "properties": {
                "timeout_minutes": {"type": "integer", "description": "Minutes to wait. Default 120."}
            },
            "required": []
        }
    },
    {
        "name": "fetch_unsplash_image",
        "description": "Fetches a relevant high quality landscape image from Unsplash.",
        "input_schema": {
            "type": "object",
            "properties": {
                "keyword": {"type": "string", "description": "Search term for the image."}
            },
            "required": ["keyword"]
        }
    },
    {
        "name": "write_outline",
        "description": "Generates a 5-point SEO article outline for a given keyword.",
        "input_schema": {
            "type": "object",
            "properties": {
                "keyword": {"type": "string"},
                "competitor_insights": {
                    "type": "string",
                    "description": "What competitors are covering — used to make the outline better."
                }
            },
            "required": ["keyword"]
        }
    },
    {
        "name": "write_article",
        "description": "Writes a full SEO-optimised German article following the approved outline.",
        "input_schema": {
            "type": "object",
            "properties": {
                "keyword": {"type": "string"},
                "outline": {"type": "string"},
                "competitor_insights": {
                    "type": "string",
                    "description": "What to cover that competitors miss — makes article rank higher."
                }
            },
            "required": ["keyword", "outline"]
        }
    },
    {
        "name": "publish_to_wordpress",
        "description": (
            "Publishes the completed article to WordPress. "
            "The content parameter MUST be the image HTML concatenated with the article HTML "
            "as a single string. Always combine them before calling this tool: "
            "content = image_html + article_html"
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "title": {"type": "string", "description": "The article title."},
                "content": {
                    "type": "string",
                    "description": "REQUIRED: image HTML + article HTML combined into one string."
                }
            },
            "required": ["title", "content"]
        }
    },
    {
        "name": "request_google_indexing",
        "description": "Pings Google to index the newly published article.",
        "input_schema": {
            "type": "object",
            "properties": {
                "url": {"type": "string"}
            },
            "required": ["url"]
        }
    }
]

# ── Tool implementations ───────────────────────────────────────────────────────

def get_published_articles():
    try:
        response = requests.get(
            f"{WP_URL}/wp-json/wp/v2/posts",
            params={"per_page": 100, "status": "publish"},
            timeout=15
        )
        if response.status_code == 200:
            posts = response.json()
            return {
                "titles": [p["title"]["rendered"] for p in posts],
                "slugs":  [p["slug"] for p in posts],
                "count":  len(posts)
            }
        return {"error": f"WordPress {response.status_code}", "titles": [], "slugs": []}
    except Exception as e:
        return {"error": str(e), "titles": [], "slugs": []}


def search_google_trends(query):
    try:
        response = requests.get(
            "https://serpapi.com/search",
            params={
                "engine":  "google_trends",
                "q":       query,
                "geo":     "DE",
                "hl":      "de",
                "api_key": SERPAPI_KEY
            },
            timeout=20
        )
        data = response.json()
        result = {}
        if "interest_over_time" in data:
            timeline = data["interest_over_time"].get("timeline_data", [])
            if timeline:
                recent = timeline[-4:]
                result["recent_interest"] = [
                    {"date": t.get("date"), "value": t.get("values", [{}])[0].get("value")}
                    for t in recent
                ]
        if "related_queries" in data:
            rq = data["related_queries"]
            result["rising_queries"] = [
                q.get("query") for q in rq.get("rising", [])[:8]
            ]
            result["top_queries"] = [
                q.get("query") for q in rq.get("top", [])[:8]
            ]
        return result if result else {"note": "No trend data found", "query": query}
    except Exception as e:
        return {"error": str(e)}


def search_google(query):
    try:
        response = requests.get(
            "https://serpapi.com/search",
            params={
                "engine":   "google",
                "q":        query,
                "location": "Germany",
                "hl":       "de",
                "gl":       "de",
                "num":      "10",
                "api_key":  SERPAPI_KEY
            },
            timeout=20
        )
        data = response.json()
        results = {}

        # Organic results
        organic = data.get("organic_results", [])[:6]
        results["top_results"] = [
            {
                "title":   r.get("title"),
                "snippet": r.get("snippet", "")[:200],
                "domain":  r.get("displayed_link", "")
            }
            for r in organic
        ]

        # People Also Ask
        paa = data.get("related_questions", [])[:6]
        results["people_also_ask"] = [q.get("question") for q in paa]

        # Related searches
        related = data.get("related_searches", [])[:6]
        results["related_searches"] = [r.get("query") for r in related]

        return results
    except Exception as e:
        return {"error": str(e)}


def get_related_searches(keyword):
    try:
        response = requests.get(
            "https://serpapi.com/search",
            params={
                "engine":   "google_autocomplete",
                "q":        keyword,
                "hl":       "de",
                "gl":       "de",
                "api_key":  SERPAPI_KEY
            },
            timeout=15
        )
        data = response.json()
        suggestions = [
            s.get("value") for s in data.get("suggestions", [])[:10]
        ]
        return {"suggestions": suggestions, "keyword": keyword}
    except Exception as e:
        return {"error": str(e)}


def send_telegram_message(message):
    try:
        url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
        r = requests.post(url, json={
            "chat_id":    TELEGRAM_CHAT_ID,
            "text":       message,
            "parse_mode": "HTML"
        }, timeout=10)
        return {"success": r.status_code == 200}
    except Exception as e:
        return {"error": str(e)}


def wait_for_telegram_approval(timeout_minutes=120):
    checks = max(1, timeout_minutes // 5)
    for _ in range(checks):
        time.sleep(300)
        try:
            url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/getUpdates"
            data = requests.get(url, params={"offset": -1}, timeout=10).json()
            results = data.get("result", [])
            if results:
                text = results[-1].get("message", {}).get("text", "").strip().upper()
                if text == "YES":
                    return {"approved": True}
                elif text == "NO":
                    return {"approved": False}
        except Exception as e:
            print(f"Telegram poll error: {e}")
    return {"approved": False, "reason": "timeout"}


def fetch_unsplash_image(keyword):
    try:
        r = requests.get(
            "https://api.unsplash.com/search/photos",
            params={"query": keyword, "per_page": 1, "orientation": "landscape"},
            headers={"Authorization": f"Client-ID {UNSPLASH_ACCESS_KEY}"},
            timeout=10
        )
        data = r.json()
        if data.get("results"):
            photo = data["results"][0]
            img_url        = photo["urls"]["regular"]
            photographer   = photo["user"]["name"]
            photographer_url = photo["user"]["links"]["html"]
            html = (
                f'<figure style="margin:0 0 2rem 0;">'
                f'<img src="{img_url}" alt="{keyword}" '
                f'style="width:100%;height:400px;object-fit:cover;border-radius:8px;">'
                f'<figcaption style="font-size:12px;color:#666;margin-top:6px;">'
                f'Foto: <a href="{photographer_url}?utm_source=heimbuero_test&utm_medium=referral" '
                f'target="_blank">{photographer}</a> on '
                f'<a href="https://unsplash.com/?utm_source=heimbuero_test&utm_medium=referral" '
                f'target="_blank">Unsplash</a></figcaption></figure>'
            )
            return {"html": html, "success": True}
        return {"html": "", "success": False, "reason": "no results"}
    except Exception as e:
        return {"html": "", "success": False, "error": str(e)}


def write_outline(keyword, competitor_insights=""):
    try:
        competitor_section = (
            f"\n\nKonkurrenzanalyse (diese Punkte sollte der Artikel besser abdecken):\n{competitor_insights}"
            if competitor_insights else ""
        )
        r = requests.post(
            "https://api.anthropic.com/v1/messages",
            headers={
                "x-api-key":         ANTHROPIC_API_KEY,
                "anthropic-version": "2023-06-01",
                "content-type":      "application/json",
            },
            json={
                "model":      "claude-sonnet-4-6",
                "max_tokens": 600,
                "messages": [{
                    "role":    "user",
                    "content": (
                        f"Erstelle eine SEO-optimierte Gliederung (5 Hauptpunkte) für einen deutschen "
                        f"Artikel zum Thema: '{keyword}'.\n"
                        f"Zielgruppe: Heimarbeiter in Deutschland.\n"
                        f"Ziel: In Google.de auf Seite 1 ranken.{competitor_section}\n\n"
                        f"Gib nur die 5 Gliederungspunkte aus, nichts weiter."
                    )
                }]
            },
            timeout=30
        )
        data = r.json()
        if "content" in data:
            return {"outline": data["content"][0]["text"], "success": True}
        return {"error": data.get("error", {}).get("message", "Unknown"), "success": False}
    except Exception as e:
        return {"error": str(e), "success": False}


def write_article(keyword, outline, competitor_insights=""):
    try:
        competitor_section = (
            f"\n- Decke diese Aspekte ab, die Konkurrenten vernachlässigen: {competitor_insights}"
            if competitor_insights else ""
        )
        r = requests.post(
            "https://api.anthropic.com/v1/messages",
            headers={
                "x-api-key":         ANTHROPIC_API_KEY,
                "anthropic-version": "2023-06-01",
                "content-type":      "application/json",
            },
            json={
                "model":      "claude-sonnet-4-6",
                "max_tokens": 4000,
                "messages": [{
                    "role":    "user",
                    "content": (
                        f"Schreibe einen ausführlichen deutschen SEO-Artikel zum Thema: '{keyword}'.\n\n"
                        f"Gliederung:\n{outline}\n\n"
                        f"Anforderungen:\n"
                        f"- Mindestens 1200 Wörter\n"
                        f"- SEO-optimiert, Keyword natürlich eingebaut\n"
                        f"- Verwende das Jahr {datetime.now().year}\n"
                        f"- Praxisnahe Tipps für Heimarbeiter in Deutschland\n"
                        f"- Beantworte häufige Fragen der Leser\n"
                        f"- Füge 3-5 Amazon.de Produktlinks ein:\n"
                        f"  <a href='https://www.amazon.de/s?k=SUCHBEGRIFF&tag={AMAZON_TRACKING_ID}' "
                        f"rel='nofollow' target='_blank'>Produktname auf Amazon ansehen</a>\n"
                        f"- Ersetze SUCHBEGRIFF mit passendem deutschen Begriff{competitor_section}\n"
                        f"- Professioneller aber freundlicher Ton\n"
                        f"- Reines HTML mit H1/H2/H3 Tags, Absätzen, Listen wo sinnvoll\n"
                        f"- KEIN Markdown, KEINE Code-Blöcke, KEIN ```html"
                    )
                }]
            },
            timeout=120
        )
        data = r.json()
        if "content" in data:
            return {"content": data["content"][0]["text"], "success": True}
        return {"error": data.get("error", {}).get("message", "Unknown"), "success": False}
    except Exception as e:
        return {"error": str(e), "success": False}


def publish_to_wordpress(title, content):
    try:
        r = requests.post(
            f"{WP_URL}/wp-json/wp/v2/posts",
            auth=(WP_USER, WP_APP_PASSWORD),
            json={"title": title, "content": content, "status": "publish", "categories": [4]},
            timeout=30
        )
        if r.status_code == 201:
            return {"success": True, "link": r.json().get("link", "")}
        return {"success": False, "status": r.status_code, "body": r.text[:300]}
    except Exception as e:
        return {"success": False, "error": str(e)}


def request_google_indexing(url):
    try:
        r = requests.post(
            "https://indexing.googleapis.com/v3/urlNotifications:publish",
            headers={"Content-Type": "application/json"},
            json={"url": url, "type": "URL_UPDATED"},
            timeout=10
        )
        return {"status": r.status_code}
    except Exception as e:
        return {"error": str(e)}


# ── Tool dispatcher ────────────────────────────────────────────────────────────

def dispatch_tool(name, inputs):
    try:
        dispatch_map = {
            "get_published_articles":    lambda: get_published_articles(),
            "search_google_trends":      lambda: search_google_trends(**inputs),
            "search_google":             lambda: search_google(**inputs),
            "get_related_searches":      lambda: get_related_searches(**inputs),
            "send_telegram_message":     lambda: send_telegram_message(**inputs),
            "wait_for_telegram_approval": lambda: wait_for_telegram_approval(**inputs),
            "fetch_unsplash_image":      lambda: fetch_unsplash_image(**inputs),
            "write_outline":             lambda: write_outline(**inputs),
            "write_article":             lambda: write_article(**inputs),
            "publish_to_wordpress":      lambda: publish_to_wordpress(**inputs),
            "request_google_indexing":   lambda: request_google_indexing(**inputs),
        }
        if name in dispatch_map:
            return dispatch_map[name]()
        return {"error": f"Unknown tool: {name}"}
    except Exception as e:
        return {"error": f"Tool {name} failed: {str(e)}. Try a different approach."}


# ── System prompt ──────────────────────────────────────────────────────────────

SYSTEM_PROMPT = f"""You are an autonomous SEO affiliate agent for heimbuero-test.de,
a German home office product review website. Your goal each run: research current 
trends, find the best keyword opportunity, and publish ONE high-quality German SEO 
article with Amazon affiliate links.

Today's date: {datetime.now().strftime('%d.%m.%Y')}
Site niche: Home office products for German workers (Bürostühle, Schreibtische, 
Monitore, Webcams, Headsets, Lampen, Drucker, Zubehör)

YOUR WORKFLOW:

Phase 1 — Research (use multiple tools):
1. Call get_published_articles to see what is already live
2. Call search_google_trends with "Homeoffice" to find rising topics in Germany
3. Call search_google_trends with "Büro Zubehör" to find more opportunities  
4. Call get_related_searches for the most promising trend you found
5. Call search_google on the best candidate keyword to analyse competition
6. Based on all research, decide the single best keyword to target:
   - High search intent (people want to buy)
   - Not already published on our site
   - Trending upward or seasonal opportunity
   - Competition is weak (thin content, old articles, no expert sites)

Phase 2 — Approval:
7. Call write_outline for your chosen keyword (include competitor_insights)
8. Call send_telegram_message with:
   - Your chosen keyword and WHY you chose it (trend data, competition gap)
   - The outline
   - Ask for YES or NO
9. Call wait_for_telegram_approval

Phase 3 — Create and publish (only if approved):
10. Call fetch_unsplash_image
11. Call write_article (include competitor_insights so article beats competition)
12. Call publish_to_wordpress with title and content where content = image_html + article_html combined as one string. Both are required
13. Call request_google_indexing
14. Call send_telegram_message with success message, URL, and why this article 
    should rank well

If NO: send_telegram_message confirming skip, stop.

IMPORTANT RULES:
- Never publish without approval
- Always explain your keyword choice with real data from your research
- Pick keywords with buying intent: "Test", "Vergleich", "bester", "kaufen", "unter X Euro"
- Avoid keywords already published (check slugs and titles)
- Think like an SEO expert: find gaps competitors haven't filled well"""


# ── Main agentic loop ──────────────────────────────────────────────────────────

def run_agent():
    print(f"[{datetime.now()}] Heimbuero Agent starting — trend research mode...")

    messages = [{
        "role":    "user",
        "content": (
            f"Run your full research and publishing workflow. "
            f"Today is {datetime.now().strftime('%d.%m.%Y %H:%M')}. "
            f"Start by checking published articles, then research current trends "
            f"to find the best keyword opportunity."
        )
    }]

    for iteration in range(MAX_ITERATIONS):
        print(f"\n[Iteration {iteration + 1}/{MAX_ITERATIONS}]")

        response = requests.post(
            "https://api.anthropic.com/v1/messages",
            headers={
                "x-api-key":         ANTHROPIC_API_KEY,
                "anthropic-version": "2023-06-01",
                "content-type":      "application/json",
            },
            json={
                "model":      "claude-sonnet-4-6",
                "max_tokens": 2000,
                "system":     SYSTEM_PROMPT,
                "tools":      TOOLS,
                "messages":   messages
            },
            timeout=60
        ).json()

        if "error" in response:
            msg = response["error"].get("message", "Unknown error")
            print(f"API error: {msg}")
            send_telegram_message(f"<b>Agent error:</b> {msg}")
            break

        stop_reason = response.get("stop_reason")
        content     = response.get("content", [])
        messages.append({"role": "assistant", "content": content})

        for block in content:
            if block.get("type") == "text" and block["text"].strip():
                print(f"[Agent] {block['text'][:400]}")

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