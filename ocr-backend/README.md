# AutoGLM OCR Backend

Standalone OCR backend deployment package for sync workers plus an HTTP/HTTPS API.

## Install

```bash
sudo bash install.sh
```

## Pairing a frontend server

The backend can pair with any frontend server at startup.

Edit `/etc/autoglm/ocr-backend.env`:

```bash
AUTOGLM_FRONTEND_BASE_URL=https://frontend.example.com:39283
AUTOGLM_OCR_API_PORT=39384
AUTOGLM_OCR_API_PUBLIC_BASE_URL=https://ocr-backend.example.com:39384
AUTOGLM_OCR_API_AUTH_TOKEN=
```

After changing the target frontend:

```bash
sudo systemctl restart autoglm-ocr-sync
```

## Backend API

The backend service exposes:

- `GET /healthz`
- `GET /status.json`
- `GET /state.json`
- `GET /manifest.json`
- `GET /artifacts/<relative-path>`
- `POST /api/sync-now`

## Uninstall

```bash
sudo bash uninstall.sh
sudo bash uninstall.sh --purge-data --purge-user
```

## TLS

Generate a certificate for the backend HTTPS API:

```bash
sudo bash setup_tls.sh self-signed 192.168.1.20
sudo bash setup_tls.sh letsencrypt ocr.example.com admin@example.com
```

Import an existing certificate and key:

```bash
sudo bash import_tls_cert.sh /path/to/cert-dir
sudo bash import_tls_cert.sh /path/to/cert-dir /path/to/key-dir
```

When `[tls].enabled=true`, the same certificate is used for the backend HTTPS API and optional outbound client auth.

## Runtime paths

- Install dir: `/opt/autoglm-ocr-backend`
- Config file: `/opt/autoglm-ocr-backend/config.ini`
- Env file: `/etc/autoglm/ocr-backend.env`
- Service: `autoglm-ocr-sync`
