import os
import json
import time
import requests
from datetime import datetime

# # ── Environment variables ──────────────────────────────────────────────────────
ANTHROPIC_API_KEY   = os.environ.get("ANTHROPIC_API_KEY")
TELEGRAM_TOKEN      = os.environ.get("TELEGRAM_TOKEN")
TELEGRAM_CHAT_ID    = os.environ.get("TELEGRAM_CHAT_ID")
WP_URL              = os.environ.get("WP_URL")
WP_USER             = os.environ.get("WP_USER")
WP_APP_PASSWORD     = os.environ.get("WP_APP_PASSWORD")
AMAZON_TRACKING_ID  = os.environ.get("AMAZON_TRACKING_ID")
UNSPLASH_ACCESS_KEY = os.environ.get("UNSPLASH_ACCESS_KEY")
SERPAPI_KEY         = os.environ.get("SERPAPI_KEY")

MAX_ITERATIONS = 24

# # ── Categories and rotation ────────────────────────────────────────────────────
CATEGORIES = [
    "Bürostuhl",
    "Monitor",
    "Schreibtisch",
    "Tastatur",
    "Maus",
    "Headset",
    "Drucker",
    "Laptop Ständer",
    "Laptop"
]

# # ── Agent state ────────────────────────────────────────────────────────────────
class ComparisonState:
    def __init__(self):
        self.category        = ""
        self.products        = []       # list of product names to compare
        self.product_specs   = {}       # dict: product name → spec summary
        self.comparison_title = ""
        self.outline         = ""
        self.image_html      = ""
        self.article_html    = ""
        self.reason          = ""

STATE = ComparisonState()

