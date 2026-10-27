import os
import re
import json
import time
import hmac
import hashlib
import ipaddress
import requests
import psycopg2
from urllib.parse import urlparse
from psycopg2.extras import RealDictCursor
from flask import (
    Flask, request, jsonify, render_template_string,
    session, redirect, url_for, abort,
)
from werkzeug.security import generate_password_hash, check_password_hash
from google import genai
from google.genai import types
from icalendar import Calendar
from datetime import datetime, date, timedelta

# ---------------------------------------------------------------------------
# Setup
# ---------------------------------------------------------------------------

app = Flask(__name__)

DATABASE_URL = os.environ.get("DATABASE_URL")
ADMIN_PASSWORD = os.environ.get("ADMIN_PASSWORD")
GEMINI_MODEL = os.environ.get("GEMINI_MODEL", "gemini-3.8-flash")
DEBUG_ERRORS = os.environ.get("DEBUG_ERRORS") == "1"

ai_client = genai.Client(api_key=os.environ.get("GEMINI_API_KEY"))

app.secret_key = os.environ.get("SECRET_KEY") or hashlib.sha256(
    ("bookaroom|" + (DATABASE_URL or "local")).encode()
).hexdigest()
app.config.update(
    SESSION_COOKIE_SECURE=True,
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE="Lax",
    PERMANENT_SESSION_LIFETIME=timedelta(days=14),
)

RESERVED_SLUGS = {
    "admin", "owner", "chat", "gate", "static", "favicon.ico",
    "robots.txt", "health", "api", "login", "logout",
}
SLUG_RE = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
URL_RE = re.compile(r"^https?://[^\s'\"()<>\\]{3,500}$")

# Columns added on top of the original table. The app adds them by itself.
NEW_COLUMNS = {
    "tagline": "TEXT DEFAULT ''",
    "location": "TEXT DEFAULT ''",
    "about": "TEXT DEFAULT ''",
    "hero_image_url": "TEXT DEFAULT ''",
    "phone": "TEXT DEFAULT ''",
    "whatsapp": "TEXT DEFAULT ''",
    "email": "TEXT DEFAULT ''",
    "rooms_json": "TEXT DEFAULT '[]'",
    "owner_password_hash": "TEXT",
}
_schema_ready = False


# ---------------------------------------------------------------------------
# Database helpers
# ---------------------------------------------------------------------------

def get_db_connection():
    return psycopg2.connect(DATABASE_URL, sslmode="require")


def ensure_schema_once():
    global _schema_ready
    if _schema_ready:
        return
    conn = get_db_connection()
    try:
        conn.autocommit = True
        cur = conn.cursor()
        for column, ddl in NEW_COLUMNS.items():
            cur.execute(f"ALTER TABLE guesthouses ADD COLUMN IF NOT EXISTS {column} {ddl}")
        cur.close()
        _schema_ready = True
    finally:
        conn.close()


def db_run(sql, params=(), fetch=None):
    ensure_schema_once()
    conn = get_db_connection()
    try:
        cur = conn.cursor(cursor_factory=RealDictCursor)
        cur.execute(sql, params)
        result = None
        if fetch == "one":
            result = cur.fetchone()
        elif fetch == "all":
            result = cur.fetchall()
        conn.commit()
        cur.close()
        return result
    finally:
        conn.close()


def fetch_guesthouse(slug):
    return db_run("SELECT * FROM guesthouses WHERE slug = %s", (slug,), fetch="one")


# ---------------------------------------------------------------------------
# Small helpers
# ---------------------------------------------------------------------------

def clip(value, limit):
    return (value or "").strip()[:limit]


def safe_url(value):
    value = (value or "").strip()
    return value if URL_RE.match(value) else ""


def parse_rooms(raw):
    """Turn stored/submitted room data into a clean list."""
    if isinstance(raw, str):
        try:
            raw = json.loads(raw or "[]")
        except ValueError:
            raw = []
    rooms = []
    if not isinstance(raw, list):
        return rooms
    for item in raw[:20]:
        if not isinstance(item, dict):
            continue
        name = clip(item.get("name"), 80)
        if not name:
            continue
        rooms.append({
            "name": name,
            "price": clip(item.get("price"), 40),
            "description": clip(item.get("description"), 600),
            "image": safe_url(item.get("image")),
        })
    return rooms


def whatsapp_link(value):
    digits = re.sub(r"\D", "", value or "")
    if not digits:
        return ""
    if digits.startswith("0"):
        digits = "27" + digits[1:]
    return "https://wa.me/" + digits


def phone_href(value):
    cleaned = re.sub(r"[^\d+]", "", value or "")
    return ("tel:" + cleaned) if cleaned else ""


def south_africa_today():
    # South Africa is UTC+2 all year (no daylight saving)
    return (datetime.utcnow() + timedelta(hours=2)).date()


def csrf_token():
    if "csrf" not in session:
        session["csrf"] = hashlib.sha256(os.urandom(32)).hexdigest()
    return session["csrf"]


def csrf_ok():
    token = session.get("csrf", "")
    sent = request.form.get("csrf", "")
    return bool(token) and hmac.compare_digest(token, sent)


def message_page(title, text, status=200):
    return render_template_string(MESSAGE_HTML, title=title, text=text, fonts=FONTS), status


# ---------------------------------------------------------------------------
# Calendar
# ---------------------------------------------------------------------------

def is_safe_ical_url(value):
    try:
        parsed = urlparse((value or "").strip())
    except ValueError:
        return False
    if parsed.scheme != "https" or not parsed.hostname:
        return False
    host = parsed.hostname.lower()
    if host == "localhost" or host.endswith(".local") or host.endswith(".internal"):
        return False
    try:
        ip = ipaddress.ip_address(host)
        if ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved:
            return False
    except ValueError:
        pass
    return True


def to_date(value):
    if isinstance(value, datetime):
        return value.date()
    return value


def load_bookings(ical_url):
    response = requests.get(ical_url.strip(), timeout=6, stream=True)
    if response.status_code != 200:
        raise ValueError(f"The calendar link did not open (status {response.status_code}).")
    data = b""
    for chunk in response.iter_content(65536):
        data += chunk
        if len(data) > 2000000:
            raise ValueError("The calendar is too large.")
    cal = Calendar.from_ical(data)
    today = south_africa_today()
    bookings = []
    for component in cal.walk():
        if component.name == "VEVENT":
            start_prop = component.get("dtstart")
            if start_prop is None:
                continue
            start = to_date(start_prop.dt)
            end_prop = component.get("dtend")
            end = to_date(end_prop.dt) if end_prop is not None else start + timedelta(days=1)
            if end > today:
                bookings.append((start, end))
    bookings.sort(key=lambda b: b[0])
    return bookings


def get_live_calendar_status(ical_url):
    if not ical_url or ical_url.strip() == "":
        return "No external calendar connected yet. All rooms assumed open."
    if not is_safe_ical_url(ical_url):
        return "Live availability calendar currently unavailable."
    try:
        bookings = load_bookings(ical_url)
        if not bookings:
            return "All rooms are open and available."
        return "\n".join(
            f"Booked from {s.strftime('%d %b %Y')} to {e.strftime('%d %b %Y')}"
            for s, e in bookings[:60]
        )
    except Exception:
        return "Live availability data offline."


# ---------------------------------------------------------------------------
# Shared page pieces
# ---------------------------------------------------------------------------

