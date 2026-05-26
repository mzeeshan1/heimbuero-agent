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

MAX_ITERATIONS = 24

# ── Categories ─────────────────────────────────────────────────────────────────
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

# ── Agent state ────────────────────────────────────────────────────────────────
class ComparisonState:
    def __init__(self):
        self.category         = ""
        self.products         = []
        self.product_specs    = {}   # product name → raw spec summary (clean, no hedges)
        self.comparison_title = ""
        self.outline          = ""
        self.image_html       = ""
        self.article_html     = ""
        self.reason           = ""
        self.meta_description = ""

STATE = ComparisonState()

# ── Tool definitions ───────────────────────────────────────────────────────────
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
                "query": {"type": "string", "description": "Search query for Google.de"}
            },
            "required": ["query"]
        }
    },
    {
        "name": "fetch_product_specs",
        "description": (
            "Searches Google for real specs, price, pros and cons of a specific product. "
            "Returns a clean structured summary of facts from reviews and manufacturer data. "
            "Call this for EACH product before write_comparison_article."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "product_name": {"type": "string", "description": "Full product name e.g. 'Herman Miller Aeron'"},
                "category":     {"type": "string", "description": "Product category e.g. 'Bürostuhl'"}
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
                "category":         {"type": "string"},
                "products":         {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "List of 2-5 product names to compare"
                },
                "comparison_title": {"type": "string", "description": "German article title ≤70 chars"},
                "reason":           {"type": "string"}
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
            "Sends comparison details and outline to the owner on Telegram for approval. "
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
            "Writes the full comparison article using stored state. "
            "Stores article HTML in state. Returns word count."
        ),
        "input_schema": {"type": "object", "properties": {}, "required": []}
    },
    {
        "name": "validate_article",
        "description": (
            "Scans the article HTML for forbidden phrases — both fake-test claims AND "
            "AI-sourcing hedges. Returns pass: true/false. "
            "Mandatory before publish_article."
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
            "properties": {"message": {"type": "string"}},
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
    """
    Fetches real search snippets about the product, then asks Claude to distil
    them into clean factual bullet points — WITHOUT sourcing hedges.

    The hedges were the problem in the original: specs containing "laut Bewertungen"
    were fed into write_comparison_article, which then reproduced them verbatim.
    Now specs are stored as clean facts; the article prompt decides how to frame them.
    """
    try:
        query = f"{product_name} {category} Erfahrungen Bewertungen Vor- Nachteile Preis 2026"
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

        summary_prompt = (
            f"Fasse diese Suchergebnisse über '{product_name}' ({category}) zusammen.\n\n"
            f"Gib zurück:\n"
            f"- Preis (ca., laut Marktdaten — konkrete Zahl wenn vorhanden)\n"
            f"- 3 Vorteile (konkrete Fakten, keine vagen Aussagen)\n"
            f"- 3 Nachteile (konkrete Fakten, keine vagen Aussagen)\n"
            f"- Für wen geeignet (ein konkreter Satz)\n\n"
            f"Suchergebnisse:\n" + "\n".join(snippets) +
            f"\nHäufige Fragen: {paa}\n\n"
            f"Schreibe DIREKTE FAKTEN — keine Quellenfloskeln wie "
            f"'laut Bewertungen', 'Nutzer berichten', 'laut Produktdaten'.\n"
            f"Erfinde KEINE Messwerte. Wenn ein Wert unbekannt ist, lass ihn weg.\n"
            f"Antworte auf Deutsch, kurz und strukturiert."
        )

        cr = requests.post(
            "https://api.anthropic.com/v1/messages",
            headers={"x-api-key": ANTHROPIC_API_KEY,
                     "anthropic-version": "2023-06-01",
                     "content-type": "application/json"},
            json={
                "model":      "claude-sonnet-4-6",
                "max_tokens": 400,
                "messages":   [{"role": "user", "content": summary_prompt}]
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
    return {"success": True, "category": category, "products": products, "title": comparison_title}


def write_comparison_outline():
    try:
        products_str = " vs ".join(STATE.products)
        specs_str = "\n\n".join([
            f"### {p}\n{STATE.product_specs.get(p, 'Specs noch nicht geladen')}"
            for p in STATE.products
        ])

        r = requests.post(
            "https://api.anthropic.com/v1/messages",
            headers={"x-api-key": ANTHROPIC_API_KEY,
                     "anthropic-version": "2023-06-01",
                     "content-type": "application/json"},
            json={
                "model":      "claude-sonnet-4-6",
                "max_tokens": 600,
                "messages": [{
                    "role": "user",
                    "content": (
                        f"Erstelle eine Gliederung für einen deutschen Vergleichsartikel.\n\n"
                        f"Titel: '{STATE.comparison_title}'\n"
                        f"Produkte: {products_str}\n\n"
                        f"Produktinfos:\n{specs_str}\n\n"
                        f"Die Gliederung soll enthalten:\n"
                        f"1. Schnellübersicht (Vergleichstabelle)\n"
                        f"2. Je ein Abschnitt pro Produkt (Stärken, Schwächen, für wen?)\n"
                        f"3. Direkter Vergleich nach Kategorien (Preis, Ergonomie, Qualität, Garantie)\n"
                        f"4. Klare Kaufempfehlung — wer sollte was kaufen\n"
                        f"5. FAQ\n\n"
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
        english_map = {
            "Bürostuhl":     "ergonomic office chair",
            "Monitor":       "computer monitor desk setup",
            "Schreibtisch":  "standing desk home office",
            "Tastatur":      "mechanical keyboard desk",
            "Maus":          "computer mouse desk",
            "Headset":       "wireless headset home office",
            "Drucker":       "office printer",
            "Laptop Ständer":"laptop stand desk setup",
            "Laptop":        "office laptop"
        }
        query = english_map.get(STATE.category, f"{STATE.category} home office")

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
    """
    Key fixes vs original:
    - Removed the mandatory AI-hedge phrase list ("PFLICHT-SCHREIBSTIL").
    - Instead: write like a knowledgeable friend — specific, direct, opinionated.
    - Required "Unser Fazit" section with a named winner and a named budget pick.
    - Both fake-test AND AI-hedge phrases are in the banned list.
    - specs fed in from fetch_product_specs are now clean facts, not pre-hedged text.
    """
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
                "model":      "claude-sonnet-4-6",
                "max_tokens": 8000,
                "messages": [{
                    "role": "user",
                    "content": (
                        f"Schreibe einen deutschen Vergleichsartikel.\n\n"
                        f"Titel: {STATE.comparison_title}\n"
                        f"Produkte: {products_str}\n\n"
                        f"Produktinfos aus Recherche:\n{specs_str}\n\n"
                        f"Gliederung:\n{STATE.outline}\n\n"

                        f"=== ZIELGRUPPE ===\n"
                        f"Heimarbeiter in Deutschland die eines dieser Produkte kaufen wollen "
                        f"und eine klare, direkte Empfehlung suchen.\n\n"

                        f"=== WAS GUTEN VERGLEICHS-INHALT AUSMACHT ===\n"
                        f"Schreibe wie ein erfahrener Freund der beide Produkte kennt:\n"
                        f"- Beginne mit einer HTML-Vergleichstabelle: "
                        f"  Produkt | Preis ca. | Beste für | Kurzbewertung\n"
                        f"- Pro Produkt: ein konkreter Hauptvorteil, ein konkreter Hauptnachteil\n"
                        f"- Sage klar für wen jedes Produkt geeignet ist — und für wen nicht\n"
                        f"- Vergleiche direkt nach Kategorien: Preis, Ergonomie, Verarbeitung, Garantie\n"
                        f"- Gib eine klare Kaufempfehlung — nicht 'das kommt drauf an'\n"
                        f"- Verwende das Jahr {datetime.now().year}\n"
                        f"- Mindestens 1500 Wörter\n\n"

                        f"=== PFLICHTABSCHNITT: 'Unser Fazit' ===\n"
                        f"Dieser Abschnitt muss enthalten:\n"
                        f"- Ein klarer Gewinner mit einem Satz Begründung\n"
                        f"- Eine Budget-Alternative (falls zutreffend)\n"
                        f"- Einen Satz für wen keines der Produkte passt\n\n"

                        f"=== HUMOR ===\n"
                        f"Bau genau DREI humorvolle Momente ein — verteilt über den Artikel, "
                        f"nicht alle auf einmal. Stil: trocken, selbstironisch, alltagsnah. "
                        f"Stilbeispiele (NICHT wörtlich übernehmen — nur als Tonbeispiel):\n"
                        f"- Intro-Ton: kurze, trockene Beobachtung aus dem Alltag\n"
                        f"- Mitten-Ton: ehrliche Alltagsbeobachtung, die zeigt dass Produkte "
                        f"  oft ungenutzt bleiben\n"
                        f"- Ende-Ton: kurzes Understatement das den Mehrwert bestätigt\n"
                        f"Erfinde eigene Formulierungen passend zum Thema '{STATE.comparison_title}'. "                        
                        f"Kein Satz aus diesem Prompt darf wörtlich im Artikel erscheinen.\n"
                        f"Kein Klamauk, kein Witz mit Pointe — ein Augenzwinkern das den "
                        f"Lesefluss auflockert ohne den informativen Ton zu brechen.\n\n"

                        f"=== ABSOLUT VERBOTEN ===\n"
                        f"Diese Phrasen sind verboten — sie machen den Artikel wertlos:\n\n"
                        f"Fake-Quellen (KI-Signal, kein Mehrwert für Leser):\n"
                        f"'laut Amazon-Bewertungen', 'laut Bewertungen', "
                        f"'basierend auf Kundenfeedback', 'Käufer berichten', "
                        f"'Nutzer berichten', 'Experten empfehlen', 'laut Produktdaten', "
                        f"'In der Praxis berichten Nutzer', 'Herstellerangaben zufolge', "
                        f"'Unsere Empfehlung basiert auf', 'laut Nutzerbewertungen'\n\n"
                        f"Fake-Tests (nie stattgefunden):\n"
                        f"'haben wir getestet', 'in unserem Test', 'haben wir gemessen', "
                        f"'im Praxistest', 'unser Testsieger', 'haben wir ausprobiert', "
                        f"'unter realen Bedingungen', 'ausführlich getestet', "
                        f"'Testteam', 'Testperson', 'Testzeitraum'\n\n"

                        f"=== AFFILIATE LINKS ===\n"
                        f"Für jedes Produkt einen Amazon-Link:\n"
                        f"{amazon_links}\n"
                        f"Format: <a href='LINK' rel='nofollow' target='_blank'>"
                        f"PRODUKTNAME auf Amazon ansehen</a>\n\n"
                        f"1-2 OTTO Links:\n"
                        f"<a href='https://tidd.ly/4usYoRq' rel='nofollow' target='_blank'>"
                        f"Passende Produkte bei OTTO ansehen</a>\n"
                        f"<a href='https://tidd.ly/4wW7IPw' rel='nofollow' target='_blank'>"
                        f"Bürobedarf bei OTTO Office ansehen</a>\n\n"

                        f"=== FORMAT ===\n"
                        f"- Reines HTML: H1, H2, H3, table, ul, li\n"
                        f"- KEIN Markdown, KEINE Code-Blöcke, KEIN ```html\n"
                        f"- Kein Disclaimer am Ende\n"
                        f"- H1 = Artikeltitel\n"
                    )
                }]
            },
            timeout=180
        )
        data = r.json()
        if "content" in data:
            STATE.article_html = data["content"][0]["text"]
            word_count = len(STATE.article_html.split())

            # Meta description
            try:
                meta_r = requests.post(
                    "https://api.anthropic.com/v1/messages",
                    headers={"x-api-key": ANTHROPIC_API_KEY,
                             "anthropic-version": "2023-06-01",
                             "content-type": "application/json"},
                    json={
                        "model":      "claude-sonnet-4-6",
                        "max_tokens": 80,
                        "messages": [{
                            "role": "user",
                            "content": (
                                f"Schreibe eine Meta-Beschreibung auf Deutsch (120-155 Zeichen) für:\n"
                                f"Titel: {STATE.comparison_title}\n"
                                f"Produkte: {', '.join(STATE.products)}\n"
                                f"Enthält das Hauptkeyword, beschreibt den Mehrwert als Vergleich. "
                                f"Nur die Beschreibung, kein Anführungszeichen."
                            )
                        }]
                    },
                    timeout=15
                )
                STATE.meta_description = meta_r.json().get("content", [{}])[0].get("text", "").strip()[:155]
            except Exception:
                STATE.meta_description = (
                    f"{STATE.comparison_title} – Detaillierter Vergleich mit Kaufempfehlung {datetime.now().year}."
                )
            return {"success": True, "word_count": word_count}
        return {"error": data.get("error", {}).get("message", "Unknown"), "success": False}
    except Exception as e:
        return {"error": str(e), "success": False}


def validate_article():
    """
    Checks for both forbidden categories:
    1. Fake-test claims
    2. AI-sourcing hedges (were *required* in the original — now blocked)

    This is a deterministic phrase check — does not rely on the LLM.
    """
    fake_test_phrases = [
        "haben wir getestet",
        "in unserem test",
        "haben wir gemessen",
        "im praxistest",
        "unser testsieger",
        "haben wir ausprobiert",
        "unter realen bedingungen",
        "ausführlich getestet",
        "auf basis unserer tests",
        "konnten wir feststellen",
        "haben wir beobachtet",
        "im testzeitraum",
        "testteam",
        "testperson",
        "testpersonen",
        "grad celsius",
        "db(a)",
        "haben wir ermittelt",
        "konnten wir messen",
        "unser test zeigt",
        "im test festgestellt",
        "haben wir überprüft",
        "von 100 punkten",
        "punkte vergeben",
        "testpunkte",
    ]

    ai_hedge_phrases = [
        "laut amazon-bewertungen",
        "laut bewertungen",
        "basierend auf kundenfeedback",
        "käufer berichten",
        "nutzer berichten",
        "experten empfehlen",
        "laut produktdaten",
        "in der praxis berichten nutzer",
        "herstellerangaben zufolge",
        "unsere empfehlung basiert auf",
        "laut nutzerbewertungen",
        "laut kundenbewertungen",
    ]

    article_lower = STATE.article_html.lower()
    fake_test_violations = [p for p in fake_test_phrases if p in article_lower]
    ai_hedge_violations  = [p for p in ai_hedge_phrases  if p in article_lower]
    all_violations       = fake_test_violations + ai_hedge_violations

    if all_violations:
        print(f"[Validate] FAILED — {len(all_violations)} violation(s): {all_violations}")
        return {
            "pass":            False,
            "fake_test":       fake_test_violations,
            "ai_hedges":       ai_hedge_violations,
            "violation_count": len(all_violations),
            "action": (
                "Article blocked. Call write_comparison_article again. "
                "Both fake-test phrases and AI-hedging phrases are forbidden. "
                "Replace them with specific model names, concrete prices, and direct opinions."
            )
        }

    word_count = len(STATE.article_html.split())
    if word_count < 800:
        return {
            "pass":       False,
            "violations": ["article_too_short"],
            "action":     f"Only {word_count} words. Minimum 800. Call write_comparison_article again."
        }

    print(f"[Validate] PASSED — {word_count} words, no forbidden phrases.")
    return {"pass": True, "word_count": word_count}


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
    if not STATE.article_html.strip():
        return {"success": False, "error": "No content — call write_comparison_article first"}

    schema = {
        "@context": "https://schema.org",
        "@type": "ItemList",
        "name": STATE.comparison_title,
        "description": STATE.meta_description or STATE.comparison_title,
        "itemListElement": [
            {"@type": "ListItem", "position": i + 1, "name": p}
            for i, p in enumerate(STATE.products)
        ]
    }
    schema_script = (
        f'\n<script type="application/ld+json">'
        f'{json.dumps(schema, ensure_ascii=False)}'
        f'</script>'
    )
    full_content = STATE.image_html + STATE.article_html + schema_script

    try:
        r = requests.post(
            f"{WP_URL}/wp-json/wp/v2/posts",
            auth=(WP_USER, WP_APP_PASSWORD),
            json={
                "title":      STATE.comparison_title,
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
        if name == "get_published_articles":    return get_published_articles()
        if name == "search_google":             return search_google(**inputs)
        if name == "fetch_product_specs":       return fetch_product_specs(**inputs)
        if name == "set_comparison":            return set_comparison(**inputs)
        if name == "write_comparison_outline":  return write_comparison_outline()
        if name == "send_approval_request":     return send_approval_request()
        if name == "fetch_image":               return fetch_image()
        if name == "write_comparison_article":  return write_comparison_article()
        if name == "validate_article":          return validate_article()
        if name == "publish_article":           return publish_article()
        if name == "send_telegram_message":     return send_telegram_message(**inputs)
        return {"error": f"Unknown tool: {name}"}
    except Exception as e:
        return {"error": f"{name} failed: {str(e)}"}


# ── System prompt ──────────────────────────────────────────────────────────────

def _system_prompt():
    return f"""You are an autonomous comparison article agent for heimbuero-test.de,
a German home office product buying guide website.

Today: {datetime.now().strftime('%d.%m.%Y')}

━━━ CONTENT STANDARD ━━━
Write like a knowledgeable friend who knows both products well.
Name specific models. Give concrete prices. Make a direct recommendation.
Do NOT hide behind vague attribution language.

The following phrases are banned from ALL content:
Fake-sourcing: "laut Amazon-Bewertungen", "Käufer berichten", "Nutzer berichten",
"basierend auf Kundenfeedback", "Experten empfehlen", "laut Produktdaten"
Fake-testing: "haben wir getestet", "Testsieger", "im Praxistest", "Testteam"

validate_article checks for BOTH categories and will block the article if found.

━━━ WORKFLOW ━━━

PHASE 1 — RESEARCH:
1. get_published_articles — avoid duplicate topics
2. Pick a category from the list not recently covered
3. search_google "beste [category] 2026 Vergleich" — find popular products
4. search_google "[product A] vs [product B]" — check competition gap
5. fetch_product_specs for EACH product (2-5 products max)
6. set_comparison — commit with title, products list, and reason

PHASE 2 — APPROVAL:
7. write_comparison_outline
8. send_approval_request — wait for YES/NO

PHASE 3 — PUBLISH (only if approved=true):
9.  fetch_image
10. write_comparison_article
11. validate_article
    - If pass=false: call write_comparison_article again, then validate_article again
    - Only proceed when pass=true
12. publish_article
13. send_telegram_message with the live URL

If approved=false: send_telegram_message confirming skip, stop.

━━━ RULES ━━━
- fetch_product_specs must be called for EACH product before write_comparison_article
- validate_article is mandatory — never skip it
- If validate_article fails twice: notify owner and stop
- Title ≤ 70 characters, primary keyword first
- Use "Vergleich" in titles — avoid "Test" or "Testbericht"
- Never publish without approval"""


# ── Main loop ──────────────────────────────────────────────────────────────────

def run_agent():
    print(f"[{datetime.now()}] Comparison Agent starting...")
    system_prompt = _system_prompt()

    messages = [{
        "role": "user",
        "content": (
            f"Publish one comparison article for today. "
            f"Today is {datetime.now().strftime('%d.%m.%Y %H:%M')}. "
            f"Start by checking published articles, then research and pick the best comparison. "
            f"validate_article is mandatory — it now blocks both fake-test phrases "
            f"AND AI-sourcing hedges like 'laut Amazon-Bewertungen'."
        )
    }]

    for iteration in range(MAX_ITERATIONS):
        print(f"\n[Iteration {iteration + 1}/{MAX_ITERATIONS}]")

        response = requests.post(
            "https://api.anthropic.com/v1/messages",
            headers={
                "x-api-key":         ANTHROPIC_API_KEY,
                "anthropic-version": "2023-06-01",
                "anthropic-beta":    "prompt-caching-2024-07-31",
                "content-type":      "application/json"
            },
            json={
                "model":      "claude-sonnet-4-6",
                "max_tokens": 1000,   # orchestration only — 1000 is sufficient
                "system":     [{"type": "text", "text": system_prompt, "cache_control": {"type": "ephemeral"}}],
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