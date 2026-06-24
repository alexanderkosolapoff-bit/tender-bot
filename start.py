import threading
import subprocess
import sys
import os

def run_bot():
    subprocess.run([sys.executable, "bot.py"])

def run_api():
    port = os.environ.get("PORT", "8000")
    subprocess.run([
        sys.executable, "-m", "uvicorn",
        "web_api:app",
        "--host", "0.0.0.0",
        "--port", port
    ])

t1 = threading.Thread(target=run_bot, daemon=True)
t2 = threading.Thread(target=run_api)
t1.start()
t2.start()
t2.join()
