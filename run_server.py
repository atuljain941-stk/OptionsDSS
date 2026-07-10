# run_server.py — production entrypoint for unattended / service operation
#
# app.py's `python app.py` uses Flask's built-in dev server (debug=True),
# which explicitly warns it isn't meant for production and isn't reliable
# for 24/7 unattended runs (memory growth, no real concurrency, no crash
# supervision). This script serves the exact same app via waitress, a
# pure-Python production WSGI server that works the same on Windows/Mac/
# Linux with no extra native dependencies (unlike gunicorn, which needs a
# Unix fork()).
#
# All the background schedulers (daily GEX/watchlist jobs, Signal Notifier,
# Agentic AI Scanner, trade/health alert watchers) start themselves inside
# create_app() regardless of how the app is served — this script doesn't
# change what runs, only how the web server itself is hosted so it can
# stay up unattended. Pair this with install_windows_service_task.ps1 to
# have Windows start this automatically at boot and restart it on crash.
#
# Usage:
#   python run_server.py                     (foreground, Ctrl+C to stop)
#   pythonw run_server.py                    (no console window)
#
# Logs to logs/server.log (rotated daily) in addition to stdout, since a
# service run via Task Scheduler has no visible console to read.

import logging
import os
import sys
from logging.handlers import TimedRotatingFileHandler
from pathlib import Path

ROOT = Path(__file__).resolve().parent
LOG_DIR = ROOT / "logs"
LOG_DIR.mkdir(exist_ok=True)

HOST = os.environ.get("OIAPP_HOST", "0.0.0.0")
PORT = int(os.environ.get("OIAPP_PORT", "5050"))
THREADS = int(os.environ.get("OIAPP_THREADS", "8"))


def _configure_logging():
    fmt = logging.Formatter("%(asctime)s [%(levelname)s] %(name)s: %(message)s")

    file_handler = TimedRotatingFileHandler(
        LOG_DIR / "server.log", when="midnight", backupCount=14, encoding="utf-8"
    )
    file_handler.setFormatter(fmt)

    stream_handler = logging.StreamHandler(sys.stdout)
    stream_handler.setFormatter(fmt)

    root = logging.getLogger()
    root.setLevel(logging.INFO)
    root.addHandler(file_handler)
    root.addHandler(stream_handler)

    # Redirect bare print() calls used throughout the app's startup/watcher
    # code into the log file too, so nothing is only visible in a console
    # window that won't exist when this runs as a service.
    class _PrintToLog:
        def write(self, msg):
            msg = msg.rstrip()
            if msg:
                logging.getLogger("stdout").info(msg)

        def flush(self):
            pass

    sys.stdout = _PrintToLog()
    sys.stderr = _PrintToLog()


def main():
    _configure_logging()
    logging.getLogger(__name__).info(f"Starting oiapp via waitress on {HOST}:{PORT} (threads={THREADS})")

    from oiapp.app_factory import create_app
    from waitress import serve

    app = create_app()
    # channel_timeout is how long waitress will tolerate a connection with
    # no I/O activity before closing it. A full watchlist scan can be a
    # single long synchronous request with no output until it's done, which
    # otherwise looks "idle" to waitress well before it's actually finished.
    serve(app, host=HOST, port=PORT, threads=THREADS, channel_timeout=1800, _quiet=False)


if __name__ == "__main__":
    main()
