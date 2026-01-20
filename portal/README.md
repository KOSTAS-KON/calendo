# Therapy Archive Portal (FastAPI) - Calendar + Billing + Attendance Colors (Windows)

## Run (PowerShell)

```powershell
python -m venv venv
Set-ExecutionPolicy -Scope Process -ExecutionPolicy Bypass
.\venv\Scripts\Activate.ps1

pip install -r requirements.txt
uvicorn app.main:app --reload
```

Open: http://127.0.0.1:8000

## Notes
- Default DB is SQLite (`therapy.db`).
- If you ran an older version, delete `therapy.db` first because we added billing tables/columns:
  ```powershell
  Remove-Item .\therapy.db
  ```


## Schema update
This version adds `parent_signed_off` and recurring `billing_plans`.
If upgrading from older zip, delete `therapy.db` first.


## Update billing statuses
Go to **Billing** and change Paid / Invoice created / Parent signed off, then click **Save**.


## Billing tabs
Billing has **Display** and **Edit** tabs. Display shows pill-style statuses. Edit lets you change statuses per row.
