# AutoGLM Frontend Server

Standalone frontend deployment package for the AutoGLM upload/file service.

## Install

```bash
sudo bash install.sh
```

## Pairing an OCR backend

The frontend can pair with any OCR backend server at startup.

Edit `/etc/autoglm-frontend/frontend.env`:

```bash
AUTOGLM_BACKEND_NOTIFY_ENABLED=true
AUTOGLM_BACKEND_BASE_URL=https://ocr-backend.example.com:39384
AUTOGLM_BACKEND_AUTH_TOKEN=
AUTOGLM_BACKEND_VERIFY_SSL=true
```

After changing the target backend:

```bash
sudo systemctl restart autoglm-post autoglm-multipart
```

When pairing is enabled, the frontend notifies the backend `POST /api/sync-now`
after a new image/download log is ready.

## Uninstall

```bash
sudo bash uninstall.sh
sudo bash uninstall.sh --purge-data --purge-user
```

## TLS

Generate a server certificate:

```bash
sudo bash setup_tls.sh self-signed 192.168.1.10
sudo bash setup_tls.sh letsencrypt front.example.com admin@example.com
```

Import an existing certificate and key:

```bash
sudo bash import_tls_cert.sh /path/to/cert-dir
sudo bash import_tls_cert.sh /path/to/cert-dir /path/to/key-dir
```

## Runtime paths

- Install dir: `/opt/autoglm-frontend`
- Config file: `/opt/autoglm-frontend/config.ini`
- Env file: `/etc/autoglm-frontend/frontend.env`
- Services: `autoglm-post`, `autoglm-multipart`