FONTS = (
    '<link rel="preconnect" href="https://fonts.googleapis.com">'
    '<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>'
    '<link href="https://fonts.googleapis.com/css2?family=Figtree:wght@400;500;600'
    '&family=Fraunces:opsz,wght@9..144,500;9..144,600&display=swap" rel="stylesheet">'
)

BASE_CSS = r"""
:root {
    --ink: #173A36;
    --ink-soft: #2B544F;
    --stone: #E8EDE9;
    --paper: #FFFFFF;
    --text: #1B2624;
    --muted: #5F6F6B;
    --line: #D5DDD8;
    --ochre: #C58B2B;
    --ochre-dark: #8A5E12;
    --danger: #A12B2B;
}
* { box-sizing: border-box; -webkit-tap-highlight-color: transparent; }
html { scroll-behavior: smooth; }
body {
    margin: 0;
    font-family: "Figtree", system-ui, -apple-system, "Segoe UI", Roboto, sans-serif;
    color: var(--text);
    line-height: 1.6;
    font-size: 17px;
    background: var(--paper);
}
h1, h2, h3 { font-family: "Fraunces", Georgia, serif; font-weight: 600; margin: 0; }
a { color: var(--ink); }
@media (prefers-reduced-motion: reduce) {
    html { scroll-behavior: auto; }
    * { animation: none !important; transition: none !important; }
}
"""

MESSAGE_HTML = r"""
<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{{ title }}</title>
{{ fonts|safe }}
<style>
/*BASE*/
body { background: var(--stone); display: flex; min-height: 100vh; align-items: center; justify-content: center; padding: 24px; }
.box { max-width: 460px; }
.box h1 { font-size: 32px; color: var(--ink); margin-bottom: 12px; }
.box p { margin: 0; color: var(--text); }
</style>
</head>
<body>
<div class="box">
    <h1>{{ title }}</h1>
    <p>{{ text }}</p>
</div>
</body>
</html>
""".replace("/*BASE*/", BASE_CSS)


# ---------------------------------------------------------------------------
# Guest mini site
# ---------------------------------------------------------------------------

