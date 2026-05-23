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

# Content style: always "research" — agent is a researcher, never a tester
CONTENT_STYLE = "research"

MAX_ITERATIONS = 24

# ── Categories and rotation ────────────────────────────────────────────────────
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
        self.products         = []       # list of product names to compare
        self.product_specs    = {}       # dict: product name → spec summary
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
            "Returns a structured summary of what reviewers and users say about it. "
            "Sources: Amazon reviews, manufacturer data, third-party test reports."
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
                "category":         {"type": "string", "description": "The product category e.g. Bürostuhl"},
                "products":         {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "List of 2-5 product names to compare"
                },
                "comparison_title": {"type": "string", "description": "German article title ≤70 characters e.g. 'Aeron vs Leap: Welcher Bürostuhl lohnt 2026?'"},
                "reason":           {"type": "string", "description": "Why this comparison is a good opportunity (search volume, competition gap, trending)"}
            },
            "required": ["category", "products", "comparison_title", "reason"]
        }
    },
    {
        "name": "write_comparison_outline",
        "description": "Generates a structured outline for the research-based comparison article using stored state.",
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
            "Writes the full research-based comparison article using stored state (products, specs, outline). "
            "Includes comparison table, pros/cons per product, verdict, and affiliate links. "
            "All claims sourced from user reviews and manufacturer data — no fake test claims. "
            "Stores article HTML in state."
        ),
        "input_schema": {"type": "object", "properties": {}, "required": []}
    },
    {
        "name": "validate_article",
        "description": (
            "Scans the article HTML for forbidden fake-test phrases before publishing. "
            "Returns pass: true/false and a list of violations if any found. "
            "Must be called after write_comparison_article and before publish_article."
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

        # Ask Claude to summarise specs from real search results
        summary_prompt = (
            f"Du bist ein Produktrechercheur. Fasse folgende Suchergebnisse über "
            f"'{product_name}' als {category} zusammen.\n\n"
            f"Gib zurück:\n"
            f"- Preis (ca., laut Marktdaten)\n"
            f"- 3 Vorteile (laut Nutzerbewertungen und Testberichten)\n"
            f"- 3 Nachteile (laut Nutzerbewertungen und Testberichten)\n"
            f"- Für wen geeignet\n\n"
            f"Suchergebnisse:\n" + "\n".join(snippets) +
            f"\nHäufige Fragen: {paa}\n\n"
            f"WICHTIG: Formuliere immer mit Quellenangabe: 'laut Bewertungen', "
            f"'laut Hersteller', 'Nutzer berichten', 'laut Produktdaten'.\n"
            f"Erfinde KEINE Messwerte oder Testergebnisse.\n"
            f"Antworte auf Deutsch, kurz und strukturiert."
        )

        cr = requests.post(
            "https://api.anthropic.com/v1/messages",
            headers={"x-api-key": ANTHROPIC_API_KEY,
                     "anthropic-version": "2023-06-01",
                     "content-type": "application/json"},
            json={
                "model": "claude-sonnet-4-20250514",
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
                "model": "claude-sonnet-4-20250514",
                "max_tokens": 600,
                "messages": [{
                    "role": "user",
                    "content": (
                        f"Erstelle eine Gliederung für einen RECHERCHE-BASIERTEN deutschen Vergleichsartikel.\n"
                        f"Dies ist ein Kaufratgeber — KEIN persönlicher Testbericht.\n\n"
                        f"Titel: '{STATE.comparison_title}'\n"
                        f"Produkte: {products_str}\n\n"
                        f"Produktinfos aus Recherche:\n{specs_str}\n\n"
                        f"Die Gliederung soll enthalten:\n"
                        f"1. Schnellübersicht (Vergleichstabelle)\n"
                        f"2. Je ein Abschnitt pro Produkt (Stärken laut Bewertungen, Schwächen laut Bewertungen, für wen?)\n"
                        f"3. Direkter Vergleich nach Kategorien (Preis, Komfort laut Nutzern, Qualität laut Bewertungen)\n"
                        f"4. Kaufempfehlung — wer sollte was kaufen (basierend auf Nutzerfeedback)\n"
                        f"5. FAQ – Häufige Fragen zum Kauf\n\n"
                        f"ERLAUBT: 'laut Bewertungen', 'Nutzer berichten', 'Kaufempfehlung', 'für wen geeignet'\n"
                        f"VERBOTEN: 'Testurteil', 'unser Test', 'Praxistest', 'haben wir getestet', 'Testteam'\n\n"
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
                "model": "claude-sonnet-4-20250514",
                "max_tokens": 8000,
                "messages": [{
                    "role": "user",
                    "content": (
                        f"Du bist ein Produktrechercheur für heimbuero-test.de. "
                        f"Du hast die Produkte NICHT persönlich besessen, getestet oder ausprobiert.\n"
                        f"Deine Quellen sind ausschließlich: Amazon.de Nutzerbewertungen, "
                        f"Hersteller-Spezifikationen, Fachmedien und Testberichte Dritter "
                        f"(z.B. CHIP, Computer Bild, Stiftung Warentest).\n\n"

                        f"Schreibe einen ausführlichen deutschen Vergleichsartikel.\n\n"
                        f"Titel: {STATE.comparison_title}\n"
                        f"Produkte: {products_str}\n\n"
                        f"Produktinfos aus Recherche:\n{specs_str}\n\n"
                        f"Gliederung:\n{STATE.outline}\n\n"

                        f"PFLICHT-ANFORDERUNGEN:\n"
                        f"- Mindestens 1500 Wörter\n"
                        f"- Beginne mit einer HTML-Vergleichstabelle: Produkt | Preis (laut Markt) | Beste für | Nutzerwertung\n"
                        f"- Je ein H2-Abschnitt pro Produkt mit:\n"
                        f"  ✅ Stärken (laut Nutzerbewertungen)\n"
                        f"  ❌ Schwächen (laut Nutzerbewertungen)\n"
                        f"- Direkter H2-Vergleich nach: Preis, Komfort laut Nutzern, Qualität laut Bewertungen, Garantie laut Hersteller\n"
                        f"- Klare Kaufempfehlung: wer sollte welches Produkt kaufen (basierend auf Nutzerfeedback)\n"
                        f"- Verwende das Jahr {datetime.now().year} — niemals frühere Jahre\n\n"

                        f"PFLICHT-SCHREIBSTIL — immer diese Quellenformulierungen verwenden:\n"
                        f"- 'Laut Amazon-Bewertungen...'\n"
                        f"- 'Der Hersteller gibt an...'\n"
                        f"- 'Käufer berichten...'\n"
                        f"- 'Laut Produktdaten...'\n"
                        f"- 'Laut Nutzerbewertungen...'\n"
                        f"- 'Basierend auf Kundenfeedback...'\n"
                        f"- 'Experten empfehlen...'\n"
                        f"- 'In der Praxis berichten Nutzer...'\n"
                        f"- 'Unsere Empfehlung basiert auf...'\n\n"

                        f"ABSOLUT VERBOTEN — diese Phrasen niemals verwenden:\n"
                        f"- 'haben wir getestet' / 'in unserem Test' / 'haben wir gemessen'\n"
                        f"- 'im Praxistest' / 'unser Testsieger' / 'haben wir ausprobiert'\n"
                        f"- 'unter realen Bedingungen getestet' / 'ausführlich getestet'\n"
                        f"- 'auf Basis unserer Tests' / 'konnten wir feststellen'\n"
                        f"- 'Testteam' / 'Testperson' / 'Testzeitraum'\n"
                        f"- Erfundene Messwerte (Temperaturen, dB-Werte, exakte Akkulaufzeiten als eigene Messung)\n"
                        f"- Sterne-Ratings oder Punktesysteme als eigene Bewertung\n"
                        f"- Testszenarien die nie stattfanden\n\n"

                        f"AFFILIATE LINKS — für jedes Produkt einen Amazon-Link:\n"
                        f"{amazon_links}\n"
                        f"Format: <a href='LINK' rel='nofollow' target='_blank'>PRODUKTNAME auf Amazon ansehen</a>\n\n"
                        f"OTTO Links — 1-2 einbauen wo passend:\n"
                        f"Allgemein: <a href='https://tidd.ly/4usYoRq' rel='nofollow' target='_blank'>"
                        f"Passende Produkte bei OTTO ansehen</a>\n"
                        f"Büro: <a href='https://tidd.ly/4wW7IPw' rel='nofollow' target='_blank'>"
                        f"Bürobedarf bei OTTO Office ansehen</a>\n\n"

                        f"FORMAT:\n"
                        f"- Professioneller, vertrauenswürdiger Ton\n"
                        f"- Reines HTML mit H1/H2/H3, Tabellen, Listen\n"
                        f"- KEIN Markdown, KEINE Code-Blöcke, KEIN ```html\n"
                        f"- Kein Affiliate-Disclaimer am Ende\n"
                    )
                }]
            },
            timeout=180
        )
        data = r.json()
        if "content" in data:
            STATE.article_html = data["content"][0]["text"]
            word_count = len(STATE.article_html.split())
            # Generate meta description with Haiku
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
                                f"Titel: {STATE.comparison_title}\n"
                                f"Produkte: {', '.join(STATE.products)}\n"
                                f"Enthält das Hauptkeyword, beschreibt den Mehrwert als Kaufratgeber. "
                                f"Nur die Beschreibung, kein Anführungszeichen, kein 'Test' oder 'getestet'."
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
    Hard mechanical check for forbidden fake-test phrases.
    Does not rely on the LLM to police itself — runs deterministically.
    """
    forbidden_phrases = [
        # Fake testing claims
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
        # Fake measurement data
        "grad celsius",
        "db(a)",
        "haben wir ermittelt",
        "konnten wir messen",
        "unser test zeigt",
        "im test festgestellt",
        "haben wir überprüft",
        # Fake scoring systems
        "von 100 punkten",
        "punkte vergeben",
        "testpunkte",
    ]

    article_lower = STATE.article_html.lower()
    violations = [phrase for phrase in forbidden_phrases if phrase in article_lower]

    if violations:
        print(f"[Validate] FAILED — {len(violations)} violation(s): {violations}")
        return {
            "pass": False,
            "violations": violations,
            "violation_count": len(violations),
            "action": "Article blocked. Call write_comparison_article again — the forbidden phrases above must not appear."
        }

    word_count = len(STATE.article_html.split())
    if word_count < 800:
        return {
            "pass": False,
            "violations": ["article_too_short"],
            "action": f"Article only {word_count} words. Minimum is 800. Call write_comparison_article again."
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
        return {"success": False, "error": "No content — write_comparison_article must be called first"}

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
a German home office product buying guide website. You compare products from well known
companies which manufacture home office products.

Today: {datetime.now().strftime('%d.%m.%Y')}
Content style: {CONTENT_STYLE} — you are a RESEARCHER, never a tester.

━━━ IDENTITY ━━━
You produce research-based buying guides and product comparisons.
You have NEVER touched, owned, or tested any product. You aggregate information from:
- Amazon.de user reviews
- Manufacturer specifications
- Third-party test reports (CHIP, Computer Bild, Stiftung Warentest, etc.)
- Expert opinions from established tech media

Every product claim must include a source hedge:
"laut Bewertungen", "laut Hersteller", "Nutzer berichten", "laut Produktdaten"

Never invent: temperatures, decibel values, battery runtimes measured by you,
test scores, test team personas, or test scenarios that never happened.
If you don't have a real sourced number, don't include it.

━━━ WORKFLOW — follow this exact order ━━━

PHASE 1 — RESEARCH:
1. get_published_articles — check what's already published, avoid duplicates
2. Pick a category not recently covered from the CATEGORIES list
3. search_google for "beste [category] 2026 Vergleich" to find popular products
4. search_google for "[product A] vs [product B]" to check competition gap
5. fetch_product_specs for each product to compare (2-5 products max)
6. set_comparison — commit to your choice with title, products list, and reason

PHASE 2 — APPROVAL:
7. write_comparison_outline — generates structured outline
8. send_approval_request — sends to owner, waits for YES/NO

PHASE 3 — PUBLISH (only if approved=true):
9.  fetch_image — gets category header image
10. write_comparison_article — writes full research-based comparison
11. validate_article — checks for forbidden fake-test phrases
    - If pass=false: call write_comparison_article again, then validate_article again
    - Only proceed when validate_article returns pass=true
12. publish_article — combines image + article and publishes
13. send_telegram_message — notify owner with success and URL

If approved=false: send_telegram_message confirming skip, then stop.

━━━ COMPARISON ARTICLE RULES ━━━
- Always compare 2-5 real, well-known products with genuine search demand in Germany
- Title format: "Produkt A vs Produkt B: Welcher [Kategorie] lohnt sich {datetime.now().year}?"
  or "Top 3 [Kategorie]: [Brand] vs [Brand] vs [Brand] im Vergleich {datetime.now().year}"
- Keep titles ≤70 characters — primary keyword first
- Use "Vergleich" or "Kaufratgeber" in titles — avoid "Test" or "Testbericht"
- Must include a comparison table and verdict based on user reviews
- Affiliate links for EVERY product compared
- Never pass article content between tools — publish_article reads from state directly
- Never publish without approval

━━━ IMPORTANT ━━━
- validate_article is mandatory before publish_article — never skip it
- If validate_article fails twice, send_telegram_message to owner explaining the issue and stop
- fetch_product_specs must be called for EACH product before write_comparison_article"""


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
            f"Remember: you are a researcher, not a tester. "
            f"validate_article is mandatory before publishing."
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
                "model":      "claude-sonnet-4-20250514",
                "max_tokens": 8000,
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