# # ── Tool definitions ───────────────────────────────────────────────────────────
TOOLS = [
    {
        "name": "get_published_articles",
        "description": "Fetches all published article titles and slugs from WordPress to avoid duplicates.",
        "input_schema": {"type": "object", "properties": {}, "required": []}
    },
    {
        "name": "search_google",
        "description": "Searches Google.de and returns top results, People Also Ask, and related searches.",
        "input_schema": {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string", 
                    "description": "Search query for Google.de"
                    }
            },
            "required": ["query"]
        }
    },
    {
        "name": "fetch_product_specs",
        "description": (
            "Searches Google for real specs, price, pros and cons of a specific product. "
            "Returns a structured summary of what reviewers say about it."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "product_name": {"type": "string", "description": "Full product name e.g. 'Herman Miller Aeron'"},
                "category": {"type": "string", "description": "Product category e.g. 'Bürostuhl'"}
            },
            "required": ["product_name", "category"]
        }
    },
    {
        "name": "set_comparison",
        "description": (
            "Call this once you have decided which products to compare. "
            "Stores category, product list, title and reason in agent state."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "category":          {"type": "string", "description": "The product category e.g. Bürostuhl"},
                "products":          {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "List of 2-5 product names to compare"
                },
                "comparison_title":  {"type": "string", "description": "German article title e.g. 'Herman Miller Aeron vs Steelcase Leap: Welcher Bürostuhl lohnt sich 2026?'"},
                "reason":            {"type": "string", "description": "Why this comparison is a good opportunity (search volume, competition gap, trending)"}
            },
            "required": ["category", "products", "comparison_title", "reason"]
        }
    },
    {
        "name": "write_comparison_outline",
        "description": "Generates a structured outline for the comparison article using stored state.",
        "input_schema": {"type": "object", "properties": {}, "required": []}
    },
    {
        "name": "send_approval_request",
        "description": (
            "Sends the comparison details and outline to the owner on Telegram for approval. "
            "Waits for YES or NO. Returns approved: true/false."
        ),
        "input_schema": {"type": "object", "properties": {}, "required": []}
    },
    {
        "name": "fetch_image",
        "description": "Fetches a relevant header image from Unsplash for the category. Stores in state.",
        "input_schema": {"type": "object", "properties": {}, "required": []}
    },
    {
        "name": "write_comparison_article",
        "description": (
            "Writes the full comparison article using stored state (products, specs, outline). "
            "Includes comparison table, pros/cons, verdict, and affiliate links. "
            "Stores article HTML in state."
        ),
        "input_schema": {"type": "object", "properties": {}, "required": []}
    },
    {
        "name": "publish_article",
        "description": "Combines image + article from state and publishes to WordPress. Returns live URL.",
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

# # ── Tool implementations ───────────────────────────────────────────────────────

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


def fetch_product_specs(product_name, category):
    try:
        query = f"{product_name} {category} Test Erfahrungen Vor- Nachteile Preis 2026"
        r = requests.get("https://serpapi.com/search", params={
            "engine": "google", "q": query,
            "location": "Germany", "hl": "de", "gl": "de",
            "num": "8", "api_key": SERPAPI_KEY
        }, timeout=20)
        data = r.json()

        snippets = [
            x.get("snippet", "")
            for x in data.get("organic_results", [])[:5]
            if x.get("snippet")
        ]
        paa = [q.get("question") for q in data.get("related_questions", [])[:4]]

        # Ask Claude to summarise the specs from search results
        summary_prompt = (
            f"Fasse folgende Suchergebnisse über '{product_name}' als {category} zusammen.\n"
            f"Gib zurück: Preis (ca.), 3 Vorteile, 3 Nachteile, für wen geeignet.\n"
            f"Suchergebnisse:\n" + "\n".join(snippets) +
            f"\nHäufige Fragen: {paa}\n\n"
            f"Antworte auf Deutsch, kurz und strukturiert."
        )

        cr = requests.post(
            "https://api.anthropic.com/v1/messages",
            headers={"x-api-key": ANTHROPIC_API_KEY,
                     "anthropic-version": "2023-06-01",
                     "content-type": "application/json"},
            json={
                "model": "claude-sonnet-4-6",
                "max_tokens": 400,
                "messages": [{"role": "user", "content": summary_prompt}]
            },
            timeout=30
        )
        cd = cr.json()
        if "content" in cd:
            specs = cd["content"][0]["text"]
            STATE.product_specs[product_name] = specs
            return {"product": product_name, "specs": specs, "success": True}
        return {"error": "Claude API error", "success": False}
    except Exception as e:
        return {"error": str(e), "success": False}


def set_comparison(category, products, comparison_title, reason):
    STATE.category         = category
    STATE.products         = products
    STATE.comparison_title = comparison_title
    STATE.reason           = reason
    print(f"[State] Comparison set: {comparison_title}")
    return {
        "success": True,
        "category": category,
        "products": products,
        "title": comparison_title
    }


def write_comparison_outline():
    try:
        products_str = " vs ".join(STATE.products)
        specs_str = "\n\n".join([
            f"{p}:\n{STATE.product_specs.get(p, 'Specs noch nicht geladen')}"
            for p in STATE.products
        ])

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
                        f"Erstelle eine Gliederung für einen deutschen Vergleichsartikel:\n"
                        f"Titel: '{STATE.comparison_title}'\n"
                        f"Produkte: {products_str}\n\n"
                        f"Produktinfos:\n{specs_str}\n\n"
                        f"Die Gliederung soll enthalten:\n"
                        f"1. Schnellübersicht (Vergleichstabelle)\n"
                        f"2. Je ein Abschnitt pro Produkt (Stärken, Schwächen, für wen?)\n"
                        f"3. Direkter Vergleich nach Kategorien (Preis, Komfort, Qualität)\n"
                        f"4. Unser Testurteil — wer sollte was kaufen\n"
                        f"5. Fazit mit Kaufempfehlung\n\n"
                        f"Gib nur die Gliederungspunkte aus, kein weiterer Text."
                    )
                }]
            },
            timeout=30
        )
        data = r.json()
        if "content" in data:
            STATE.outline = data["content"][0]["text"]
            return {"outline": STATE.outline, "success": True}
        return {"error": "API error", "success": False}
    except Exception as e:
        return {"error": str(e), "success": False}


