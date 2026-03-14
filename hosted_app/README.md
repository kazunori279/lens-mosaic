# Hosted App

`hosted_app` is the Cloud Run service for LensMosaic.

It currently serves:

- static UI assets
- public search endpoints
- item detail endpoints for the UI
- hosted live WebSocket endpoints for the quick demo

This app is intended to support both:

1. same-origin hosted demo mode
2. hosted UI plus local live backend mode

## Mobile Local Test

Use this flow when you want to run `hosted_app` locally and open it from a phone or
tablet on the same LAN.

## 1. Configure the app

```bash
cd hosted_app/app
cp .env.example .env
```

Set the required values in `.env` for your local environment.

## 2. Run the direct model preflight

Before starting the server, verify that the Live model itself is responding from this
machine:

```bash
uv run --project hosted_app python hosted_app/model_test.py --timeout 60
```

This probe runs:

- a Vertex AI text test
- a Vertex AI audio test
- a Gemini API text test, if `GEMINI_API_KEY` or `GOOGLE_API_KEY` is set
- a Gemini API audio test, if `GEMINI_API_KEY` or `GOOGLE_API_KEY` is set

Use this step to separate model/provider latency from app/server latency before
debugging `uvicorn` or browser behavior.

## 3. Clear port 8081 if needed

If port `8081` is already in use, stop the existing process first:

```bash
lsof -nP -iTCP:8081 -sTCP:LISTEN
kill <PID>
```

## 4. Generate local cert files

The repository includes an OpenSSL config template at
`hosted_app/app/certs/openssl-san.cnf`.

Before generating the cert, edit that file and replace the sample LAN IP with your
computer's actual LAN IP address.

Then run:

```bash
cd hosted_app/app
mkdir -p certs
openssl req -x509 -nodes -days 365 -newkey rsa:2048 \
  -keyout certs/lan-key.pem \
  -out certs/lan-cert.pem \
  -config certs/openssl-san.cnf \
  -extensions req_ext
```

These generated cert files are ignored by git and should stay local to your machine.

## 5. Start the hosted app over HTTPS

For phone or tablet testing, the HTTPS server must be started with `--host 0.0.0.0`
so the app is reachable from other devices on your LAN.

```bash
cd hosted_app/app
uv run --project .. uvicorn main:app \
  --host 0.0.0.0 \
  --port 8081 \
  --ssl-keyfile certs/lan-key.pem \
  --ssl-certfile certs/lan-cert.pem
```

## 6. Verify from your Mac

Check the LAN URL before moving to the phone:

```bash
curl -k https://YOUR_LAN_IP:8081/health
```

## 7. Open it from your phone

Use your computer's LAN IP in the URL:

```text
https://YOUR_LAN_IP:8081/
```

Open the URL directly in Safari once before relying on the QR code, so you can accept
the local certificate warning explicitly.

## 8. Generate a QR code

After the HTTPS server is running, you can generate a QR code for the phone URL:

```bash
python3 - <<'PY'
from urllib.parse import quote
from urllib.request import urlopen
from pathlib import Path

url = "https://YOUR_LAN_IP:8081/"
qr_url = f"http://api.qrserver.com/v1/create-qr-code/?size=480x480&data={quote(url, safe='')}"
out = Path("/tmp/lens-mosaic-hosted-app-mobile-qr.png")
out.write_bytes(urlopen(qr_url, timeout=20).read())
print(out)
print(url)
PY
```

Open the generated PNG at `/tmp/lens-mosaic-hosted-app-mobile-qr.png`, show it to the
user on your desktop, and let the user scan it from their smartphone.

After scanning, have the user open the hosted app on their smartphone and test the page
load, camera permission, microphone permission, and live connection flow.

## 9. Notes

- Verify the LAN URL from your Mac first with `curl -k https://YOUR_LAN_IP:8081/health`.
- If `model_test.py` is already slow, fix the provider/model issue before debugging the
  hosted app server.
- If the phone can reach the page but live mode disconnects quickly, check the server log
  separately from the basic LAN/HTTPS setup. A page load proves the LAN path is working.
- For the hosted UI plus `local_live` workflow, see
  [docs/local-reader-quickstart.md](/Users/kaz/Documents/GitHub/lens-mosaic/docs/local-reader-quickstart.md).
