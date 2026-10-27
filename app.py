import os
import requests
import psycopg2
from psycopg2.extras import RealDictCursor
from flask import Flask, request, jsonify, render_template_string
from google import genai
from google.genai import types
from icalendar import Calendar
from datetime import datetime, date, timedelta

app = Flask(__name__)

ai_client = genai.Client(api_key=os.environ.get("GEMINI_API_KEY"))
DATABASE_URL = os.environ.get("DATABASE_URL")


def get_db_connection():
    return psycopg2.connect(DATABASE_URL, sslmode="require")


def south_africa_today():
    # South Africa is UTC+2 all year (no daylight saving)
    return (datetime.utcnow() + timedelta(hours=2)).date()


def to_date(value):
    if isinstance(value, datetime):
        return value.date()
    return value


def get_live_calendar_status(ical_url):
    if not ical_url or ical_url.strip() == "":
        return "No external calendar connected yet. All rooms assumed open."
    try:
        response = requests.get(ical_url.strip(), timeout=5)
        if response.status_code != 200:
            return "Live availability calendar currently unavailable."

        cal = Calendar.from_ical(response.content)
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

                # Only keep bookings that have not finished yet
                if end > today:
                    bookings.append((start, end))

        if not bookings:
            return "All rooms are open and available."

        bookings.sort(key=lambda b: b[0])
        lines = [
            f"Booked from {s.strftime('%d %b %Y')} to {e.strftime('%d %b %Y')}"
            for s, e in bookings[:60]
        ]
        return "\n".join(lines)
    except Exception:
        return "Live availability data offline."


