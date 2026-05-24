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

# ── Agent state ────────────────────────────────────────────────────────────────
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
                "keyword":              {"type": "string"},
                "reason":               {"type": "string"},
                "competitor_insights":  {"type": "string", "description": "Specific gaps in competitor content we should fill."}
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
            "Waits for YES or NO reply. Returns approved: true/false."
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
            "Writes the full SEO buying guide for the chosen keyword following the outline. "
            "Stores article HTML in state. Returns word count and success status."
        ),
        "input_schema": {"type": "object", "properties": {}, "required": []}
    },
    {
        "name": "validate_article",
        "description": (
            "Scans the article HTML for forbidden phrases before publishing. "
            "Returns pass: true/false and a list of violations if any found. "
            "Must be called after write_article and before publish_article."
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
    return {"success": True, "keyword": keyword}


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
                        f"Erstelle eine Gliederung für einen Kaufratgeber.\n"
                        f"Thema: '{STATE.chosen_keyword}'\n"
                        f"Zielgruppe: Heimarbeiter in Deutschland die ein Produkt kaufen möchten.\n"
                        f"Lücken die Konkurrenten vernachlässigen: {STATE.competitor_insights}\n\n"
                        f"Format: Genau 5 H2-Abschnitte. Jeder Abschnitt soll einem Käufer "
                        f"helfen eine bessere Entscheidung zu treffen.\n\n"
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
        f"<b>Competitor gaps we'll cover:</b>\n{STATE.competitor_insights or '—'}\n\n"
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
            .replace("Kaufratgeber", "") \
            .replace("Kaufberatung", "") \
            .replace("Vergleich", "") \
            .replace("bester", "best") \
            .replace("Bestes", "best") \
            .replace("Bester", "best") \
            .replace("Test", "") \
            .strip()

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
                f'<img src="{img_url}" alt="{STATE.chosen_keyword} – Homeoffice Kaufratgeber {datetime.now().year}" '
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
    """
    Writes a buying guide that sounds like a knowledgeable friend — specific,
    opinionated, structured — without fake sourcing hedges or fake test claims.

    Key changes from original:
    - Removed the mandatory "laut Amazon-Bewertungen / Käufer berichten" style list.
      Those phrases were the primary HCU signal in every generated article.
    - Instead: specific model names, real prices, concrete criteria, honest trade-offs.
    - Added a required "Redaktionelle Einschätzung" section — one concrete recommendation
      with a reason. This is what separates useful content from aggregated noise.
    - Banned both fake-test AND fake-sourcing phrases in the same prompt.
    """
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
                        f"Schreibe einen deutschen SEO-Kaufratgeber zum Thema: "
                        f"'{STATE.chosen_keyword}'\n\n"

                        f"Gliederung:\n{STATE.chosen_outline}\n\n"

                        f"Lücken gegenüber Konkurrenten die du abdecken sollst:\n"
                        f"{STATE.competitor_insights}\n\n"

                        f"=== ZIELGRUPPE ===\n"
                        f"Heimarbeiter in Deutschland, die ein Produkt kaufen wollen "
                        f"und eine klare, ehrliche Empfehlung suchen.\n\n"

                        f"=== WAS GUTEN INHALT AUSMACHT ===\n"
                        f"Schreibe wie ein erfahrener Freund der sich in der Kategorie auskennt:\n"
                        f"- Nenne konkrete Modellnamen und Preisbereiche "
                        f"  (z.B. 'Der FlexiSpot E7 kostet rund 400 Euro und bietet...')\n"
                        f"- Nenne einen echten Vorteil UND einen echten Nachteil pro Produkt\n"
                        f"- Sage klar für wen ein Produkt geeignet ist und für wen nicht\n"
                        f"- Beantworte die Fragen die Käufer wirklich haben "
                        f"  (Garantie, Aufbau, Rücksendung, typische Probleme)\n"
                        f"- Gib eine konkrete Empfehlung — nicht 'das kommt drauf an'\n"
                        f"- Verwende das Jahr {datetime.now().year}\n"
                        f"- Mindestens 1200 Wörter\n\n"

                        f"=== PFLICHTABSCHNITT ===\n"
                        f"Füge einen Abschnitt 'Unsere Einschätzung' ein, der:\n"
                        f"- Ein konkretes Modell als Hauptempfehlung nennt (mit Begründung)\n"
                        f"- Ein Budget-Alternative nennt\n"
                        f"- Einen Satz dazu schreibt, für wen sich keines der Produkte lohnt\n\n"

                        f"=== HUMOR ===\n"
                        f"Bau genau DREI humorvolle Momente ein — verteilt über den Artikel, "
                        f"nicht alle auf einmal. Stil: trocken, selbstironisch, alltagsnah. "
                        f"Beispiele für den richtigen Ton:\n"
                        f"- Intro: 'Der Rücken beschwert sich seit 2020. Er hat recht.'\n"
                        f"- Mitte: 'Kurbeltische klingen gut im Prospekt — und werden dann nie "
                        f"  benutzt. Wie das Laufband im Keller.'\n"
                        f"- Ende: kurzes trockenes Understatement, "
                        f"  z.B. 'Zahlt sich aus. Irgendwann.'\n"
                        f"Kein Klamauk, kein Witz mit Pointe — ein Augenzwinkern das den "
                        f"Lesefluss auflockert ohne den informativen Ton zu brechen.\n\n"

                        f"=== ABSOLUT VERBOTEN ===\n"
                        f"Diese Phrasen machen den Artikel wertlos — niemals verwenden:\n\n"
                        f"Fake-Quellen (klingen nach KI, bieten keinen Mehrwert):\n"
                        f"'laut Amazon-Bewertungen', 'laut Bewertungen', "
                        f"'basierend auf Kundenfeedback', 'Käufer berichten', "
                        f"'Nutzer berichten', 'Experten empfehlen', 'laut Produktdaten', "
                        f"'In der Praxis berichten Nutzer', 'Herstellerangaben zufolge', "
                        f"'Unsere Empfehlung basiert auf'\n\n"
                        f"Fake-Tests (nie stattgefunden):\n"
                        f"'haben wir getestet', 'in unserem Test', 'haben wir gemessen', "
                        f"'im Praxistest', 'unser Testsieger', 'haben wir ausprobiert', "
                        f"'unter realen Bedingungen', 'ausführlich getestet', "
                        f"'Testteam', 'Testperson', 'Testzeitraum'\n\n"

                        f"=== AFFILIATE LINKS ===\n"
                        f"3-4 Amazon.de Links einbauen:\n"
                        f"<a href='https://www.amazon.de/s?k=SUCHBEGRIFF&tag={AMAZON_TRACKING_ID}' "
                        f"rel='nofollow' target='_blank'>Produktname auf Amazon ansehen</a>\n"
                        f"SUCHBEGRIFF = passender deutscher Suchbegriff.\n\n"
                        f"1-2 OTTO Links:\n"
                        f"<a href='https://tidd.ly/4usYoRq' rel='nofollow' target='_blank'>"
                        f"Passende Produkte bei OTTO ansehen</a>\n"
                        f"<a href='https://tidd.ly/4wW7IPw' rel='nofollow' target='_blank'>"
                        f"Bürobedarf bei OTTO Office ansehen</a>\n\n"

                        f"=== FORMAT ===\n"
                        f"- Reines HTML: H1, H2, H3, p, ul, li\n"
                        f"- KEIN Markdown, KEINE Code-Blöcke, KEIN ```html\n"
                        f"- Kein Disclaimer am Ende\n"
                        f"- H1 = Keyword + {datetime.now().year}\n"
                    )
                }]
            },
            timeout=120
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
                        "model": "claude-sonnet-4-6",
                        "max_tokens": 80,
                        "messages": [{
                            "role": "user",
                            "content": (
                                f"Schreibe eine Meta-Beschreibung auf Deutsch (120-155 Zeichen) für:\n"
                                f"Keyword: {STATE.chosen_keyword}\n"
                                f"Enthält das Keyword, beschreibt den Mehrwert als Kaufratgeber. "
                                f"Nur die Beschreibung, kein Anführungszeichen."
                            )
                        }]
                    },
                    timeout=15
                )
                STATE.meta_description = meta_r.json().get("content", [{}])[0].get("text", "").strip()[:155]
            except Exception:
                STATE.meta_description = (
                    f"{STATE.chosen_keyword} – Kaufratgeber und Empfehlungen "
                    f"für das Homeoffice {datetime.now().year}."
                )
            return {"success": True, "word_count": word_count}
        return {"error": data.get("error", {}).get("message", "Unknown"), "success": False}
    except Exception as e:
        return {"error": str(e), "success": False}


