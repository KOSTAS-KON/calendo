# Render Cron Job for SMS Outbox (every 5 minutes)

To enable fully automatic sending of queued SMS reminders and "send now" messages:

## 1) Create a Cron Job in Render
- Render Dashboard → **New** → **Cron Job**
- Repo: this same GitHub repository
- Branch: `main`
- Command:
  ```bash
  python sms/tools/run_outbox_once.py
  ```
- Schedule: **Every 5 minutes**

## 2) Environment variables (copy from calendo-sms service)
Set the same SMS-related environment variables as your `calendo-sms` web service:

Required for Infobip sending:
- `SMS_PROVIDER=infobip`
- `INFOBIP_BASE_URL=https://<your_base>.api.infobip.com`
- `INFOBIP_API_KEY=...`
- `INFOBIP_FROM=...`

Required to talk to the Portal internal APIs:
- `PORTAL_BASE_URL=https://calendo-3ktr.onrender.com` (or your portal URL)
- `INTERNAL_API_KEY=...` (must match Portal)
- `SSO_SHARED_SECRET=...` (must match Portal)

Optional:
- `APP_TIMEZONE=Europe/Athens`

## 3) Verify
- In SMS UI → Outbox: queued reminders should automatically move to `sent` within 5 minutes when due.
- In Render cron logs: you should see `OUTBOX: due=... sent=... failed=...`
