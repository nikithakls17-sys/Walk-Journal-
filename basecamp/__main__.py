"""Run Base Camp and (if cloudflared is installed) open an HTTPS tunnel so the phone's camera + GPS work.

    python -m basecamp              # server + tunnel + QR code
    python -m basecamp --no-tunnel  # server only
"""
import argparse
import re
import shutil
import socket
import subprocess
import sys
import threading

import uvicorn

PORT = 8000
URL_RE = re.compile(r"https://[a-z0-9-]+\.trycloudflare\.com")


def lan_ip() -> str:
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("8.8.8.8", 80))
        return s.getsockname()[0]
    except OSError:
        return "127.0.0.1"
    finally:
        s.close()


def print_qr(url: str):
    try:
        import qrcode
        qr = qrcode.QRCode(border=1)
        qr.add_data(url)
        qr.print_ascii(invert=True)
    except Exception as e:  # a QR code is a nicety; never block startup on it
        print(f"(couldn't draw QR code: {e})")


def banner(url: str, note: str = ""):
    line = "=" * 60
    print(f"\n{line}\n  Walk Journal Base Camp\n  Open on your phone: {url}\n{note}{line}", flush=True)
    print_qr(url)


def start_tunnel():
    exe = shutil.which("cloudflared")
    if not exe:
        banner(f"http://{lan_ip()}:{PORT}",
               "  cloudflared not found, so this is plain HTTP on your Wi-Fi.\n"
               "  Phone camera + GPS need HTTPS. Install cloudflared:\n"
               "    winget install --id Cloudflare.cloudflared   (Windows)\n"
               "    brew install cloudflared                      (macOS)\n")
        return None
    proc = subprocess.Popen([exe, "tunnel", "--no-autoupdate", "--url", f"http://localhost:{PORT}"],
                            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, encoding="utf-8", errors="replace")

    def watch():
        shown = False
        for line in proc.stdout:
            m = URL_RE.search(line)
            if m and not shown:
                shown = True
                banner(m.group(0), "  (HTTPS via Cloudflare quick tunnel. The URL changes each run.)\n")
    threading.Thread(target=watch, daemon=True).start()
    return proc


def main():
    ap = argparse.ArgumentParser(description="Walk Journal Base Camp")
    ap.add_argument("--no-tunnel", action="store_true")
    ap.add_argument("--port", type=int, default=PORT)
    args = ap.parse_args()
    globals()["PORT"] = args.port

    from basecamp import check
    if check.main() != 0:
        print("Base Camp will still start. Quests use the built-in fallback until Gemma is reachable.\n")

    tunnel = None if args.no_tunnel else start_tunnel()
    if args.no_tunnel:
        banner(f"http://localhost:{PORT}")
    try:
        uvicorn.run("basecamp.app.main:app", host="0.0.0.0", port=PORT, log_level="warning")
    finally:
        if tunnel:
            tunnel.terminate()


if __name__ == "__main__":
    sys.exit(main())
