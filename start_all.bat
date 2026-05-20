@echo off
echo Starting PatchBrain...
start "Backend" cmd /k "cd /d C:\Users\jonat\patchbrain\backend && call venv\Scripts\activate && uvicorn main:app --reload --port 8000"
start "Worker" cmd /k "cd /d C:\Users\jonat\patchbrain\backend && call venv\Scripts\activate && python worker.py"
start "Frontend" cmd /k "cd /d C:\Users\jonat\patchbrain\frontend && npm run dev"
echo All systems starting...
pause