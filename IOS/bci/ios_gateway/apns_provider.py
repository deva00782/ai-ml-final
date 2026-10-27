import os
import time
import json
import jwt  # PyJWT
import hyper  # or use httpx/http2? We'll use httpx with http2, fallback to requests
import httpx

# APNs provider using token-based auth (HTTP/2 + JWT)

def load_config():
    team_id = os.getenv("APNS_TEAM_ID")
    key_id = os.getenv("APNS_KEY_ID")
    bundle_id = os.getenv("APNS_BUNDLE_ID", "com.test.bci")
    auth_key_path = os.getenv("APNS_AUTH_KEY_PATH")
    environment = os.getenv("APNS_ENVIRONMENT", "development")  # development or production
    return team_id, key_id, bundle_id, auth_key_path, environment

def get_apns_host(environment):
    if environment == "production":
        return "api.push.apple.com"
    else:
        return "api.development.push.apple.com"

_cached_token = None
_cached_token_time = 0

def generate_jwt(team_id, key_id, auth_key_path):
    global _cached_token, _cached_token_time
    # Cache for ~50 minutes (APNs allows max 1 hour)
    if _cached_token and time.time() - _cached_token_time < 3000:
        return _cached_token
    if not team_id or not key_id or not auth_key_path:
        print("[APNS] Missing APNS_TEAM_ID / APNS_KEY_ID / APNS_AUTH_KEY_PATH env vars")
        print("[APNS] Set them before running gateway")
        return None
    if not os.path.exists(auth_key_path):
        print(f"[APNS] Auth key file not found: {auth_key_path}")
        return None
    with open(auth_key_path, "r") as f:
        private_key = f.read()
    header = {"alg": "ES256", "kid": key_id}
    payload = {"iss": team_id, "iat": int(time.time())}
    token = jwt.encode(payload, private_key, algorithm="ES256", headers=header)
    _cached_token = token
    _cached_token_time = time.time()
    return token

async def send_push(device_token: str, payload: dict):
    team_id, key_id, bundle_id, auth_key_path, environment = load_config()
    if not device_token:
        print("[APNS] No device token, skipping push")
        return False, "no token"

    jwt_token = generate_jwt(team_id, key_id, auth_key_path)
    if not jwt_token:
        # In development without real credentials, log and simulate success
        print("[APNS] No JWT - running in MOCK mode (no real APNs credentials)")
        print(f"[APNS] Would send to {device_token[:8]}...: {json.dumps(payload)}")
        return True, "mock"

    host = get_apns_host(environment)
    url = f"https://{host}:443/3/device/{device_token}"
    headers = {
        "authorization": f"bearer {jwt_token}",
        "apns-topic": bundle_id,
        "apns-push-type": "background",
        "apns-priority": "5",
        "content-type": "application/json"
    }
    # APNs expects content-available background pushes with priority 5
    try:
        async with httpx.AsyncClient(http2=True) as client:
            resp = await client.post(url, headers=headers, json=payload)
            print(f"[APNS] Response status: {resp.status_code}")
            if resp.status_code == 200:
                print("[APNS] Push sent successfully")
                return True, "ok"
            else:
                body = resp.text[:500]
                print(f"[APNS] Push failed HTTP {resp.status_code}: {body}")
                return False, body
    except Exception as e:
        print(f"[APNS] Exception sending push: {e}")
        return False, str(e)

# Sync wrapper for Flask gateway
def send_push_sync(device_token: str, payload: dict):
    import asyncio
    try:
        loop = asyncio.get_event_loop()
    except RuntimeError:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
    return loop.run_until_complete(send_push(device_token, payload))