def send_approval_request():
    products_str = " vs ".join(STATE.products)
    message = (
        f"<b>Vergleichsartikel — Freigabe erforderlich</b>\n\n"
        f"<b>Kategorie:</b> {STATE.category}\n"
        f"<b>Produkte:</b> {products_str}\n"
        f"<b>Titel:</b> {STATE.comparison_title}\n\n"
        f"<b>Warum dieser Vergleich:</b>\n{STATE.reason}\n\n"
        f"<b>Gliederung:</b>\n{STATE.outline}\n\n"
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
        english_map = {
            "Bürostuhl": "ergonomic office chair",
            "Monitor": "computer monitor desk setup",
            "Schreibtisch": "standing desk home office",
            "Tastatur": "mechanical keyboard desk",
            "Maus": "computer mouse desk",
            "Headset": "wireless headset home office",
            "Drucker": "office printer",
            "Laptop Ständer": "laptop stand desk setup",
            "Laptop": "office laptop"
            
        }
        query = english_map.get(STATE.category, f"{STATE.category} home office")
        print(f"[Image] Searching Unsplash for: {query}")

        r = requests.get(
            "https://api.unsplash.com/search/photos",
            params={"query": query, "per_page": 1, "orientation": "landscape"},
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
                f'<img src="{img_url}" alt="{STATE.comparison_title}" '
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


def write_comparison_article():
    try:
        products_str = " vs ".join(STATE.products)
        specs_str = "\n\n".join([
            f"### {p}\n{STATE.product_specs.get(p, 'Keine Specs verfügbar')}"
            for p in STATE.products
        ])
        amazon_links = "\n".join([
            f"- {p}: https://www.amazon.de/s?k={p.replace(' ', '+')}&tag={AMAZON_TRACKING_ID}"
            for p in STATE.products
        ])

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
                        f"Schreibe einen ausführlichen deutschen Vergleichsartikel.\n\n"
                        f"Titel: {STATE.comparison_title}\n"
                        f"Produkte: {products_str}\n\n"
                        f"Produktinfos aus Recherche:\n{specs_str}\n\n"
                        f"Gliederung:\n{STATE.outline}\n\n"
                        f"Anforderungen:\n"
                        f"- Mindestens 1500 Wörter\n"
                        f"- Beginne mit einer HTML-Vergleichstabelle mit Spalten: Produkt, Preis, Beste für, Bewertung\n"
                        f"- Je ein H2-Abschnitt pro Produkt mit Stärken (✅) und Schwächen (❌)\n"
                        f"- Direkter H2-Vergleich nach: Preis, Komfort, Qualität, Garantie\n"
                        f"- Klares Testurteil: wer sollte welches Produkt kaufen\n"
                        f"- Verwende das Jahr {datetime.now().year}\n"
                        f"- Füge für jedes Produkt einen Amazon-Affiliate-Link ein:\n"
                        f"{amazon_links}\n"
                        f"  Format: <a href='LINK' rel='nofollow' target='_blank'>PRODUKTNAME auf Amazon ansehen</a>\n"
                        f"- Füge außerdem 1-2 OTTO Links ein wo es passt:\n"
                        f"  Für allgemeine Produkte: <a href='https://tidd.ly/4usYoRq' rel='nofollow' target='_blank'>Passende Produkte bei OTTO ansehen</a>\n"
                        f"  Für Büroausstattung: <a href='https://tidd.ly/4wW7IPw' rel='nofollow' target='_blank'>Bürobedarf bei OTTO Office ansehen</a>\n"
                        f"- Professioneller, vertrauenswürdiger Ton\n"
                        f"- Reines HTML mit H1/H2/H3, Tabellen, Listen\n"
                        f"- KEIN Markdown, KEINE Code-Blöcke, KEIN ```html\n"
                        f"- Kein Affiliate-Disclaimer am Ende"
                    )                
                }]
            },
            timeout=180
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
    full_content = STATE.image_html + STATE.article_html
    if not full_content.strip():
        return {"success": False, "error": "No content — write_comparison_article must be called first"}
    try:
        r = requests.post(
            f"{WP_URL}/wp-json/wp/v2/posts",
            auth=(WP_USER, WP_APP_PASSWORD),
            json={
                "title":      STATE.comparison_title,
                "content":    full_content,
                "status":     "publish",
                "categories": [4]
            },
            timeout=30
        )
        if r.status_code == 201:
            link = r.json().get("link", "")
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