GUEST_HTML = r"""
<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">
<meta name="theme-color" content="#173A36">
<title>{{ s.name }}{% if s.tagline %} | {{ s.tagline }}{% endif %}</title>
<meta name="description" content="{{ (s.about or s.tagline or ('Book your stay at ' ~ s.name))[:150] }}">
{{ fonts|safe }}
<style>
/*BASE*/
.wrap { max-width: 720px; margin: 0 auto; padding: 0 22px; }
.hero {
    background-color: var(--ink);
    background-size: cover;
    background-position: center;
    color: #fff;
    padding: 76px 0 60px;
}
.hero .where { margin: 0 0 14px; font-size: 15px; color: rgba(255,255,255,0.82); }
.hero h1 { font-size: clamp(38px, 10vw, 62px); line-height: 1.05; margin-bottom: 16px; }
.rule { width: 56px; height: 3px; background: var(--ochre); margin-bottom: 18px; }
.hero .tag { font-size: 19px; margin: 0 0 30px; max-width: 32ch; color: rgba(255,255,255,0.94); }
.cta { display: flex; gap: 12px; flex-wrap: wrap; }
.btn {
    font: inherit; font-weight: 600; font-size: 16px;
    border-radius: 999px; padding: 13px 22px;
    border: 2px solid transparent; cursor: pointer;
    text-decoration: none; display: inline-block;
}
.btn-primary { background: var(--ochre); color: #1B1405; }
.btn-ghost { border-color: rgba(255,255,255,0.75); color: #fff; background: transparent; }
section { padding: 52px 0; }
section h2 { font-size: 30px; color: var(--ink); margin-bottom: 18px; }
.about p { margin: 0; max-width: 60ch; white-space: pre-line; }
.rooms { background: var(--stone); }
.room { padding: 26px 0; border-top: 1px solid var(--line); }
.room:first-of-type { border-top: none; padding-top: 4px; }
.room img { width: 100%; height: 230px; object-fit: cover; border-radius: 10px; margin-bottom: 14px; display: block; }
.room-head { display: flex; justify-content: space-between; gap: 16px; align-items: baseline; }
.room h3 { font-size: 23px; color: var(--ink); }
.price { font-weight: 600; color: var(--ochre-dark); font-size: 16px; text-align: right; }
.room p { margin: 8px 0 12px; white-space: pre-line; }
.link {
    font: inherit; font-weight: 600; color: var(--ink);
    background: none; border: none; padding: 0 0 2px;
    border-bottom: 2px solid var(--ochre); cursor: pointer;
}
.steps { margin: 0; padding-left: 22px; max-width: 60ch; }
.steps li { margin-bottom: 10px; }
.contact-list { display: flex; flex-direction: column; gap: 10px; }
.contact-list p { margin: 0; }
.contact-list a { font-weight: 600; }
footer { padding: 28px 0 110px; text-align: center; font-size: 14px; color: var(--muted); background: var(--stone); }

/* Floating chat button */
#chat-open {
    position: fixed; right: 18px; bottom: calc(18px + env(safe-area-inset-bottom)); z-index: 20;
    font: inherit; font-weight: 600; font-size: 16px; color: #fff;
    background: var(--ink); border: none; border-radius: 999px;
    padding: 15px 24px; cursor: pointer;
    box-shadow: 0 8px 24px rgba(23,58,54,0.35);
}
#chat-open:focus-visible, .btn:focus-visible, .link:focus-visible { outline: 3px solid var(--ochre); outline-offset: 3px; }

/* Chat sheet */
#chat {
    position: fixed; top: 0; right: 0; bottom: 0; left: 0; z-index: 50;
    display: none; flex-direction: column; background: var(--stone);
}
#chat.open { display: flex; }
.chat-head {
    background: var(--ink); color: #fff; flex-shrink: 0;
    padding: calc(14px + env(safe-area-inset-top)) 18px 14px;
    display: flex; justify-content: space-between; align-items: center; gap: 12px;
}
.chat-head h3 { font-size: 20px; line-height: 1.2; }
.status { display: flex; align-items: center; gap: 8px; font-size: 13px; color: rgba(255,255,255,0.8); margin-top: 4px; }
.dot { width: 8px; height: 8px; border-radius: 50%; background: var(--ochre); flex-shrink: 0; }
#chat-close {
    font: inherit; font-weight: 600; font-size: 14px; color: #fff;
    background: transparent; border: 1.5px solid rgba(255,255,255,0.6);
    border-radius: 999px; padding: 8px 16px; cursor: pointer; flex-shrink: 0;
}
#messages {
    flex: 1; overflow-y: auto; padding: 18px 16px 8px;
    display: flex; flex-direction: column; gap: 10px;
    -webkit-overflow-scrolling: touch;
}
.msg {
    max-width: 88%; padding: 11px 15px; font-size: 16px; line-height: 1.5;
    word-wrap: break-word; overflow-wrap: anywhere; animation: rise 0.22s ease-out;
}
.msg p { margin: 0 0 8px; }
.msg p:last-child, .msg ul:last-child, .msg ol:last-child { margin-bottom: 0; }
.msg ul, .msg ol { margin: 0 0 8px; padding-left: 20px; }
.msg li { margin-bottom: 4px; }
.msg strong { font-weight: 600; }
.bot { align-self: flex-start; background: var(--paper); border: 1px solid var(--line); border-radius: 4px 18px 18px 18px; }
.user { align-self: flex-end; background: var(--ink); color: #fff; border-radius: 18px 4px 18px 18px; white-space: pre-wrap; }
.typing { display: flex; gap: 5px; padding: 15px 16px; }
.typing span { width: 7px; height: 7px; border-radius: 50%; background: var(--muted); animation: pulse 1.1s infinite ease-in-out; }
.typing span:nth-child(2) { animation-delay: 0.15s; }
.typing span:nth-child(3) { animation-delay: 0.3s; }
#chips { display: flex; flex-wrap: wrap; gap: 8px; padding: 4px 16px 12px; }
.chip {
    font: inherit; font-size: 14px; font-weight: 500; color: var(--ink);
    background: transparent; border: 1.5px solid var(--ink);
    border-radius: 999px; padding: 8px 14px; cursor: pointer;
}
.chip:active { background: var(--ink); color: #fff; }
#input-area {
    display: flex; gap: 10px; flex-shrink: 0;
    padding: 12px 14px calc(12px + env(safe-area-inset-bottom));
    background: var(--paper); border-top: 1px solid var(--line);
}
#user-input {
    flex: 1; min-width: 0; font: inherit; font-size: 16px; color: var(--text);
    padding: 12px 16px; border: 1.5px solid var(--line); border-radius: 999px;
    outline: none; background: var(--stone);
}
#user-input:focus { border-color: var(--ink); background: #fff; }
#send {
    font: inherit; font-size: 15px; font-weight: 600; color: #fff;
    background: var(--ink); border: none; border-radius: 999px; padding: 0 22px; cursor: pointer;
}
#send:disabled { opacity: 0.45; cursor: default; }
@keyframes rise {
    from { opacity: 0; transform: translateY(8px); }
    to { opacity: 1; transform: translateY(0); }
}
@keyframes pulse {
    0%, 80%, 100% { opacity: 0.3; transform: scale(0.85); }
    40% { opacity: 1; transform: scale(1); }
}
@media (max-width: 699px) {
    body.lock { overflow: hidden; }
}
@media (min-width: 700px) {
    #chat {
        top: auto; left: auto; right: 24px; bottom: 24px;
        width: 420px; height: min(680px, calc(100vh - 48px));
        border-radius: 18px; overflow: hidden;
        box-shadow: 0 18px 60px rgba(23,58,54,0.3);
    }
}
</style>
</head>
<body>

<header class="hero"{% if s.hero %} style="background-image: linear-gradient(rgba(14,38,35,0.55), rgba(14,38,35,0.82)), url('{{ s.hero }}');"{% endif %}>
    <div class="wrap">
        {% if s.location %}<p class="where">{{ s.location }}</p>{% endif %}
        <h1>{{ s.name }}</h1>
        <div class="rule"></div>
        {% if s.tagline %}<p class="tag">{{ s.tagline }}</p>{% endif %}
        <div class="cta">
            <button class="btn btn-primary" data-chat="I would like to check availability and book a room.">Check availability</button>
            {% if s.rooms %}<a class="btn btn-ghost" href="#rooms">See rooms</a>{% endif %}
        </div>
    </div>
</header>

<section class="about">
    <div class="wrap">
        <h2>Welcome</h2>
        <p>{{ s.about or ('Welcome to ' ~ s.name ~ '. Chat with our assistant any time of day to check rates and open dates, and to book your stay.') }}</p>
    </div>
</section>

{% if s.rooms %}
<section class="rooms" id="rooms">
    <div class="wrap">
        <h2>Our rooms</h2>
        {% for r in s.rooms %}
        <article class="room">
            {% if r.image %}<img src="{{ r.image }}" alt="{{ r.name }}" loading="lazy">{% endif %}
            <div class="room-head">
                <h3>{{ r.name }}</h3>
                {% if r.price %}<span class="price">{{ r.price }}</span>{% endif %}
            </div>
            {% if r.description %}<p>{{ r.description }}</p>{% endif %}
            <button class="link" data-chat="Tell me more about the {{ r.name }} and whether it is available.">Ask about this room</button>
        </article>
        {% endfor %}
    </div>
</section>
{% endif %}

<section>
    <div class="wrap">
        <h2>How booking works</h2>
        <ol class="steps">
            <li>Chat with us and tell us your dates.</li>
            <li>We check the live calendar and confirm what is open.</li>
            <li>You pay the guesthouse directly. The payment details appear in the chat when you are ready to book.</li>
            <li>Send your proof of payment to confirm your stay.</li>
        </ol>
    </div>
</section>

{% if s.phone or s.whatsapp_link or s.email %}
<section class="rooms">
    <div class="wrap">
        <h2>Get in touch</h2>
        <div class="contact-list">
            {% if s.phone %}<p>Call: <a href="{{ s.phone_href }}">{{ s.phone }}</a></p>{% endif %}
            {% if s.whatsapp_link %}<p>WhatsApp: <a href="{{ s.whatsapp_link }}" rel="noopener">Message us</a></p>{% endif %}
            {% if s.email %}<p>Email: <a href="mailto:{{ s.email }}">{{ s.email }}</a></p>{% endif %}
        </div>
    </div>
</section>
{% endif %}

<footer>Booking assistant by BookaRoom</footer>

<button id="chat-open" data-open="1">Chat with us</button>

<div id="chat" role="dialog" aria-label="Chat with {{ s.name }}">
    <div class="chat-head">
        <div>
            <h3>{{ s.name }}</h3>
            <div class="status"><span class="dot"></span>Online now. Replies instantly.</div>
        </div>
        <button id="chat-close">Close</button>
    </div>
    <div id="messages">
        <div class="msg bot"><p>Hi, welcome to {{ s.name }}. How can I assist you today? Ask me about rooms, rates or open dates, or tell me when you would like to stay.</p></div>
    </div>
    <div id="chips">
        <button class="chip" data-q="What are your room rates?">Room rates</button>
        <button class="chip" data-q="Which dates are available?">Open dates</button>
        <button class="chip" data-q="How do I book a room?">How to book</button>
        {% if s.rooms %}<button class="chip" data-go="rooms">View rooms</button>{% endif %}
    </div>
    <div id="input-area">
        <input type="text" id="user-input" placeholder="Type your message" autocomplete="off" enterkeyhint="send">
        <button id="send">Send</button>
    </div>
</div>

<script>
    const slug = {{ s.slug|tojson }};
    const chatEl = document.getElementById('chat');
    const openBtn = document.getElementById('chat-open');
    const messagesEl = document.getElementById('messages');
    const inputEl = document.getElementById('user-input');
    const sendBtn = document.getElementById('send');
    const chipsEl = document.getElementById('chips');
    let history = [];
    let busy = false;

    function openChat() {
        chatEl.classList.add('open');
        document.body.classList.add('lock');
        openBtn.style.display = 'none';
        scrollDown();
    }
    function closeChat() {
        chatEl.classList.remove('open');
        document.body.classList.remove('lock');
        openBtn.style.display = '';
    }
    function esc(s) {
        return s.replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;');
    }
    function inline(s) {
        return esc(s).replace(/\*\*(.+?)\*\*/g, '<strong>$1</strong>');
    }
    function render(text) {
        const lines = text.replace(/\r/g, '').split('\n');
        let html = '';
        let list = null;
        const close = () => { if (list) { html += '</' + list + '>'; list = null; } };
        for (const raw of lines) {
            const line = raw.trim();
            let m;
            if ((m = line.match(/^[*\-\u2022]\s+(.*)$/))) {
                if (list !== 'ul') { close(); html += '<ul>'; list = 'ul'; }
                html += '<li>' + inline(m[1]) + '</li>';
            } else if ((m = line.match(/^\d+[.)]\s+(.*)$/))) {
                if (list !== 'ol') { close(); html += '<ol>'; list = 'ol'; }
                html += '<li>' + inline(m[1]) + '</li>';
            } else if (line === '') {
                close();
            } else if ((m = line.match(/^#{1,6}\s+(.*)$/))) {
                close();
                html += '<p><strong>' + inline(m[1]) + '</strong></p>';
            } else {
                close();
                html += '<p>' + inline(line) + '</p>';
            }
        }
        close();
        return html;
    }
    function scrollDown() {
        messagesEl.scrollTo({ top: messagesEl.scrollHeight, behavior: 'smooth' });
    }
    function addMessage(text, sender) {
        const div = document.createElement('div');
        div.className = 'msg ' + sender;
        if (sender === 'bot') { div.innerHTML = render(text); } else { div.textContent = text; }
        messagesEl.appendChild(div);
        scrollDown();
    }
    function showTyping() {
        const div = document.createElement('div');
        div.className = 'msg bot typing';
        div.innerHTML = '<span></span><span></span><span></span>';
        messagesEl.appendChild(div);
        scrollDown();
        return div;
    }
    async function sendMessage(text) {
        text = (text || '').trim();
        if (!text || busy) return;
        busy = true;
        sendBtn.disabled = true;
        chipsEl.style.display = 'none';
        addMessage(text, 'user');
        history.push({ role: 'user', text: text });
        inputEl.value = '';
        const typing = showTyping();
        try {
            const res = await fetch('/chat/' + slug, {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ messages: history })
            });
            const data = await res.json();
            typing.remove();
            const reply = data.reply || 'Sorry, something went wrong. Please try again.';
            addMessage(reply, 'bot');
            if (res.ok && !data.error) {
                history.push({ role: 'model', text: reply });
            } else {
                history.pop();
            }
        } catch (err) {
            typing.remove();
            history.pop();
            addMessage('Connection lost. Please check your signal and send your message again.', 'bot');
        }
        busy = false;
        sendBtn.disabled = false;
    }
    document.addEventListener('click', (e) => {
        const chatBtn = e.target.closest('[data-chat]');
        if (chatBtn) { openChat(); sendMessage(chatBtn.getAttribute('data-chat')); return; }
        if (e.target.closest('[data-open]')) { openChat(); return; }
        const q = e.target.closest('[data-q]');
        if (q) { sendMessage(q.getAttribute('data-q')); return; }
        const go = e.target.closest('[data-go]');
        if (go) {
            closeChat();
            const target = document.getElementById(go.getAttribute('data-go'));
            if (target) target.scrollIntoView();
        }
    });
    document.getElementById('chat-close').addEventListener('click', closeChat);
    sendBtn.addEventListener('click', () => sendMessage(inputEl.value));
    inputEl.addEventListener('keydown', (e) => { if (e.key === 'Enter') { e.preventDefault(); sendMessage(inputEl.value); } });
    document.addEventListener('keydown', (e) => { if (e.key === 'Escape') closeChat(); });
</script>
</body>
</html>
""".replace("/*BASE*/", BASE_CSS)


