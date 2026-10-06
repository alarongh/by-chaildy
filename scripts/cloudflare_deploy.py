"""Deploy only on Workers Free; copy credentials over stdin and migrate SQLite privately.

No secrets, API response bodies or customer records are printed. The original
polling process is stopped only by the explicitly requested cutover action.
"""
import argparse
import json
import os
from pathlib import Path
import re
import secrets
import shutil
import sqlite3
import ssl
import subprocess
import sys
import time
import urllib.error
import urllib.request

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from dotenv import dotenv_values
import certifi
from bot.config import Settings

CLOUD = ROOT / "cloudflare"
STATE = ROOT / "data" / "cloudflare-runtime.json"
PLAN_CHECK = ROOT / "data" / "cloudflare-plan-verification.json"
CLI = CLOUD / "node_modules" / "wrangler" / "bin" / "wrangler.js"
TABLES = ("sessions", "bookings", "outbox", "sent_messages")


def command(*args, payload=None, account=None):
    run_env = os.environ.copy()
    run_env["WRANGLER_SEND_METRICS"] = "false"
    if account:
        run_env["CLOUDFLARE_ACCOUNT_ID"] = account
    result = subprocess.run([shutil.which("node") or "node", str(CLI), *args], cwd=CLOUD,
                            env=run_env, input=payload, capture_output=True, text=True,
                            encoding="utf-8", errors="replace", timeout=180)
    if result.returncode:
        # Wrangler failures can include request data; never echo the raw output.
        raise RuntimeError("Cloudflare command failed: " + " ".join(args[:2]))
    return result.stdout


def request(url, method="GET", body=None, headers=None):
    payload = json.dumps(body, ensure_ascii=False).encode("utf-8") if body is not None else None
    req = urllib.request.Request(url, data=payload, method=method,
                                 headers={"Content-Type": "application/json",
                                          "User-Agent": "chaildy-deployment/1.0", **(headers or {})})
    try:
        with urllib.request.urlopen(req, timeout=35,
                                    context=ssl.create_default_context(cafile=certifi.where())) as response:
            return json.loads(response.read())
    except urllib.error.HTTPError as error:
        raise RuntimeError("Remote request failed: HTTP " + str(error.code)) from None
    except (urllib.error.URLError, TimeoutError):
        raise RuntimeError("Remote request timed out or is unavailable") from None


def state():
    return json.loads(STATE.read_text(encoding="utf-8")) if STATE.exists() else {}


def save(value):
    STATE.parent.mkdir(parents=True, exist_ok=True)
    temporary = STATE.with_suffix(".tmp")
    temporary.write_text(json.dumps(value, indent=2)+"\n", encoding="utf-8")
    temporary.replace(STATE)


def cloud_api(value, operation, body=None):
    return request(value["url"]+"/_admin/"+operation,
                   "POST" if body is not None else "GET", body,
                   {"X-Deployment-Secret": value["deploy_secret"]})


def telegram(cfg, method, body=None):
    result = request("https://api.telegram.org/bot"+cfg.token+"/"+method,
                     "POST" if body is not None else "GET", body)
    if not result.get("ok"):
        raise RuntimeError("Telegram rejected the requested operation")
    return result["result"]