CHAT_HTML = r"""
<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="utf-8">
    <meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">
    <meta name="theme-color" content="#173A36">
    <title>Chat with {{ property_name }}</title>
    <link rel="preconnect" href="https://fonts.googleapis.com">
    <link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
    <link href="https://fonts.googleapis.com/css2?family=Figtree:wght@400;500;600&family=Fraunces:opsz,wght@9..144,500;9..144,600&display=swap" rel="stylesheet">
    <style>
        :root {
            --ink: #173A36;
            --ink-soft: #2B544F;
            --stone: #E8EDE9;
            --paper: #FFFFFF;
            --text: #1B2624;
            --muted: #5F6F6B;
            --line: #D5DDD8;
            --ochre: #C58B2B;
        }
        * { box-sizing: border-box; -webkit-tap-highlight-color: transparent; }
        html, body { height: 100%; margin: 0; }
        body {
            font-family: "Figtree", system-ui, -apple-system, "Segoe UI", Roboto, sans-serif;
            background: var(--stone);
            color: var(--text);
            display: flex;
            justify-content: center;
        }
        #app {
            width: 100%;
            max-width: 560px;
            height: 100vh;
            height: 100dvh;
            display: flex;
            flex-direction: column;
            background: var(--stone);
        }
        header {
            background: var(--ink);
            color: #fff;
            padding: calc(16px + env(safe-area-inset-top)) 20px 16px;
            flex-shrink: 0;
        }
        header h1 {
            font-family: "Fraunces", Georgia, serif;
            font-weight: 600;
            font-size: 22px;
            line-height: 1.2;
            margin: 0 0 6px;
            letter-spacing: 0.1px;
        }
        .status {
            display: flex;
            align-items: center;
            gap: 8px;
            font-size: 13px;
            color: rgba(255,255,255,0.78);
        }
        .dot {
            width: 8px; height: 8px;
            border-radius: 50%;
            background: var(--ochre);
            flex-shrink: 0;
        }
        #messages {
            flex: 1;
            overflow-y: auto;
            padding: 20px 16px 8px;
            display: flex;
            flex-direction: column;
            gap: 10px;
            -webkit-overflow-scrolling: touch;
        }
        .msg {
            max-width: 86%;
            padding: 11px 15px;
            font-size: 16px;
            line-height: 1.5;
            word-wrap: break-word;
            overflow-wrap: anywhere;
            animation: rise 0.22s ease-out;
        }
        .msg p { margin: 0 0 8px; }
        .msg p:last-child, .msg ul:last-child, .msg ol:last-child { margin-bottom: 0; }
        .msg ul, .msg ol { margin: 0 0 8px; padding-left: 20px; }
        .msg li { margin-bottom: 4px; }
        .msg strong { font-weight: 600; }
        .bot {
            align-self: flex-start;
            background: var(--paper);
            border: 1px solid var(--line);
            border-radius: 4px 18px 18px 18px;
        }
        .user {
            align-self: flex-end;
            background: var(--ink);
            color: #fff;
            border-radius: 18px 4px 18px 18px;
            white-space: pre-wrap;
        }
        .typing {
            display: flex;
            gap: 5px;
            padding: 15px 16px;
        }
        .typing span {
            width: 7px; height: 7px;
            border-radius: 50%;
            background: var(--muted);
            animation: pulse 1.1s infinite ease-in-out;
        }
        .typing span:nth-child(2) { animation-delay: 0.15s; }
        .typing span:nth-child(3) { animation-delay: 0.3s; }
        #chips {
            display: flex;
            flex-wrap: wrap;
            gap: 8px;
            padding: 4px 16px 12px;
        }
        .chip {
            font-family: inherit;
            font-size: 14px;
            font-weight: 500;
            color: var(--ink);
            background: transparent;
            border: 1.5px solid var(--ink);
            border-radius: 999px;
            padding: 8px 14px;
            cursor: pointer;
        }
        .chip:active { background: var(--ink); color: #fff; }
        #input-area {
            display: flex;
            gap: 10px;
            padding: 12px 14px calc(12px + env(safe-area-inset-bottom));
            background: var(--paper);
            border-top: 1px solid var(--line);
            flex-shrink: 0;
        }
        #user-input {
            flex: 1;
            font-family: inherit;
            font-size: 16px;
            color: var(--text);
            padding: 12px 16px;
            border: 1.5px solid var(--line);
            border-radius: 999px;
            outline: none;
            background: var(--stone);
            min-width: 0;
        }
        #user-input:focus { border-color: var(--ink); background: #fff; }
        #send {
            font-family: inherit;
            font-size: 15px;
            font-weight: 600;
            color: #fff;
            background: var(--ink);
            border: none;
            border-radius: 999px;
            padding: 0 22px;
            cursor: pointer;
        }
        #send:disabled { opacity: 0.45; cursor: default; }
        #send:focus-visible, .chip:focus-visible { outline: 3px solid var(--ochre); outline-offset: 2px; }
        @keyframes rise {
            from { opacity: 0; transform: translateY(8px); }
            to { opacity: 1; transform: translateY(0); }
        }
        @keyframes pulse {
            0%, 80%, 100% { opacity: 0.3; transform: scale(0.85); }
            40% { opacity: 1; transform: scale(1); }
        }
        @media (prefers-reduced-motion: reduce) {
            .msg, .typing span { animation: none; }
        }
        @media (min-width: 600px) {
            #app { margin: 24px 0; height: calc(100dvh - 48px); border-radius: 20px; overflow: hidden; box-shadow: 0 12px 40px rgba(23,58,54,0.18); }
        }
    </style>
</head>
<body>
    <div id="app">
        <header>
            <h1>{{ property_name }}</h1>
            <div class="status"><span class="dot"></span>Online now. Replies instantly.</div>
        </header>
        <div id="messages">
            <div class="msg bot"><p>Welcome to {{ property_name }}. Ask me about rooms, rates or open dates, and I can help you book.</p></div>
        </div>
        <div id="chips">
            <button class="chip" data-q="What are your room rates?">Room rates</button>
            <button class="chip" data-q="Which dates are available?">Open dates</button>
            <button class="chip" data-q="How do I book a room?">How to book</button>
        </div>
        <div id="input-area">
            <input type="text" id="user-input" placeholder="Type your message" autocomplete="off" enterkeyhint="send">
            <button id="send">Send</button>
        </div>
    </div>
    <script>
        const slug = {{ slug|tojson }};
        const messagesEl = document.getElementById('messages');
        const inputEl = document.getElementById('user-input');
        const sendBtn = document.getElementById('send');
        const chipsEl = document.getElementById('chips');
        let history = [];
        let busy = false;

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
                if (res.ok) {
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
            inputEl.focus();
        }
        sendBtn.addEventListener('click', () => sendMessage(inputEl.value));
        inputEl.addEventListener('keydown', (e) => { if (e.key === 'Enter') { e.preventDefault(); sendMessage(inputEl.value); } });
        chipsEl.addEventListener('click', (e) => {
            const q = e.target.getAttribute('data-q');
            if (q) sendMessage(q);
        });
    </script>
</body>
</html>
"""


