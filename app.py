import os
import requests
import psycopg2
from psycopg2.extras import RealDictCursor
from flask import Flask, request, jsonify, render_template_string
from google import genai
from google.genai import types
from icalendar import Calendar
from datetime import datetime

app = Flask(__name__)

# Initialize Google AI Studio Client
ai_client = genai.Client(api_key=os.environ.get("GEMINI_API_KEY"))
DATABASE_URL = os.environ.get("DATABASE_URL")

def get_db_connection():
    return psycopg2.connect(DATABASE_URL, sslmode='require')

def get_live_calendar_status(ical_url):
    if not ical_url or ical_url.strip() == "":
        return "No external calendar connected yet. All rooms assumed open."
    try:
        response = requests.get(ical_url, timeout=5)
        if response.status_code != 200:
            return "Live availability calendar currently unavailable."
        
        cal = Calendar.from_ical(response.content)
        current_time = datetime.now()
        active_bookings = []
        
        for component in cal.walk():
            if component.name == "VEVENT":
                start = component.get('dtstart').dt
                end = component.get('dtend').dt
                
                if isinstance(start, datetime): start = start.replace(tzinfo=None)
                if isinstance(end, datetime): end = end.replace(tzinfo=None)
                    
                if end > current_time:
                    active_bookings.append(f"Booked from {start.strftime('%d %b')} to {end.strftime('%d %b')}")
                    
        return "\\n".join(active_bookings) if active_bookings else "All rooms are open and available."
    except Exception:
        return "Live availability data offline."

CHAT_HTML = """
<!DOCTYPE html>
<html>
<head>
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Chat with {{ property_name }}</title>
    <style>
        body { font-family: Arial, sans-serif; margin: 0; padding: 10px; background: #f4f4f9; }
        #chatbox { max-width: 500px; margin: auto; background: white; border-radius: 8px; box-shadow: 0 2px 10px rgba(0,0,0,0.1); display: flex; flex-direction: column; height: 88vh; }
        #messages { flex: 1; padding: 15px; overflow-y: auto; border-bottom: 1px solid #ddd; }
        .msg { margin: 8px 0; padding: 10px; border-radius: 8px; max-width: 80%; word-wrap: break-word; font-size: 15px; }
        .user { background: #007bff; color: white; align-self: flex-end; margin-left: auto; }
        .bot { background: #e9ecef; color: black; align-self: flex-start; white-space: pre-wrap; }
        #input-area { display: flex; padding: 10px; }
        input { flex: 1; padding: 10px; border: 1px solid #ddd; border-radius: 4px; outline: none; }
        button { background: #007bff; color: white; border: none; padding: 10px 15px; margin-left: 5px; border-radius: 4px; cursor: pointer; }
    </style>
</head>
<body>
    <div id="chatbox">
        <div id="messages"><div class="msg bot">Welcome to {{ property_name }}. I am your 24/7 automated gate assistant. How can I help you right now?</div></div>
        <div id="input-area">
            <input type="text" id="user-input" placeholder="Ask about prices, rooms, or open dates..." onkeypress="if(event.key==='Enter') sendMessage()">
            <button onclick="sendMessage()">Send</button>
        </div>
    </div>
    <script>
        const slug = "{{ slug }}";
        async function sendMessage() {
            const input = document.getElementById('user-input');
            const text = input.value.trim();
            if (!text) return;
            
            appendMessage(text, 'user');
            input.value = '';
            
            const response = await fetch(`/chat/${slug}`, {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ message: text })
            });
            const data = await response.json();
            appendMessage(data.reply, 'bot');
        }
        function appendMessage(text, sender) {
            const msgs = document.getElementById('messages');
            const div = document.createElement('div');
            div.className = `msg ${sender}`;
            div.innerText = text;
            msgs.appendChild(div);
            msgs.scrollTop = msgs.scrollHeight;
        }
    </script>
</body>
</html>
"""

@app.route("/gate/<guesthouse_slug>", methods=["GET"])
def dynamic_gate_page(guesthouse_slug):
    try:
        conn = get_db_connection()
        cur = conn.cursor(cursor_factory=RealDictCursor)
        cur.execute("SELECT * FROM guesthouses WHERE slug = %s", (guesthouse_slug,))
        guesthouse = cur.fetchone()
        cur.close()
        conn.close()
        
        if not guesthouse:
            return "Guesthouse system profile not found.", 404
            
        return render_template_string(CHAT_HTML, property_name=guesthouse["name"], slug=guesthouse_slug)
    except Exception as e:
        return f"Database connectivity error: {str(e)}", 500

@app.route("/chat/<guesthouse_slug>", methods=["POST"])
def dynamic_chat_endpoint(guesthouse_slug):
    try:
        conn = get_db_connection()
        cur = conn.cursor(cursor_factory=RealDictCursor)
        cur.execute("SELECT * FROM guesthouses WHERE slug = %s", (guesthouse_slug,))
        guesthouse = cur.fetchone()
        cur.close()
        conn.close()
    except Exception:
        return jsonify({"reply": "Database wiring issue."}), 500
        
    if not guesthouse:
        return jsonify({"reply": "System configuration error."}), 404
        
    user_text = request.json.get("message", "")
    calendar_status = get_live_calendar_status(guesthouse["ical_url"])
    
    system_instruction = (
        f"You are the virtual booking assistant for {guesthouse['name']}. You converse politely and professionally.\\n"
        f"Property Profile & Prices: {guesthouse['custom_prompt']}\\n"
        f"Live Calendar Booked Blockouts:\\n{calendar_status}\\n\\n"
        "If the customer is satisfied and explicitly ready to book or check in, you MUST provide them with "
        f"the following direct payment details: {guesthouse['payment_details']} .\\n"
        "Crucial Note: Explicitly add a friendly security reminder saying: 'Please note: Payments go "
        "100% directly to the owner. Owners are advised to move received funds to a secondary secure account immediately.'"
    )
    
    try:
        response = ai_client.models.generate_content(
            model='gemini-2.5-flash',
            contents=user_text,
            config=types.GenerateContentConfig(system_instruction=system_instruction)
        )
        return jsonify({"reply": response.text})
    except Exception:
        return jsonify({"reply": "System busy. Please resend your message."}), 200

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)))
