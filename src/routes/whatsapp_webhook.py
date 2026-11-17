"""Receive WhatsApp messages from Meta's Cloud API webhook."""

import os

from fastapi import APIRouter, Request, Response

router = APIRouter()


@router.get("/webhook/whatsapp")
async def verify(request: Request):
    """Meta calls this once to verify the webhook URL."""
    params = request.query_params
    verify_token = os.environ.get("WHATSAPP_VERIFY_TOKEN", "top3-verify")

    if params.get("hub.mode") == "subscribe" and params.get("hub.verify_token") == verify_token:
        return Response(content=params.get("hub.challenge", ""), media_type="text/plain")
    return Response(content="verification failed", status_code=403)


@router.post("/webhook/whatsapp")
async def receive(request: Request):
    """Meta POSTs here whenever someone replies."""
    body = await request.json()

    for entry in body.get("entry", []):
        for change in entry.get("changes", []):
            value = change.get("value", {})

            # Map sender wa_id -> display name, when WhatsApp supplies it
            names = {
                c.get("wa_id"): c.get("profile", {}).get("name", "")
                for c in value.get("contacts", [])
            }

            for msg in value.get("messages", []):
                sender = msg.get("from")
                name = names.get(sender, "")
                kind = msg.get("type")

                if kind == "text":
                    text = msg["text"]["body"]
                elif kind == "button":
                    text = msg["button"].get("text", "")
                elif kind == "interactive":
                    block = msg["interactive"]
                    text = (block.get("button_reply") or block.get("list_reply") or {}).get("title", "")
                else:
                    text = f"<{kind} message>"

                print(f"[whatsapp] {name or sender}: {text}", flush=True)

    # Always 200 — Meta retries anything else.
    return {"status": "ok"}
