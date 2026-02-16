# Render deployment notes (Portal + SMS)

This repository contains **two** deployable web apps:

1) **Portal** (FastAPI) – clinic suite: children, therapists, calendar, billing, SMS outbox.
2) **SMS Calendar** (Streamlit) – flashy calendar UI + SMS tooling.

Your Render project screenshots show these resources:
- `calendo-db` (Postgres)
- `calendo` (Portal) – web service
- `calendo-sms` (SMS Calendar) – web service
- (Optional) a cron job for SMS outbox

The most common Render deploy pitfall is **starting from the repo root** while the
Portal expects to import the `app` package and find `app/static` + `app/templates`.
This repo includes start scripts that work regardless of the working directory.

---

## Recommended Render configuration

You can deploy with either:

### Option A (simplest): keep the service root as the repository root

**Portal service (`calendo`)**
- Build command:
  ```bash
  pip install -r portal/requirements.txt
  ```
- Start command:
  ```bash
  bash render_start_portal.sh
  ```

**SMS service (`calendo-sms`)**
- Build command:
  ```bash
  pip install -r sms/requirements.txt
  ```
- Start command:
  ```bash
  bash render_start_sms.sh
  ```

### Option B: set the Render **Root Directory** per service

If you prefer, you can set:
- Portal root directory: `portal`
- SMS root directory: `sms`

Then you can use the Dockerfiles in each folder **or** use the native Python environment.

---

## Environment variables (match your screenshots)

### Portal (`calendo`)

Required:
- `DATABASE_URL`
- `SECRET_KEY`
- `INTERNAL_API_KEY`
- `SSO_SHARED_SECRET`

Common/Optional:
- `ADMIN_KEY`
- `BOOTSTRAP_OWNER_EMAIL`
- `BOOTSTRAP_OWNER_PASSWORD`
- `SMS_APP_URL` (e.g. `https://<your-sms>.onrender.com/sms/`)
- `TURNSTILE_ENABLED`, `TURNSTILE_SITE_KEY`, `TURNSTILE_SECRET_KEY`, `TURNSTILE_TIMEOUT_SECONDS`

### SMS Calendar (`calendo-sms`)

Required:
- `PORTAL_BASE_URL` (e.g. `https://<your-portal>.onrender.com`)
- `INTERNAL_API_KEY`
- `SSO_SHARED_SECRET`

Optional:
- `PORTAL_APP_URL` (UI links)
- `INTERNAL_TOKEN` (legacy fallback)
- `SMS_PROVIDER` (`infobip` or `mock`)
- `INFOBIP_BASE_URL`, `INFOBIP_API_KEY`, `INFOBIP_FROM`

---

## Troubleshooting

### Portal fails to boot with “Directory 'app/static' does not exist”
Use `bash render_start_portal.sh` **or** make sure the service Root Directory is `portal`.

### Portal fails with “Weak SESSION_SECRET/SECRET_KEY detected”
Set a strong `SECRET_KEY` (>= 32 chars) in Render environment variables.

### Postgres URL starts with `postgres://`
This repo normalizes it to `postgresql://` automatically.