def validate_article():
    """
    Checks for both categories of forbidden phrases:
    1. Fake-test claims (never stattgefunden)
    2. AI-sourcing hedges (the HCU signal the original validator missed entirely)
    """
    # Fake testing claims
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
        "haben wir ermittelt",
        "konnten wir messen",
        "unser test zeigt",
        "im test festgestellt",
        "haben wir überprüft",
        "von 100 punkten",
        "punkte vergeben",
        "testpunkte",
    ]

    # AI-sourcing hedges — the primary HCU signal; these were *required* in the old prompt
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
                "Article blocked. Call write_article again. "
                "AI-hedging phrases like 'laut Amazon-Bewertungen' and 'Käufer berichten' "
                "are forbidden alongside fake-test phrases. "
                "Replace them with specific model names, concrete facts, and direct opinions."
            )
        }

    word_count = len(STATE.article_html.split())
    if word_count < 800:
        return {
            "pass":   False,
            "violations": ["article_too_short"],
            "action": f"Article only {word_count} words. Minimum is 800. Call write_article again."
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
        return {"success": False, "error": "No content — call write_article first"}

    schema = {
        "@context": "https://schema.org",
        "@type": "Article",
        "headline": STATE.chosen_keyword,
        "description": STATE.meta_description or STATE.chosen_keyword,
        "datePublished": datetime.now().strftime("%Y-%m-%d"),
        "publisher": {
            "@type": "Organization",
            "name": "HeimBüro Test",
            "url": WP_URL or "https://heimbuero-test.de"
        }
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
        if name == "get_published_articles":  return get_published_articles()
        if name == "search_google_trends":    return search_google_trends(**inputs)
        if name == "search_google":           return search_google(**inputs)
        if name == "get_related_searches":    return get_related_searches(**inputs)
        if name == "set_chosen_keyword":      return set_chosen_keyword(**inputs)
        if name == "write_outline":           return write_outline()
        if name == "send_approval_request":   return send_approval_request()
        if name == "fetch_image":             return fetch_image()
        if name == "write_article":           return write_article()
        if name == "validate_article":        return validate_article()
        if name == "publish_article":         return publish_article()
        if name == "send_telegram_message":   return send_telegram_message(**inputs)
        return {"error": f"Unknown tool: {name}"}
    except Exception as e:
        return {"error": f"{name} failed: {str(e)}"}


# ── System prompt ──────────────────────────────────────────────────────────────

def _system_prompt():
    return f"""You are an autonomous SEO affiliate agent for heimbuero-test.de,
a German home office product buying guide website.

Today: {datetime.now().strftime('%d.%m.%Y')}
Niche: Home office products for German workers.

━━━ CONTENT STANDARD ━━━
Write like a knowledgeable friend who knows the category well.
Name specific products. Give concrete recommendations. Admit trade-offs honestly.
Do NOT hide behind vague attribution language.

The following phrases are banned from all content — they are the primary signal
Google uses to identify low-quality AI content:
"laut Amazon-Bewertungen", "Käufer berichten", "Nutzer berichten",
"basierend auf Kundenfeedback", "Experten empfehlen", "laut Produktdaten"

Also banned: any fake-test language ("haben wir getestet", "Testsieger", etc.)

━━━ WORKFLOW ━━━

PHASE 1 — RESEARCH:
1. get_published_articles — avoid duplicate topics
2. search_google_trends("Homeoffice") — find rising topics
3. search_google_trends("Büro Zubehör") — find more opportunities
4. get_related_searches for the most promising trend
5. search_google on the best candidate to analyse competitors
6. set_chosen_keyword — commit with reason and specific competitor_insights

PHASE 2 — APPROVAL:
7. write_outline
8. send_approval_request — wait for YES/NO

PHASE 3 — PUBLISH (only if approved=true):
9.  fetch_image
10. write_article
11. validate_article
    - If pass=false: call write_article again, then validate_article again
    - Only proceed when pass=true
    - BOTH fake-test phrases AND ai-hedge phrases will cause failure
12. publish_article
13. send_telegram_message with the live URL

If approved=false: send_telegram_message confirming skip, stop.

━━━ RULES ━━━
- Complete Phase 1 fully before choosing a keyword
- Never skip set_chosen_keyword
- validate_article is mandatory — never skip it
- If validate_article fails twice: send_telegram_message explaining the issue and stop
- Keyword (article title) ≤ 60 characters, primary keyword first
- Prefer 'Kaufratgeber' or 'Vergleich' over 'Test' in titles"""


# ── Main loop ──────────────────────────────────────────────────────────────────

def run_agent():
    print(f"[{datetime.now()}] Agent starting...")
    system_prompt = _system_prompt()

    messages = [{
        "role":    "user",
        "content": (
            f"Run your full workflow. Today is {datetime.now().strftime('%d.%m.%Y %H:%M')}. "
            f"Start with research phase. "
            f"validate_article is mandatory before publishing — "
            f"it now checks for AI-hedging phrases as well as fake-test phrases."
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
                "max_tokens": 1000,
                "system": [{"type": "text", "text": system_prompt, "cache_control": {"type": "ephemeral"}}],
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