def free_account():
    try:
        identity = json.loads(command("whoami", "--json"))
        auth = json.loads(command("auth", "token", "--json"))
    except (RuntimeError, json.JSONDecodeError):
        raise RuntimeError("Cloudflare CLI sign-in required. Run wrangler login and finish authorization yourself.") from None
    accounts = identity.get("accounts", [])
    selected = state().get("account") or os.getenv("CLOUDFLARE_ACCOUNT_ID")
    if not selected:
        if len(accounts) != 1:
            raise RuntimeError("Select one owned Cloudflare account using CLOUDFLARE_ACCOUNT_ID.")
        selected = accounts[0]["id"]
    if selected not in {a["id"] for a in accounts} or not auth.get("token"):
        raise RuntimeError("Selected account is not available to this authorization.")
    try:
        subscriptions = request("https://api.cloudflare.com/client/v4/accounts/"+selected+"/subscriptions",
                                headers={"Authorization": "Bearer "+auth["token"]})
    except RuntimeError as error:
        # Narrow Wrangler OAuth does not include billing. Accept a recent,
        # account-specific dashboard observation instead of requesting more access.
        check = json.loads(PLAN_CHECK.read_text(encoding="utf-8")) if PLAN_CHECK.exists() else {}
        expected_url = "https://dash.cloudflare.com/"+selected+"/workers/plans"
        age = time.time() - float(check.get("verified_at", 0))
        if (str(error) == "Remote request failed: HTTP 403" and
                check.get("account") == selected and check.get("plan") == "free" and
                check.get("source") == expected_url and 0 <= age <= 3600):
            return selected
        raise RuntimeError("Could not verify Workers Free; inspect the current plan in the dashboard.") from None
    if not subscriptions.get("success") or not isinstance(subscriptions.get("result"), list):
        raise RuntimeError("Could not verify Workers Free; deployment stopped.")
    for item in subscriptions["result"]:
        # No billing mutations are ever issued. Stop on any paid Workers subscription.
        plan = item.get("rate_plan", {})
        description = json.dumps(plan).lower()
        if "worker" in description and ("free" not in description or float(item.get("price") or 0) > 0):
            raise RuntimeError("Account has a paid Workers subscription; choose a Workers Free account.")
    return selected


def deploy():
    cfg = Settings.from_env()
    if not cfg.token or not cfg.can_collect:
        raise RuntimeError("Local production bot configuration is incomplete.")
    account = free_account()
    value = state()
    value.setdefault("webhook_secret", secrets.token_urlsafe(36))
    value.setdefault("deploy_secret", secrets.token_urlsafe(36))
    value["account"] = account
    output = command("deploy", account=account)
    urls = re.findall(r"https://by-chaildy-bot\.[a-z0-9-]+\.workers\.dev", output)
    if not urls and not value.get("url"):
        raise RuntimeError("Deployment did not return a verified workers.dev URL.")
    value["url"] = urls[-1] if urls else value["url"]
    save(value)
    local = dotenv_values(ROOT / ".env")
    keys = ("BOT_TOKEN", "ADMIN_IDS", "MASTER_TELEGRAM", "CITY", "OPERATOR_NAME",
            "OPERATOR_ADDRESS", "PRIVACY_CONTACT", "TIMEZONE", "RETENTION_DAYS", "PRIVACY_READY")
    credentials = {k: str(local.get(k) or "") for k in keys}
    credentials.update(WEBHOOK_SECRET=value["webhook_secret"], DEPLOY_SECRET=value["deploy_secret"], TEST_MODE="false")
    command("secret", "bulk", payload=json.dumps(credentials), account=account)
    result = cloud_api(value, "status")
    if not result.get("configured"):
        raise RuntimeError("Cloud deployment exists but configuration verification failed.")
    print("Free cloud service deployed; local polling unchanged.")
    print(value["url"])


def freeze_local():
    if os.name != "nt":
        raise RuntimeError("This migration expects the tracked Windows polling process.")
    # Verify the PID and every child against this exact checkout before stopping anything.
    # Values are passed via environment, not interpolated into PowerShell code.
    script = r'''
    $pidFile = Join-Path $env:CHAILDY_ROOT 'data\bot.pid'
    if (-not (Test-Path -LiteralPath $pidFile)) { exit 0 }
    $tracked = [int](Get-Content -LiteralPath $pidFile)
    $process = Get-CimInstance Win32_Process -Filter "ProcessId=$tracked"
    if ($null -eq $process) { exit 0 }
    $expected = Join-Path $env:CHAILDY_ROOT '.venv\Scripts\python.exe'
    if ($process.CommandLine -notlike ('*' + $expected + '* -m bot.main*')) { exit 3 }
    $children = Get-CimInstance Win32_Process | Where-Object {
        $_.ParentProcessId -eq $tracked -and $_.Name -eq 'python.exe' -and
        $_.CommandLine -like ('*' + $expected + '* -m bot.main*')
    }
    foreach ($child in $children) { Stop-Process -Id $child.ProcessId -ErrorAction SilentlyContinue }
    Stop-Process -Id $tracked -ErrorAction SilentlyContinue
    '''
    result = subprocess.run(["powershell", "-NoProfile", "-Command", script],
                            env={**os.environ, "CHAILDY_ROOT": str(ROOT)}, capture_output=True, timeout=30)
    if result.returncode:
        raise RuntimeError("Local PID did not match this bot; no unrelated process was stopped.")


