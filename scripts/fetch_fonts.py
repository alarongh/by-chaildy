"""Download official Google Fonts Manrope subsets for self-hosted fonts."""
from pathlib import Path
import re
import urllib.request

ROOT = Path(__file__).resolve().parents[1]
url = "https://fonts.googleapis.com/css2?family=Manrope:wght@200..800&display=swap"
req = urllib.request.Request(url, headers={"User-Agent":"Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"})
css = urllib.request.urlopen(req, timeout=30).read().decode()
folder = ROOT / "site" / "assets"
folder.mkdir(exist_ok=True)
for subset in ("latin", "cyrillic"):
    match = re.search(r"/\* "+subset+r" \*/\s*@font-face\s*\{(.*?)\}", css, re.S)
    if not match:
        raise SystemExit(f"Font subset {subset} not available")
    src = re.search(r"url\((https://fonts.gstatic.com/[^)]+)\)", match[1])
    data = urllib.request.urlopen(src[1], timeout=30).read()
    (folder / f"manrope-{subset}.woff2").write_bytes(data)
    print(subset, len(data), "bytes")
license_url = "https://raw.githubusercontent.com/google/fonts/main/ofl/manrope/OFL.txt"
(folder / "Manrope-OFL.txt").write_bytes(urllib.request.urlopen(license_url, timeout=30).read())
