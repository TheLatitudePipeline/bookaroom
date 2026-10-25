import os
import requests
from flask import Flask, request
from google import genai

app = Flask(__name__)

@app.route("/", methods=["GET"])
def home():
    return "BookaRoom Engine is Live and Running!", 200

@app.route("/webhook", methods=["GET"])
def verify_webhook():
    mode = request.args.get("hub.mode")
    token = request.args.get("hub.verify_token")
    challenge = request.args.get("hub.challenge")
    
    if mode == "subscribe" and token == "bookaroom_secret_token_2026":
        return challenge, 200
    return "Verification failed", 403

@app.route("/webhook", methods=["POST"])
def handle_message():
    payload = request.json
    print("Incoming message payload:", payload)
    return "OK", 200

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)))