def fetch_guesthouse(slug):
    conn = get_db_connection()
    try:
        cur = conn.cursor(cursor_factory=RealDictCursor)
        cur.execute("SELECT * FROM guesthouses WHERE slug = %s", (slug,))
        guesthouse = cur.fetchone()
        cur.close()
        return guesthouse
    finally:
        conn.close()


@app.route("/gate/<guesthouse_slug>", methods=["GET"])
def dynamic_gate_page(guesthouse_slug):
    try:
        guesthouse = fetch_guesthouse(guesthouse_slug)
    except Exception as e:
        app.logger.exception("Database error on gate page")
        return f"Database error (temporary debug): {type(e).__name__}: {e}", 500

    if not guesthouse:
        return "Guesthouse system profile not found.", 404

    return render_template_string(CHAT_HTML, property_name=guesthouse["name"], slug=guesthouse_slug)


@app.route("/chat/<guesthouse_slug>", methods=["POST"])
def dynamic_chat_endpoint(guesthouse_slug):
    try:
        guesthouse = fetch_guesthouse(guesthouse_slug)
    except Exception:
        return jsonify({"reply": "Database wiring issue."}), 500

    if not guesthouse:
        return jsonify({"reply": "System configuration error."}), 404

    data = request.get_json(silent=True) or {}
    raw_messages = data.get("messages")

    # Backwards compatible: accept a single "message" too
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

    # Gemini needs the conversation to start with the guest and end with the guest
    while cleaned and cleaned[0][0] != "user":
        cleaned.pop(0)
    if not cleaned or cleaned[-1][0] != "user":
        return jsonify({"reply": "Please type a message and try again."}), 400

    contents = [
        types.Content(role=role, parts=[types.Part.from_text(text=text)])
        for role, text in cleaned
    ]

    calendar_status = get_live_calendar_status(guesthouse["ical_url"])
    today_text = south_africa_today().strftime("%A, %d %B %Y")

    system_instruction = (
        f"You are the virtual booking assistant for {guesthouse['name']}. You converse politely and professionally.\n"
        f"Today's date in South Africa is {today_text}.\n"
        f"Property Profile & Prices: {guesthouse['custom_prompt']}\n"
        f"Live Calendar Booked Blockouts:\n{calendar_status}\n\n"
        "Keep replies short and easy to read on a phone. Use short paragraphs or simple bullet lists. Do not use tables.\n"
        "If the customer is satisfied and explicitly ready to book or check in, you MUST provide them with "
        f"the following direct payment details: {guesthouse['payment_details']} .\n"
        "Crucial Note: Explicitly add a friendly security reminder saying: 'Please note: Payments go "
        "100% directly to the owner. Owners are advised to move received funds to a secondary secure account immediately.'"
    )

    try:
        response = ai_client.models.generate_content(
            model="gemini-2.5-flash",
            contents=contents,
            config=types.GenerateContentConfig(system_instruction=system_instruction),
        )
        return jsonify({"reply": response.text or "Sorry, I could not answer that. Please try again."})
    except Exception as e:
        app.logger.exception("Gemini error in chat endpoint")
        return jsonify({"reply": f"AI error (temporary debug): {type(e).__name__}: {str(e)[:300]}"}), 200


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)))