def site_context(gh):
    return {
        "slug": gh["slug"],
        "name": gh["name"],
        "tagline": (gh.get("tagline") or "").strip(),
        "location": (gh.get("location") or "").strip(),
        "about": (gh.get("about") or "").strip(),
        "hero": safe_url(gh.get("hero_image_url")),
        "phone": (gh.get("phone") or "").strip(),
        "phone_href": phone_href(gh.get("phone")),
        "whatsapp_link": whatsapp_link(gh.get("whatsapp")),
        "email": (gh.get("email") or "").strip(),
        "rooms": parse_rooms(gh.get("rooms_json")),
    }


def render_guest_site(slug):
    if slug in RESERVED_SLUGS or not SLUG_RE.match(slug):
        return message_page("Page not found", "We could not find that page.", 404)
    try:
        gh = fetch_guesthouse(slug)
    except Exception as e:
        app.logger.exception("Database error on guest site")
        detail = f" ({type(e).__name__}: {e})" if DEBUG_ERRORS else ""
        return message_page("Please try again", "This page is temporarily unavailable." + detail, 500)
    if not gh:
        return message_page("Guesthouse not found", "We could not find that guesthouse. Please check the link.", 404)
    return render_template_string(GUEST_HTML, s=site_context(gh), fonts=FONTS)


@app.route("/")
def home():
    return message_page("BookaRoom", "A 24/7 booking assistant for South African guesthouses.")


@app.route("/health")
def health():
    return "ok"


@app.route("/gate/<guesthouse_slug>")
def old_gate_link(guesthouse_slug):
    return redirect(url_for("guest_site", guesthouse_slug=guesthouse_slug), code=301)


@app.route("/<guesthouse_slug>")
def guest_site(guesthouse_slug):
    return render_guest_site(guesthouse_slug)


# ---------------------------------------------------------------------------
# Chat endpoint
# ---------------------------------------------------------------------------

def build_system_instruction(gh):
    rooms = parse_rooms(gh.get("rooms_json"))
    room_lines = "\n".join(
        f"- {r['name']}: {r['price']}. {r['description']}".strip() for r in rooms
    ) or "No room list has been added yet."
    calendar_status = get_live_calendar_status(gh.get("ical_url"))
    today_text = south_africa_today().strftime("%A, %d %B %Y")
    contact_bits = []
    if (gh.get("phone") or "").strip():
        contact_bits.append("phone " + gh["phone"].strip())
    if (gh.get("whatsapp") or "").strip():
        contact_bits.append("WhatsApp " + gh["whatsapp"].strip())
    if (gh.get("email") or "").strip():
        contact_bits.append("email " + gh["email"].strip())
    contact_text = ", ".join(contact_bits) or "not provided"

    return (
        f"You are the virtual booking assistant for {gh['name']}. You converse politely and professionally.\n"
        f"Today's date in South Africa is {today_text}.\n"
        f"About the guesthouse: {(gh.get('about') or '').strip()}\n"
        f"Location: {(gh.get('location') or '').strip()}\n"
        f"Rooms and prices:\n{room_lines}\n"
        f"Extra information, rates and house rules: {gh.get('custom_prompt') or ''}\n"
        f"Owner contact details: {contact_text}\n"
        f"Live Calendar Booked Blockouts:\n{calendar_status}\n\n"
        "The guest is chatting on the guesthouse's own website, which has a Rooms section. "
        "If the guest wants to see the rooms, tell them to tap 'View rooms' above the message box or scroll to 'Our rooms'.\n"
        "Only state facts you have been given above. If you do not know something, say so and offer the owner's contact details.\n"
        "Keep replies short and easy to read on a phone. Use short paragraphs or simple bullet lists. Do not use tables.\n"
        "If the customer is satisfied and explicitly ready to book or check in, you MUST provide them with "
        f"the following direct payment details: {gh.get('payment_details') or ''} .\n"
        "Crucial Note: Explicitly add a friendly security reminder saying: 'Please note: Payments go "
        "100% directly to the owner. Owners are advised to move received funds to a secondary secure account immediately.'"
    )


