# SMS3 Developer Notes

## Quick start
From the project root:

    tools\dev_run.ps1

This will:
- create `venv/` if missing
- install requirements
- start the Streamlit app

## Reset local data
    tools\dev_reset_data.ps1

This backs up current CSVs to `data/_backup/<timestamp>/` and recreates empty CSVs with headers.

## VS Code
Use `.vscode/launch.json` -> "Run Streamlit (sms3)".
