import json
import os

STORE_PATH = os.path.join(os.path.dirname(__file__), "apns_device.json")

def save_device(device_token: str, environment: str = "development"):
    data = {
        "device_token": device_token,
        "environment": environment
    }
    with open(STORE_PATH, "w") as f:
        json.dump(data, f, indent=2)
    print(f"[APNS] Device token saved to {STORE_PATH}")

def load_device():
    if not os.path.exists(STORE_PATH):
        return None
    try:
        with open(STORE_PATH, "r") as f:
            return json.load(f)
    except Exception as e:
        print(f"[APNS] Failed to load device token: {e}")
        return None

def get_token():
    data = load_device()
    if data:
        return data.get("device_token")
    return None
