"""WhatsApp sender — Meta WhatsApp Cloud API.

Setup (one-time, at developers.facebook.com):
  1. Create a Meta app -> add the "WhatsApp" product
  2. Copy the test phone number ID and a temporary access token
  3. Add 919880603061 as a verified recipient (required while in test mode)

Then export:
  export WHATSAPP_TOKEN="EAAG..."
  export WHATSAPP_PHONE_NUMBER_ID="123456789012345"

Run:
  python -m src.whatsapp
"""

import os
import pathlib

import requests


def _load_env():
    """Read KEY=VALUE lines from .env at the project root, if present."""
    env_file = pathlib.Path(__file__).resolve().parent.parent / ".env"
    if not env_file.exists():
        return
    for line in env_file.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip())


_load_env()

GRAPH_API_VERSION = "v25.0"
DEFAULT_RECIPIENT = "919880603061"  # Kamal — E.164 without '+', India country code 91


def send_text(to: str, body: str) -> dict:
    """Send a free-form text message. Only allowed inside the 24-hour
    customer-service window; outside it, use send_template() instead."""
    token = os.environ["WHATSAPP_TOKEN"]
    phone_number_id = os.environ["WHATSAPP_PHONE_NUMBER_ID"]

    resp = requests.post(
        f"https://graph.facebook.com/{GRAPH_API_VERSION}/{phone_number_id}/messages",
        headers={"Authorization": f"Bearer {token}"},
        json={
            "messaging_product": "whatsapp",
            "recipient_type": "individual",
            "to": to,
            "type": "text",
            "text": {"preview_url": False, "body": body},
        },
        timeout=15,
    )
    resp.raise_for_status()
    return resp.json()


def send_template(to: str, name: str = "hello_world", lang: str = "en_US") -> dict:
    """Send a pre-approved template. This is what works for a first contact —
    'hello_world' ships with every new Cloud API app."""
    token = os.environ["WHATSAPP_TOKEN"]
    phone_number_id = os.environ["WHATSAPP_PHONE_NUMBER_ID"]

    resp = requests.post(
        f"https://graph.facebook.com/{GRAPH_API_VERSION}/{phone_number_id}/messages",
        headers={"Authorization": f"Bearer {token}"},
        json={
            "messaging_product": "whatsapp",
            "to": to,
            "type": "template",
            "template": {"name": name, "language": {"code": lang}},
        },
        timeout=15,
    )
    resp.raise_for_status()
    return resp.json()


if __name__ == "__main__":
    # First-ever message to a number must be a template.
    #print(send_template(DEFAULT_RECIPIENT))

    # Once Kamal replies, the 24h window opens and plain text works:
    print(send_text(DEFAULT_RECIPIENT, "hello test"))


# --- Alternative: Twilio (pip install twilio) -------------------------------
# from twilio.rest import Client
#
# def send_via_twilio(to: str = DEFAULT_RECIPIENT, body: str = "hello test") -> str:
#     client = Client(os.environ["TWILIO_ACCOUNT_SID"], os.environ["TWILIO_AUTH_TOKEN"])
#     msg = client.messages.create(
#         from_="whatsapp:+14155238886",   # Twilio sandbox number
#         to=f"whatsapp:+{to}",
#         body=body,
#     )
#     return msg.sid