# # ── Tool dispatcher ────────────────────────────────────────────────────────────

def dispatch_tool(name, inputs):
    try:
        if name == "get_published_articles":     return get_published_articles()
        if name == "search_google":              return search_google(**inputs)
        if name == "fetch_product_specs":        return fetch_product_specs(**inputs)
        if name == "set_comparison":             return set_comparison(**inputs)
        if name == "write_comparison_outline":   return write_comparison_outline()
        if name == "send_approval_request":      return send_approval_request()
        if name == "fetch_image":                return fetch_image()
        if name == "write_comparison_article":   return write_comparison_article()
        if name == "publish_article":            return publish_article()
        if name == "send_telegram_message":      return send_telegram_message(**inputs)
        return {"error": f"Unknown tool: {name}"}
    except Exception as e:
        return {"error": f"{name} failed: {str(e)}"}


# ── System prompt ──────────────────────────────────────────────────────────────

SYSTEM_PROMPT = f"""You are an autonomous comparison article agent for heimbuero-test.de,
a German home office product review website. You compare products from well known companies
which manufacture home office products. 

Today: {datetime.now().strftime('%d.%m.%Y')}
Your goal: research real products, pick the best comparison opportunity, 
and publish ONE high-quality German comparison article with affiliate links for ALL products.

Available categories to rotate through:
{json.dumps(CATEGORIES, ensure_ascii=False)}

YOUR EXACT WORKFLOW:

PHASE 1 — RESEARCH:
1. get_published_articles — check what's already published on heimbuero-test.de(avoid duplicates)
2. Pick a category not recently covered from the already published articles
3. search_google for "beste [category] 2026 Vergleich" to find popular products
4. search_google for "[product A] vs [product B]" to check competition
5. fetch_product_specs for each product you want to compare (2-5 products max)
6. set_comparison — commit to your choice with title, products list, and reason

PHASE 2 — APPROVAL:
7. write_comparison_outline — generates structured outline
8. send_approval_request — sends to owner, waits for YES/NO

PHASE 3 — PUBLISH (only if approved):
9. fetch_image — gets category header image
10. write_comparison_article — writes full article using all stored specs
11. publish_article — combines image + article and publishes
12. send_telegram_message — notify owner with success and URL

If approved=false: send_telegram_message confirming skip, then stop.

COMPARISON ARTICLE RULES:
- Always compare 2-5 real, well-known products
- Pick products with genuine search demand in Germany
- Title format: "Produkt A vs Produkt B: Welcher [Kategorie] lohnt sich [Jahr]?"
  or "Top 3 [Kategorie]: [Brand] vs [Brand] vs [Brand] im Test [Jahr]"
- Must include a comparison table and verdict
- Affiliate links for EVERY product compared
- Never pass article content between tools — publish_article reads from state directly
- Never publish without approval"""


# ── Main loop ──────────────────────────────────────────────────────────────────

def run_agent():
    print(f"[{datetime.now()}] Comparison Agent starting...")
    messages = [{
        "role": "user",
        "content": (
            f"Publish article for today"
            f"Today is {datetime.now().strftime('%d.%m.%Y %H:%M')}. "
            f"Start by checking published articles, then research and pick the best comparison."
        )
    }]

    for iteration in range(MAX_ITERATIONS):
        response = requests.post(
            "https://api.anthropic.com/v1/messages",
                        headers={"x-api-key": ANTHROPIC_API_KEY,
                     "anthropic-version": "2023-06-01",
                     "content-type": "application/json"},
            json={
                "model":      "claude-sonnet-4-6",
                "max_tokens": 8000,
                "system":     SYSTEM_PROMPT,
                "tools":      TOOLS,
                "messages":   messages
            },
            timeout=60
        ).json()


        if "error" in response:
            msg = response["error"].get("message", "Unknown")
            print(f"API error: {msg}")
            send_telegram_message(f"<b>Comparison Agent error:</b> {msg}")
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

    print(f"\n[{datetime.now()}] Comparison Agent finished.")


if __name__ == "__main__":
    run_agent()