def busy_reply(gh):
    text = "Sorry, I am a bit busy right now. Please try again in a moment."
    phone = (gh.get("phone") or "").strip()
    whatsapp = (gh.get("whatsapp") or "").strip()
    if phone or whatsapp:
        text += " You can also contact us directly on " + (whatsapp or phone) + "."
    return text


@app.route("/chat/<guesthouse_slug>", methods=["POST"])
def dynamic_chat_endpoint(guesthouse_slug):
    try:
        guesthouse = fetch_guesthouse(guesthouse_slug)
    except Exception:
        app.logger.exception("Database error in chat endpoint")
        return jsonify({"reply": "Sorry, something went wrong. Please try again shortly.", "error": True}), 500

    if not guesthouse:
        return jsonify({"reply": "System configuration error.", "error": True}), 404

    data = request.get_json(silent=True) or {}
    raw_messages = data.get("messages")
    if not isinstance(raw_messages, list):
        raw_messages = [{"role": "user", "text": data.get("message", "")}]

    cleaned = []
    for m in raw_messages[-12:]:
        if not isinstance(m, dict):
            continue
        role = "model" if m.get("role") == "model" else "user"
        text = str(m.get("text", "")).strip()[:1000]
        if text:
            cleaned.append((role, text))

    while cleaned and cleaned[0][0] != "user":
        cleaned.pop(0)
    if not cleaned or cleaned[-1][0] != "user":
        return jsonify({"reply": "Please type a message and try again.", "error": True}), 400

    contents = [
        types.Content(role=role, parts=[types.Part.from_text(text=text)])
        for role, text in cleaned
    ]
    system_instruction = build_system_instruction(guesthouse)

    models_to_try = []
    for name in (GEMINI_MODEL, "gemini-flash-latest", "gemini-flash-lite-latest"):
        if name and name not in models_to_try:
            models_to_try.append(name)

    response = None
    last_err = None
    for model_name in models_to_try:
        for attempt in range(2):
            try:
                response = ai_client.models.generate_content(
                    model=model_name,
                    contents=contents,
                    config=types.GenerateContentConfig(system_instruction=system_instruction),
                )
                break
            except Exception as err:
                last_err = err
                temporary = any(code in str(err) for code in ("503", "UNAVAILABLE", "429"))
                if temporary and attempt == 0:
                    time.sleep(1.5)
                    continue
                break
        if response is not None:
            break

    if response is None:
        app.logger.error("Gemini failed on all models: %s: %s", type(last_err).__name__, last_err)
        if DEBUG_ERRORS:
            reply = f"AI error (debug): {type(last_err).__name__}: {str(last_err)[:300]}"
        else:
            reply = busy_reply(guesthouse)
        return jsonify({"reply": reply, "error": True}), 200

    return jsonify({"reply": response.text or "Sorry, I could not answer that. Please try again."})


# ---------------------------------------------------------------------------
# Owner dashboard
# ---------------------------------------------------------------------------

