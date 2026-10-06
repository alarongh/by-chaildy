"""Keep cloud prompts and consent aligned with the original Python bot."""
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from bot.config import Settings
from bot.content import FAQ, PREPARATION, consent_text
from bot.flow import PROMPTS

cfg = Settings(operator="{{OPERATOR_NAME}}", address="{{OPERATOR_ADDRESS}}",
               privacy_contact="{{PRIVACY_CONTACT}}", retention_days="{{RETENTION_DAYS}}")
text = consent_text(cfg).replace("2026-10-05.1", "2026-10-05.cloud.1")
text = text.replace("Анкета и ответы передаются через Telegram;", "База заявок размещается в Cloudflare. Анкета и ответы передаются через Telegram;")
target = ROOT / "cloudflare" / "src" / "content.json"
target.parent.mkdir(parents=True, exist_ok=True)
target.write_text(json.dumps({"faq": FAQ, "prepare": PREPARATION, "prompts": PROMPTS,
                              "consent": text, "consentVersion": "2026-10-05.cloud.1"},
                             ensure_ascii=False, indent=2)+"\n", encoding="utf-8")
print("Cloud content exported; no credentials included.")
