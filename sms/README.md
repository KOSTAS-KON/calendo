\
# sms3 — Calendar + Send Center (Queue-only, CSV-based)

## Why you hit the install error
You are likely using **Python 3.9.7** (or Streamlit from **Anaconda** on PATH).
Many modern Streamlit packages **do not ship wheels for Python 3.9.7**, and Anaconda’s `streamlit.exe`
may load a broken `pyarrow` DLL.

✅ Recommended: **Python 3.11 (or 3.10+)** + a clean `venv`.

---

## Setup (PowerShell)
```powershell
cd C:\Users\Kostas\Documents\team\kostas\sms3

# Use Python 3.10+ (recommended 3.11)
python -V

python -m venv venv
.\venv\Scripts\Activate.ps1
pip install -r .\requirements.txt
```

## Configure Infobip
```powershell
$env:INFOBIP_API_KEY = "YOUR_KEY"
$env:INFOBIP_BASEURL = "YOUR_BASEURL"  # e.g. 4kvd9p.api.infobip.com
```

## Run the app (IMPORTANT: use venv python)
```powershell
.\tools\run_demo.ps1
```

## Send queued SMS from outbox
```powershell
python -m src.calendar.send_outbox
```

## Data files
- data/calendar/customers.csv
- data/calendar/appointments.csv
- data/calendar/outbox.csv


## Scheduler (automatic reminders)

This project can automatically queue reminders like **1 day before** or **2 hours before** an appointment.

### Run once (queue-only)
```powershell
.\tools\run_scheduler_once.ps1
```

### Windows Task Scheduler (every 5 minutes)
```powershell
Unblock-File .\tools\install_windows_tasks.ps1
.\tools\install_windows_tasks.ps1
```

The scheduler reads `config/scheduler.yaml` and writes queued messages into:
- `data/calendar/outbox.csv`

Sending is performed by:
- `python -m src.calendar.send_outbox`


## First-run Setup Wizard (Infobip)

On first launch, the app will **block** until you either:
- Configure Infobip in the Setup Wizard (saved to `config/secrets.yaml`), or
- Continue in Queue-only mode (no live sending).

You can reopen the wizard from the sidebar: **Open Setup Wizard**.