OWNER_HTML = r"""
<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="robots" content="noindex, nofollow">
<title>Owner dashboard | {{ f.name or slug }}</title>
{{ fonts|safe }}
<style>
/*BASE*/
body { background: var(--stone); padding-bottom: 96px; }
.top { background: var(--ink); color: #fff; padding: 18px 20px; }
.top h1 { font-size: 22px; }
.top p { margin: 4px 0 0; font-size: 14px; color: rgba(255,255,255,0.8); }
.top-links { display: flex; gap: 10px; margin-top: 14px; flex-wrap: wrap; align-items: center; }
.top-links a, .top-links button {
    font: inherit; font-size: 14px; font-weight: 600; color: #fff; text-decoration: none;
    background: transparent; border: 1.5px solid rgba(255,255,255,0.6);
    border-radius: 999px; padding: 8px 16px; cursor: pointer;
}
.wrap { max-width: 640px; margin: 0 auto; padding: 20px 16px; }
.card { background: #fff; border: 1px solid var(--line); border-radius: 14px; padding: 20px; margin-bottom: 16px; }
.card h2 { font-size: 22px; color: var(--ink); margin-bottom: 4px; }
.hint { margin: 0 0 14px; font-size: 14px; color: var(--muted); }
label { display: block; font-weight: 600; font-size: 15px; margin: 14px 0 6px; }
input[type=text], input[type=email], input[type=url], input[type=password], textarea {
    width: 100%; font: inherit; font-size: 16px; color: var(--text);
    padding: 12px 14px; border: 1.5px solid var(--line); border-radius: 10px; background: #fff;
}
textarea { min-height: 110px; resize: vertical; }
input:focus, textarea:focus { outline: none; border-color: var(--ink); }
.notice { padding: 12px 16px; border-radius: 10px; margin-bottom: 16px; font-weight: 500; }
.ok { background: #DDEFE3; color: #14472A; }
.err { background: #F6DADA; color: var(--danger); }
.room-edit { border: 1.5px solid var(--line); border-radius: 12px; padding: 4px 14px 14px; margin-bottom: 12px; }
.btn {
    font: inherit; font-weight: 600; font-size: 15px; border-radius: 999px;
    padding: 11px 20px; border: 1.5px solid var(--ink); background: #fff; color: var(--ink); cursor: pointer;
}
.btn-main { background: var(--ink); color: #fff; }
.btn-small { font-size: 14px; padding: 8px 14px; margin-top: 12px; }
.savebar {
    position: fixed; left: 0; right: 0; bottom: 0; background: #fff; border-top: 1px solid var(--line);
    padding: 12px 16px calc(12px + env(safe-area-inset-bottom));
}
.savebar .inner { max-width: 640px; margin: 0 auto; }
.savebar .btn { width: 100%; padding: 14px; font-size: 17px; }
details { margin-top: 12px; }
summary { font-weight: 600; cursor: pointer; color: var(--ink); }
details ol { padding-left: 20px; margin: 10px 0 0; }
details li { margin-bottom: 6px; }
#cal-result { margin: 10px 0 0; font-size: 15px; font-weight: 500; }
.login { max-width: 420px; margin: 60px auto; padding: 0 16px; }
</style>
</head>
<body>

{% if not logged_in %}
<div class="login">
    <div class="card">
        <h2>Owner login</h2>
        <p class="hint">{{ f.name or slug }}</p>
        {% if error %}<div class="notice err">{{ error }}</div>{% endif %}
        <form method="post" action="/owner/{{ slug }}/login">
            <input type="hidden" name="csrf" value="{{ csrf }}">
            <label for="password">Password</label>
            <input type="password" id="password" name="password" autocomplete="current-password" required>
            <p style="margin: 18px 0 0;"><button class="btn btn-main" type="submit">Log in</button></p>
        </form>
    </div>
</div>
{% else %}

<div class="top">
    <h1>{{ f.name or slug }}</h1>
    <p>Owner dashboard. Guests never see this page.</p>
    <div class="top-links">
        <a href="/{{ slug }}" target="_blank" rel="noopener">View your site</a>
        <form method="post" action="/owner/{{ slug }}/logout" style="margin:0;">
            <input type="hidden" name="csrf" value="{{ csrf }}">
            <button type="submit">Log out</button>
        </form>
    </div>
</div>

<form method="post" action="/owner/{{ slug }}/save" id="form" class="wrap">
    <input type="hidden" name="csrf" value="{{ csrf }}">
    <input type="hidden" name="rooms_json" id="rooms_json">

    {% if saved %}<div class="notice ok">Saved. Your site is updated.</div>{% endif %}
    {% if error %}<div class="notice err">{{ error }}</div>{% endif %}

    <div class="card">
        <h2>Your site</h2>
        <p class="hint">This is what guests see at the top of your page.</p>
        <label for="name">Guesthouse name</label>
        <input type="text" id="name" name="name" value="{{ f.name }}" maxlength="80" required>
        <label for="tagline">Short tagline</label>
        <input type="text" id="tagline" name="tagline" value="{{ f.tagline }}" maxlength="120" placeholder="Quiet rooms, a short drive from the Kruger gate">
        <label for="location">Location</label>
        <input type="text" id="location" name="location" value="{{ f.location }}" maxlength="120" placeholder="Hazyview, Mpumalanga">
        <label for="about">Welcome message</label>
        <textarea id="about" name="about" maxlength="1200" placeholder="A few friendly lines about your guesthouse.">{{ f.about }}</textarea>
        <label for="hero_image_url">Main photo link (optional)</label>
        <input type="url" id="hero_image_url" name="hero_image_url" value="{{ f.hero_image_url }}" maxlength="500" placeholder="https://...">
        <p class="hint" style="margin-top:6px;">Use a direct link to a photo. It must start with https://</p>
    </div>

    <div class="card">
        <h2>Contact</h2>
        <p class="hint">Shown on your site, and offered by the assistant if it cannot answer something.</p>
        <label for="phone">Phone number</label>
        <input type="text" id="phone" name="phone" value="{{ f.phone }}" maxlength="30">
        <label for="whatsapp">WhatsApp number</label>
        <input type="text" id="whatsapp" name="whatsapp" value="{{ f.whatsapp }}" maxlength="30">
        <label for="email">Email</label>
        <input type="email" id="email" name="email" value="{{ f.email }}" maxlength="120">
    </div>

    <div class="card">
        <h2>Rooms</h2>
        <p class="hint">Add each room type with its price. These appear on your site and the assistant uses them to answer guests.</p>
        <div id="rooms-list"></div>
        <button type="button" class="btn" id="add-room">Add a room</button>
    </div>

    <div class="card">
        <h2>Booking assistant</h2>
        <p class="hint">Everything else the assistant should know.</p>
        <label for="custom_prompt">Rates, house rules and gate times</label>
        <textarea id="custom_prompt" name="custom_prompt" maxlength="4000" placeholder="Check-in from 14:00, check-out by 10:00. Gate closes at 22:00. No smoking indoors. Breakfast included.">{{ f.custom_prompt }}</textarea>
        <label for="payment_details">Payment details</label>
        <textarea id="payment_details" name="payment_details" maxlength="1000" placeholder="Bank name, account name, account number, reference. Or your Yoco payment link.">{{ f.payment_details }}</textarea>
        <p class="hint" style="margin-top:6px;">Only shown to a guest who is ready to book.</p>
    </div>

    <div class="card">
        <h2>Availability calendar</h2>
        <p class="hint">Connect your Google Calendar so the assistant knows which dates are taken.</p>
        <label for="ical_url">Calendar link (iCal)</label>
        <input type="url" id="ical_url" name="ical_url" value="{{ f.ical_url }}" maxlength="600" placeholder="https://calendar.google.com/calendar/ical/.../basic.ics">
        <button type="button" class="btn btn-small" id="check-cal">Check calendar</button>
        <p id="cal-result"></p>
        <details>
            <summary>How to find my calendar link</summary>
            <ol>
                <li>Open calendar.google.com in your browser. On a phone, choose "Desktop site" in the browser menu.</li>
                <li>On the left, under "My calendars", tap the three dots next to the calendar you use for bookings, then "Settings and sharing".</li>
                <li>Scroll down to "Integrate calendar".</li>
                <li>Copy the "Secret address in iCal format". It ends in .ics</li>
                <li>Paste it in the box above and tap Save changes.</li>
            </ol>
        </details>
    </div>

    <div class="savebar"><div class="inner"><button type="submit" class="btn btn-main">Save changes</button></div></div>
</form>

<script>
    const slug = {{ slug|tojson }};
    const csrf = {{ csrf|tojson }};
    const initialRooms = {{ rooms|tojson }};
    const listEl = document.getElementById('rooms-list');

    function makeField(box, key, label, type, value, placeholder) {
        const lab = document.createElement('label');
        lab.textContent = label;
        const input = document.createElement(type === 'textarea' ? 'textarea' : 'input');
        if (type !== 'textarea') input.type = type;
        input.value = value || '';
        input.setAttribute('data-f', key);
        if (placeholder) input.placeholder = placeholder;
        if (type === 'textarea') input.style.minHeight = '80px';
        box.appendChild(lab);
        box.appendChild(input);
    }
    function addRoom(room) {
        room = room || {};
        const box = document.createElement('div');
        box.className = 'room-edit';
        makeField(box, 'name', 'Room name', 'text', room.name, 'Double room');
        makeField(box, 'price', 'Price', 'text', room.price, 'R950 per night');
        makeField(box, 'description', 'Description', 'textarea', room.description, 'Queen bed, en-suite, garden view');
        makeField(box, 'image', 'Photo link (optional)', 'url', room.image, 'https://...');
        const remove = document.createElement('button');
        remove.type = 'button';
        remove.className = 'btn btn-small';
        remove.textContent = 'Remove this room';
        remove.addEventListener('click', () => box.remove());
        box.appendChild(remove);
        listEl.appendChild(box);
    }
    initialRooms.forEach(addRoom);
    document.getElementById('add-room').addEventListener('click', () => addRoom({}));

    document.getElementById('form').addEventListener('submit', () => {
        const rooms = [];
        listEl.querySelectorAll('.room-edit').forEach((box) => {
            const get = (k) => box.querySelector('[data-f="' + k + '"]').value;
            rooms.push({ name: get('name'), price: get('price'), description: get('description'), image: get('image') });
        });
        document.getElementById('rooms_json').value = JSON.stringify(rooms);
    });

    document.getElementById('check-cal').addEventListener('click', async () => {
        const out = document.getElementById('cal-result');
        out.textContent = 'Checking...';
        const fd = new FormData();
        fd.append('csrf', csrf);
        fd.append('ical_url', document.getElementById('ical_url').value);
        try {
            const res = await fetch('/owner/' + slug + '/check-calendar', { method: 'POST', body: fd });
            const data = await res.json();
            out.textContent = data.message;
        } catch (err) {
            out.textContent = 'Could not check the calendar. Please try again.';
        }
    });
</script>
{% endif %}
</body>
</html>
""".replace("/*BASE*/", BASE_CSS)


def owner_form_values(gh):
    return {
        "name": gh.get("name") or "",
        "tagline": gh.get("tagline") or "",
        "location": gh.get("location") or "",
        "about": gh.get("about") or "",
        "hero_image_url": gh.get("hero_image_url") or "",
        "phone": gh.get("phone") or "",
        "whatsapp": gh.get("whatsapp") or "",
        "email": gh.get("email") or "",
        "payment_details": gh.get("payment_details") or "",
        "ical_url": gh.get("ical_url") or "",
        "custom_prompt": gh.get("custom_prompt") or "",
    }


