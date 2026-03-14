# Hosted App Mobile Quickstart

Use this flow when you want to run `hosted_app` locally and open it from a phone or
tablet on the same LAN.

## 1. Generate local cert files

The repository includes an OpenSSL config template at
`hosted_app/app/certs/openssl-san.cnf`.

Before generating the cert, edit that file and replace `192.168.1.10` with your
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

## 2. Configure the hosted app

```bash
cd hosted_app/app
cp .env.example .env
```

Set the required values in `.env` for your local environment.

## 3. Start the hosted app over HTTPS

```bash
cd hosted_app/app
uv run --project .. uvicorn main:app \
  --host 0.0.0.0 \
  --port 8081 \
  --ssl-keyfile certs/lan-key.pem \
  --ssl-certfile certs/lan-cert.pem
```

## 4. Open it from your phone

Use your computer's LAN IP in the URL:

```text
https://YOUR_LAN_IP:8081/
```

Your browser may ask you to accept the local certificate warning before camera and
microphone access works.

## 5. Quick checks

- `https://YOUR_LAN_IP:8081/health`
- `https://YOUR_LAN_IP:8081/`
