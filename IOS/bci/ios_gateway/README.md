# iOS Gateway — MQTT → APNs

Experimental gateway: listens to `bci/rohan/commands` on `broker.emqx.io:1883` and forwards as APNs background pushes (`content-available:1`).

```
BCI/Master Hub → MQTT → ios_gateway → APNs → iPhone → Shortcut → JioSaavn
```

## Setup

1. **Apple Developer**
   - Create APNs Auth Key (.p8) with Key ID
   - Note Team ID, Bundle ID `com.test.bci`
   - Enable **Push Notifications** capability in Xcode (already done: `bci.entitlements` aps-environment=development, UIBackgroundModes remote-notification)

2. **Environment variables** (do NOT commit .p8):
```bash
export APNS_TEAM_ID="YOUR_TEAM_ID"
export APNS_KEY_ID="YOUR_KEY_ID"
export APNS_BUNDLE_ID="com.test.bci"
export APNS_AUTH_KEY_PATH="/path/to/AuthKey_XXXX.p8"
export APNS_ENVIRONMENT="development"  # or production
```

3. **Install & run**
```bash
cd ios_gateway
python3 -m venv venv && source venv/bin/activate
pip install -r requirements.txt
python gateway.py
# Listens on 0.0.0.0:8080
```

Endpoints:
- `POST /register-device` `{"device_token":"<hex>","environment":"development"}` — iPhone calls automatically via `APNSManager`
- `GET /health` — token status
- `POST /test-push` `{"bci_command":"NEXT","confidence":0.99}` — manual test without MQTT

Storage: `apns_device.json` (gitignored) holds current token.

## iPhone
- Open app → APNs auto-registers, shows `Token: abc...` and `Gateway: http://<PC_IP>:8080` (edit field to PC LAN IP, e.g. `http://192.168.1.50:8080`, then `Register with Gateway`)
- Set delivery mode to `APNs (background)`; keep `Direct MQTT` as fallback (avoid both simultaneously — duplicate commands)
- Keep app foregrounded for Test A, background for Test B, force-quit for Test C

## Flow
MQTT `{"command":"NEXT","confidence":0.99,"timestamp":"..."}`
→ gateway `forward_to_apns` →
```json
{"aps":{"content-available":1},"bci_command":"NEXT","confidence":0.99,"timestamp":"..."}
```
→ `api.development.push.apple.com` with JWT (`ES256`, `apns-push-type: background`, `priority:5`, `topic: com.test.bci`)
→ `AppDelegate.didReceiveRemoteNotification` → `APNSCommandProcessor` → `CommandValidator` → `CommandExecutor` → `ShortcutService` (`shortcuts://run-shortcut?name=JioSaavn%20Next`) → JioSaavn

**Note:** Background `UIApplication.open` success is **best-effort**; iOS may throttle/delay `content-available` pushes and force-quit discards them. Phase 1 measures each of the 7 stages via logs.

## Security
- `.p8` added to `.gitignore`
- No secrets in repo; use env vars
- `apns_device.json` gitignored

## Test
```bash
curl -X POST http://localhost:8080/test-push -H "Content-Type: application/json" -d '{"bci_command":"NEXT"}'
# check iPhone Xcode console for [APNS] Background notification received
```

## Limitations
- Mock mode if no credentials: gateway logs `Would send to ...` and pretends success for pipeline testing.