def render_owner(slug, f, rooms, logged_in, error=None, saved=False, status=200):
    html = render_template_string(
        OWNER_HTML, slug=slug, f=f, rooms=rooms, logged_in=logged_in,
        error=error, saved=saved, csrf=csrf_token(), fonts=FONTS,
    )
    return html, status


def load_owner_guesthouse(slug):
    if not SLUG_RE.match(slug):
        return None
    return fetch_guesthouse(slug)


@app.route("/owner/<slug>")
def owner_page(slug):
    try:
        gh = load_owner_guesthouse(slug)
    except Exception:
        app.logger.exception("Database error on owner page")
        return message_page("Please try again", "The dashboard is temporarily unavailable.", 500)
    if not gh:
        return message_page("Not found", "We could not find that guesthouse.", 404)
    if session.get("owner_slug") != slug:
        return render_owner(slug, {"name": gh["name"]}, [], logged_in=False)
    return render_owner(
        slug, owner_form_values(gh), parse_rooms(gh.get("rooms_json")),
        logged_in=True, saved=request.args.get("saved") == "1",
    )


@app.route("/owner/<slug>/login", methods=["POST"])
def owner_login(slug):
    if not csrf_ok():
        return message_page("Session expired", "Please go back, reload the page and try again.", 400)
    try:
        gh = load_owner_guesthouse(slug)
    except Exception:
        app.logger.exception("Database error on owner login")
        return message_page("Please try again", "The dashboard is temporarily unavailable.", 500)
    if not gh:
        return message_page("Not found", "We could not find that guesthouse.", 404)

    stored = gh.get("owner_password_hash") or ""
    password = request.form.get("password", "")
    if stored and check_password_hash(stored, password):
        session.clear()
        session.permanent = True
        session["owner_slug"] = slug
        return redirect(url_for("owner_page", slug=slug))

    time.sleep(1)
    error = "Wrong password. Please try again." if stored else "No password has been set for this guesthouse yet. Please contact BookaRoom."
    return render_owner(slug, {"name": gh["name"]}, [], logged_in=False, error=error, status=401)


@app.route("/owner/<slug>/logout", methods=["POST"])
def owner_logout(slug):
    if csrf_ok():
        session.pop("owner_slug", None)
    return redirect(url_for("owner_page", slug=slug))


@app.route("/owner/<slug>/save", methods=["POST"])
def owner_save(slug):
    if session.get("owner_slug") != slug:
        return redirect(url_for("owner_page", slug=slug))
    if not csrf_ok():
        return message_page("Session expired", "Please go back, reload the page and try again.", 400)
    try:
        gh = load_owner_guesthouse(slug)
    except Exception:
        app.logger.exception("Database error on owner save")
        return message_page("Please try again", "The dashboard is temporarily unavailable.", 500)
    if not gh:
        return message_page("Not found", "We could not find that guesthouse.", 404)

    form = request.form
    f = {
        "name": clip(form.get("name"), 80),
        "tagline": clip(form.get("tagline"), 120),
        "location": clip(form.get("location"), 120),
        "about": clip(form.get("about"), 1200),
        "hero_image_url": clip(form.get("hero_image_url"), 500),
        "phone": clip(form.get("phone"), 30),
        "whatsapp": clip(form.get("whatsapp"), 30),
        "email": clip(form.get("email"), 120),
        "payment_details": clip(form.get("payment_details"), 1000),
        "ical_url": clip(form.get("ical_url"), 600),
        "custom_prompt": clip(form.get("custom_prompt"), 4000),
    }
    rooms = parse_rooms(form.get("rooms_json", "[]"))

    errors = []
    if not f["name"]:
        errors.append("Please enter the guesthouse name.")
    if f["hero_image_url"] and not safe_url(f["hero_image_url"]):
        errors.append("The main photo link must start with https:// and contain no spaces.")
    if f["ical_url"] and not is_safe_ical_url(f["ical_url"]):
        errors.append("The calendar link must start with https://")
    if f["email"] and "@" not in f["email"]:
        errors.append("That email address does not look right.")
    if errors:
        return render_owner(slug, f, rooms, logged_in=True, error=" ".join(errors), status=400)

    try:
        db_run(
            "UPDATE guesthouses SET name=%s, tagline=%s, location=%s, about=%s, hero_image_url=%s, "
            "phone=%s, whatsapp=%s, email=%s, rooms_json=%s, payment_details=%s, ical_url=%s, "
            "custom_prompt=%s WHERE slug=%s",
            (
                f["name"], f["tagline"], f["location"], f["about"], f["hero_image_url"],
                f["phone"], f["whatsapp"], f["email"], json.dumps(rooms), f["payment_details"],
                f["ical_url"], f["custom_prompt"], slug,
            ),
        )
    except Exception:
        app.logger.exception("Database error saving owner changes")
        return render_owner(slug, f, rooms, logged_in=True, error="Could not save right now. Please try again.", status=500)
    return redirect(url_for("owner_page", slug=slug, saved=1))


@app.route("/owner/<slug>/check-calendar", methods=["POST"])
def owner_check_calendar(slug):
    if session.get("owner_slug") != slug or not csrf_ok():
        return jsonify({"message": "Please log in again."}), 403
    url = clip(request.form.get("ical_url"), 600)
    if not url:
        return jsonify({"message": "Paste your calendar link first."})
    if not is_safe_ical_url(url):
        return jsonify({"message": "The link must start with https://"})
    try:
        bookings = load_bookings(url)
    except Exception as e:
        detail = str(e) if isinstance(e, ValueError) else "Could not read a calendar from that link."
        return jsonify({"message": detail})
    if not bookings:
        return jsonify({"message": "Calendar connected. No upcoming bookings found, so all dates show as open."})
    first = bookings[0]
    return jsonify({
        "message": f"Calendar connected. Found {len(bookings)} upcoming booking(s). "
                   f"Next: {first[0].strftime('%d %b %Y')} to {first[1].strftime('%d %b %Y')}."
    })


# ---------------------------------------------------------------------------
# Admin (platform owner only)
# ---------------------------------------------------------------------------