def snapshot(cfg):
    source = Path(cfg.db_path)
    if not source.is_absolute():
        source = ROOT / source
    if not source.exists():
        raise RuntimeError("Working database is missing; refusing an empty replacement.")
    backup = ROOT / "data" / "cloudflare-migration.sqlite3"
    with sqlite3.connect(source) as original, sqlite3.connect(backup) as copy:
        original.backup(copy)
        copy.row_factory = sqlite3.Row
        result = {t: [dict(r) for r in copy.execute("SELECT * FROM "+t)] for t in TABLES}
    return result


def cutover():
    cfg, value = Settings.from_env(), state()
    if not value.get("url"):
        raise RuntimeError("Deploy the cloud service before cutover.")
    result = cloud_api(value, "status")
    if result.get("active"):
        info = telegram(cfg, "getWebhookInfo")
        if info.get("url") == value["url"]+"/webhook":
            print("Cloud bot is already active.")
            return
        raise RuntimeError("Cloud is active with another webhook; review before changing it.")
    if not result.get("configured"):
        raise RuntimeError("Cloud configuration is incomplete; local bot left running.")
    info = telegram(cfg, "getWebhookInfo")
    if info.get("url") and info["url"] != value["url"]+"/webhook":
        raise RuntimeError("Another webhook already exists; local bot left unchanged.")
    if not result.get("imported"):
        freeze_local()
        value["stage"] = "local_frozen"
        save(value)
        data = snapshot(cfg)
        expected = {t: len(data[t]) for t in TABLES}
        value["source_counts"] = expected
        save(value)
        transferred = cloud_api(value, "import", data)
        if any(transferred["counts"][t] != expected[t] for t in TABLES):
            raise RuntimeError("Migration count mismatch; do not activate.")
        value["stage"] = "imported"
        save(value)
    elif value.get("stage") not in ("local_frozen", "imported", "webhook_set", "active"):
        raise RuntimeError("An existing imported snapshot needs review before cutover.")
    telegram(cfg, "setWebhook", {"url": value["url"]+"/webhook", "secret_token": value["webhook_secret"],
                                 "max_connections": 1, "allowed_updates": ["message", "callback_query"],
                                 "drop_pending_updates": False})
    value["stage"] = "webhook_set"
    save(value)
    cloud_api(value, "activate", {})
    value["stage"] = "active"
    save(value)
    info = telegram(cfg, "getWebhookInfo")
    health = request(value["url"]+"/health")
    if info.get("url") != value["url"]+"/webhook" or not health.get("active"):
        raise RuntimeError("Activation verification incomplete; polling stays stopped to avoid duplicates.")
    print("Cloud bot active; webhook verified; local polling stopped. Computer can be switched off.")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("action", choices=("auth-check", "deploy", "status", "cutover"))
    args = parser.parse_args()
    os.chdir(ROOT)
    if args.action == "auth-check":
        free_account()
        print("Authenticated Workers Free account verified.")
    elif args.action == "deploy":
        deploy()
    elif args.action == "cutover":
        cutover()
    elif state().get("url"):
        print(json.dumps(cloud_api(state(), "status")))
    else:
        print("Cloud bot not deployed. Local runtime remains unchanged.")


if __name__ == "__main__":
    try:
        main()
    except Exception as error:
        # Even unexpected network/subprocess exceptions might embed credentials.
        print(str(error) if isinstance(error, RuntimeError) else "Operation failed: "+type(error).__name__)
        raise SystemExit(1)
