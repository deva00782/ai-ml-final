import json
import os
import time
import threading
from datetime import datetime, timezone
from flask import Flask, request, jsonify
import paho.mqtt.client as mqtt
from device_store import save_device, get_token
from apns_provider import send_push_sync

app = Flask(__name__)

# MQTT config (same as iOS before)
MQTT_HOST = "broker.emqx.io"
MQTT_PORT = 1883
MQTT_TOPIC = "aiml/rohan/commands"

# Track
mqtt_client = None

@app.route("/register-device", methods=["POST"])
def register_device():
    data = request.get_json(force=True, silent=True) or {}
    token = data.get("device_token")
    env = data.get("environment", "development")
    if not token:
        return jsonify({"error": "device_token required"}), 400
    # Basic validation: hex string 64 chars (32 bytes)
    if len(token) < 32:
        print(f"[APNS] Warning: suspicious token length {len(token)}")
    save_device(token, env)
    print(f"[APNS] Registered device_token {token[:8]}... env={env}")
    return jsonify({"status": "ok"}), 200

@app.route("/health", methods=["GET"])
def health():
    token = get_token()
    return jsonify({
        "status": "ok",
        "mqtt_host": MQTT_HOST,
        "mqtt_topic": MQTT_TOPIC,
        "has_token": token is not None,
        "token_prefix": token[:8] + "..." if token else None
    })

@app.route("/test-push", methods=["POST"])
def test_push():
    """Manual test: POST {"bci_command":"NEXT","confidence":0.99}"""
    data = request.get_json(force=True, silent=True) or {}
    cmd = data.get("bci_command") or data.get("command") or "NEXT"
    confidence = data.get("confidence", 0.99)
    timestamp = data.get("timestamp") or datetime.now(timezone.utc).isoformat()
    url = data.get("url")
    print(f"[TEST] Manual test_push {cmd}")
    result = forward_to_apns(cmd, confidence, timestamp, url)
    return jsonify(result)

def forward_to_apns(command: str, confidence: float, timestamp, url=None):
    token = get_token()
    if not token:
        print("[APNS] Device token not found - iPhone has not registered yet")
        print("[APNS] Ask iPhone to open app and tap Re-register")
        return {"ok": False, "error": "no device token - register iPhone first"}

    # Normalize command to upper (keep original for mapping, but APNs payload uses raw)
    print(f"[MQTT] Received BCI command: {command}")
    print(f"[MQTT] Confidence: {confidence}")
    print(f"[APNS] Preparing background push for: {command}")
    print("[APNS] Device token found")

    payload = {
        "aps": {
            "content-available": 1
        },
        "bci_command": command,
        "confidence": confidence,
        "timestamp": str(timestamp) if timestamp else datetime.now(timezone.utc).isoformat(),
    }
    if url:
        payload["url"] = url

    print("[APNS] Sending request to APNs")
    print(f"[APNS] Payload: {json.dumps(payload)}")
    ok, resp = send_push_sync(token, payload)
    if ok:
        print(f"[APNS] Response status: 200 ({resp})")
        return {"ok": True, "apns": resp}
    else:
        print("[APNS] Push failed")
        print(f"[APNS] Response: {resp}")
        return {"ok": False, "error": resp}

# MQTT callbacks
def on_connect(client, userdata, flags, rc):
    if rc == 0:
        print(f"[MQTT] Connected to {MQTT_HOST}:{MQTT_PORT}")
        client.subscribe(MQTT_TOPIC, qos=1)
        print(f"[MQTT] Subscribed to {MQTT_TOPIC}")
    else:
        print(f"[MQTT] Connect failed rc={rc}")

def on_message(client, userdata, msg):
    try:
        raw = msg.payload.decode('utf-8')
        print(f"[MQTT] Raw: {raw[:200]}")
        data = json.loads(raw)
        cmd = data.get("command") or data.get("bci_command") or "UNKNOWN"
        conf = data.get("confidence", 0.99)
        ts = data.get("timestamp") or datetime.now(timezone.utc).isoformat()
        url = data.get("url")
        forward_to_apns(cmd, conf, ts, url)
    except Exception as e:
        print(f"[MQTT] Error handling message: {e}")

def start_mqtt_thread():
    global mqtt_client
    client = mqtt.Client()
    client.on_connect = on_connect
    client.on_message = on_message
    try:
        client.connect(MQTT_HOST, MQTT_PORT, 60)
        client.loop_forever()
    except Exception as e:
        print(f"[MQTT] Loop error: {e}")

if __name__ == "__main__":
    # Start MQTT in background thread
    t = threading.Thread(target=start_mqtt_thread, daemon=True)
    t.start()
    print(f"[Gateway] Starting http on 0.0.0.0:8080")
    print(f"[Gateway] MQTT {MQTT_HOST} -> APNs")
    # Flask
    app.run(host="0.0.0.0", port=8080, debug=False)