ADMIN_HTML = r"""
<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="robots" content="noindex, nofollow">
<title>BookaRoom admin</title>
{{ fonts|safe }}
<style>
/*BASE*/
body { background: var(--stone); padding: 20px 16px 60px; }
.wrap { max-width: 640px; margin: 0 auto; }
h1 { font-size: 28px; color: var(--ink); margin-bottom: 16px; }
.card { background: #fff; border: 1px solid var(--line); border-radius: 14px; padding: 20px; margin-bottom: 16px; }
.card h2 { font-size: 21px; color: var(--ink); margin-bottom: 10px; }
label { display: block; font-weight: 600; font-size: 15px; margin: 12px 0 6px; }
input[type=text], input[type=password] {
    width: 100%; font: inherit; font-size: 16px; padding: 12px 14px;
    border: 1.5px solid var(--line); border-radius: 10px;
}
.btn {
    font: inherit; font-weight: 600; font-size: 15px; border-radius: 999px;
    padding: 11px 20px; border: 1.5px solid var(--ink); background: var(--ink); color: #fff; cursor: pointer; margin-top: 14px;
}
.btn-light { background: #fff; color: var(--ink); }
.notice { padding: 12px 16px; border-radius: 10px; margin-bottom: 16px; font-weight: 500; }
.ok { background: #DDEFE3; color: #14472A; }
.err { background: #F6DADA; color: var(--danger); }
.row { border-top: 1px solid var(--line); padding: 16px 0; }
.row:first-of-type { border-top: none; }
.row h3 { font-size: 19px; color: var(--ink); }
.row p { margin: 4px 0; font-size: 15px; word-break: break-all; }
.small { font-size: 14px; color: var(--muted); }
.inline { display: flex; gap: 8px; align-items: flex-end; }
.inline input { flex: 1; }
.inline .btn { margin-top: 0; white-space: nowrap; }
</style>
</head>
<body>
<div class="wrap">
<h1>BookaRoom admin</h1>

{% if not enabled %}
    <div class="card"><p>Admin is switched off. Add a setting called ADMIN_PASSWORD in Render to turn it on.</p></div>
{% elif not logged_in %}
    <div class="card">
        <h2>Log in</h2>
        {% if error %}<div class="notice err">{{ error }}</div>{% endif %}
        <form method="post" action="/admin/login">
            <input type="hidden" name="csrf" value="{{ csrf }}">
            <label for="password">Admin password</label>
            <input type="password" id="password" name="password" required>
            <button class="btn" type="submit">Log in</button>
        </form>
    </div>
{% else %}
    {% if message %}<div class="notice ok">{{ message }}</div>{% endif %}
    {% if error %}<div class="notice err">{{ error }}</div>{% endif %}

    <div class="card">
        <h2>Add a guesthouse</h2>
        <form method="post" action="/admin/create">
            <input type="hidden" name="csrf" value="{{ csrf }}">
            <label for="slug">Web address word</label>
            <input type="text" id="slug" name="slug" placeholder="sunset-lodge" required>
            <p class="small" style="margin:6px 0 0;">Lowercase letters, numbers and hyphens. This becomes the link guests use.</p>
            <label for="gname">Guesthouse name</label>
            <input type="text" id="gname" name="name" placeholder="Sunset Lodge" required>
            <label for="gpass">Owner password (at least 8 characters)</label>
            <input type="text" id="gpass" name="password" required>
            <button class="btn" type="submit">Create guesthouse</button>
        </form>
    </div>

    <div class="card">
        <h2>Your guesthouses</h2>
        {% for g in guesthouses %}
        <div class="row">
            <h3>{{ g.name }}</h3>
            <p>Guest site: <a href="/{{ g.slug }}" target="_blank" rel="noopener">/{{ g.slug }}</a></p>
            <p>Owner login: <a href="/owner/{{ g.slug }}" target="_blank" rel="noopener">/owner/{{ g.slug }}</a></p>
            <p class="small">{% if g.has_password %}Owner password is set.{% else %}No owner password yet.{% endif %}</p>
            <form method="post" action="/admin/password" class="inline">
                <input type="hidden" name="csrf" value="{{ csrf }}">
                <input type="hidden" name="slug" value="{{ g.slug }}">
                <div style="flex:1;">
                    <label for="pw-{{ g.slug }}" style="margin-top:8px;">{% if g.has_password %}Reset owner password{% else %}Set owner password{% endif %}</label>
                    <input type="text" id="pw-{{ g.slug }}" name="password" placeholder="At least 8 characters" required>
                </div>
                <button class="btn btn-light" type="submit">Save</button>
            </form>
        </div>
        {% else %}
        <p>No guesthouses yet.</p>
        {% endfor %}
    </div>

    <form method="post" action="/admin/logout"><input type="hidden" name="csrf" value="{{ csrf }}"><button class="btn btn-light" type="submit">Log out</button></form>
{% endif %}
</div>
</body>
</html>
""".replace("/*BASE*/", BASE_CSS)


def render_admin(error=None, message=None, status=200):
    enabled = bool(ADMIN_PASSWORD)
    logged_in = enabled and session.get("admin") is True
    guesthouses = []
    if logged_in:
        try:
            guesthouses = db_run(
                "SELECT id, slug, name, "
                "(owner_password_hash IS NOT NULL AND owner_password_hash <> '') AS has_password "
                "FROM guesthouses ORDER BY id",
                fetch="all",
            ) or []
        except Exception as e:
            app.logger.exception("Database error on admin page")
            error = error or f"Could not load guesthouses ({type(e).__name__})."
    html = render_template_string(
        ADMIN_HTML, enabled=enabled, logged_in=logged_in, guesthouses=guesthouses,
        error=error, message=message, csrf=csrf_token(), fonts=FONTS,
    )
    return html, status


def admin_required():
    return bool(ADMIN_PASSWORD) and session.get("admin") is True and csrf_ok()


@app.route("/admin")
def admin_page():
    return render_admin()


@app.route("/admin/login", methods=["POST"])
def admin_login():
    if not ADMIN_PASSWORD:
        return render_admin()
    if not csrf_ok():
        return render_admin(error="Session expired. Please try again.", status=400)
    if hmac.compare_digest(request.form.get("password", ""), ADMIN_PASSWORD):
        session.clear()
        session.permanent = True
        session["admin"] = True
        return redirect(url_for("admin_page"))
    time.sleep(1)
    return render_admin(error="Wrong password.", status=401)


@app.route("/admin/logout", methods=["POST"])
def admin_logout():
    if csrf_ok():
        session.pop("admin", None)
    return redirect(url_for("admin_page"))


@app.route("/admin/create", methods=["POST"])
def admin_create():
    if not admin_required():
        return redirect(url_for("admin_page"))
    slug = clip(request.form.get("slug"), 40).lower()
    name = clip(request.form.get("name"), 80)
    password = request.form.get("password", "")
    if not SLUG_RE.match(slug) or len(slug) < 3 or slug in RESERVED_SLUGS:
        return render_admin(error="That web address word is not allowed. Use 3 or more lowercase letters, numbers and hyphens.", status=400)
    if not name:
        return render_admin(error="Please enter the guesthouse name.", status=400)
    if len(password) < 8:
        return render_admin(error="The owner password needs at least 8 characters.", status=400)
    try:
        db_run(
            "INSERT INTO guesthouses (slug, name, payment_details, ical_url, custom_prompt, rooms_json, owner_password_hash) "
            "VALUES (%s, %s, '', '', '', '[]', %s)",
            (slug, name, generate_password_hash(password)),
        )
    except psycopg2.IntegrityError:
        return render_admin(error="That web address word is already taken.", status=400)
    except Exception:
        app.logger.exception("Database error creating guesthouse")
        return render_admin(error="Could not create the guesthouse. Please try again.", status=500)
    return render_admin(message=f"Created {name}. Owner login: /owner/{slug}")


@app.route("/admin/password", methods=["POST"])
def admin_password():
    if not admin_required():
        return redirect(url_for("admin_page"))
    slug = clip(request.form.get("slug"), 40)
    password = request.form.get("password", "")
    if len(password) < 8:
        return render_admin(error="The owner password needs at least 8 characters.", status=400)
    try:
        db_run(
            "UPDATE guesthouses SET owner_password_hash = %s WHERE slug = %s",
            (generate_password_hash(password), slug),
        )
    except Exception:
        app.logger.exception("Database error setting password")
        return render_admin(error="Could not save the password. Please try again.", status=500)
    return render_admin(message=f"Password saved for {slug}.")


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)))
