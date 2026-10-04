"""Publish the prepared repository using Git Credential Manager. Never print credentials."""
import argparse
import json
from pathlib import Path
import subprocess
import sys
import urllib.error
import urllib.request

ROOT = Path(__file__).resolve().parents[1]
OWNER = "alarongh"
REPO = "by-chaildy"


def credential():
    result = subprocess.run(["git", "credential", "fill"], input="protocol=https\nhost=github.com\n\n",
                            capture_output=True, text=True, encoding="utf-8", timeout=40)
    if result.returncode:
        raise SystemExit("GitHub credentials unavailable. Interactive authentication is required.")
    fields = dict(line.split("=", 1) for line in result.stdout.splitlines() if "=" in line)
    secret = fields.get("password")
    if not secret:
        raise SystemExit("GitHub credential helper returned no access token.")
    return secret


def request(token, path, method="GET", data=None):
    body = json.dumps(data).encode() if data is not None else None
    req = urllib.request.Request("https://api.github.com"+path, data=body, method=method,
                                 headers={"Authorization": "Bearer "+token,
                                          "Accept": "application/vnd.github+json",
                                          "X-GitHub-Api-Version": "2022-11-28", "User-Agent": "chaildy-publisher",
                                          "Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=30) as response:
            raw = response.read()
            return response.status, json.loads(raw) if raw else {}
    except urllib.error.HTTPError as error:
        # Explicitly avoid dumping headers, request bodies or tokens.
        return error.code, {}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("action", choices=["check", "publish", "status"])
    args = parser.parse_args()
    token = credential()
    status, user = request(token, "/user")
    if status != 200 or user.get("login") != OWNER:
        raise SystemExit("Git credential account does not match verified GitHub profile alarongh.")
    print("Authenticated GitHub account:", user["login"])
    if args.action == "check":
        status, repo = request(token, f"/repos/{OWNER}/{REPO}")
        print("Target repository:", "available to create" if status == 404 else f"HTTP {status}; already exists")
        return
    if args.action == "status":
        status, pages = request(token, f"/repos/{OWNER}/{REPO}/pages")
        print(json.dumps({"pages_status":status,"url":pages.get("html_url"),"build_type":pages.get("build_type")}, ensure_ascii=False))
        status, runs = request(token, f"/repos/{OWNER}/{REPO}/actions/runs?per_page=6")
        print(json.dumps({"runs_status":status,"runs":[{k:r.get(k) for k in ["id","name","status","conclusion","html_url","head_sha"]} for r in runs.get("workflow_runs",[])]}))
        return
    status, repo = request(token, f"/repos/{OWNER}/{REPO}")
    if status == 404:
        status, repo = request(token, "/user/repos", "POST", {
            "name":REPO,"description":"by:Chaildy - tattoo booking bot and website style preview",
            "private":False,"auto_init":False})
        if status != 201:
            raise SystemExit(f"Repository creation rejected: HTTP {status}")
        print("Created:", repo["html_url"])
    elif status == 200:
        remote = subprocess.run(["git","remote","get-url","origin"], cwd=ROOT, capture_output=True, text=True)
        expected = f"https://github.com/{OWNER}/{REPO}.git"
        if remote.returncode or remote.stdout.strip() != expected:
            raise SystemExit("Repository already exists and is not attached to this checkout. Refusing to overwrite.")
    else:
        raise SystemExit(f"Repository lookup failed: HTTP {status}")
    remote = subprocess.run(["git","remote","get-url","origin"], cwd=ROOT, capture_output=True, text=True)
    if remote.returncode:
        subprocess.run(["git","remote","add","origin",f"https://github.com/{OWNER}/{REPO}.git"],cwd=ROOT,check=True)
    subprocess.run(["git","push","-u","origin","main"], cwd=ROOT, check=True)
    status, pages = request(token, f"/repos/{OWNER}/{REPO}/pages")
    if status == 404:
        status, pages = request(token, f"/repos/{OWNER}/{REPO}/pages", "POST", {"build_type":"workflow"})
        if status not in (201, 200):
            raise SystemExit(f"Pages setup rejected: HTTP {status}. Repository is saved.")
    elif status != 200:
        raise SystemExit(f"Pages lookup failed: HTTP {status}")
    elif pages.get("build_type") != "workflow":
        raise SystemExit("Existing Pages uses another build type; leaving it unchanged.")
    # Dispatch only after Pages exists so configure-pages succeeds on first deployment.
    status, _ = request(token, f"/repos/{OWNER}/{REPO}/actions/workflows/pages.yml/dispatches", "POST", {"ref":"main"})
    print("Pages workflow dispatch:", status)
    print("Website:", f"https://{OWNER}.github.io/{REPO}/")


if __name__ == "__main__":
    main()
