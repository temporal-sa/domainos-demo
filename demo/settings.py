"""Environment-driven settings. Pure Python: safe to import anywhere."""

import os
from pathlib import Path

try:  # optional; lets people keep settings in a .env file
    from dotenv import load_dotenv

    load_dotenv(Path(__file__).resolve().parent.parent / ".env")
except Exception:  # pragma: no cover
    pass

ROOT = Path(__file__).resolve().parent.parent
CONFIG_DIR = Path(os.getenv("DEMO_CONFIG_DIR", ROOT / "config"))
RUNTIME_DIR = Path(os.getenv("DEMO_RUNTIME_DIR", ROOT / "runtime"))
ANSIBLE_DIR = Path(os.getenv("DEMO_ANSIBLE_DIR", ROOT / "ansible"))

# --- Temporal -------------------------------------------------------------
TEMPORAL_ADDRESS = os.getenv("TEMPORAL_ADDRESS", "localhost:7233")
TEMPORAL_NAMESPACE = os.getenv("TEMPORAL_NAMESPACE", "default")
TEMPORAL_API_KEY = os.getenv("TEMPORAL_API_KEY", "")
TEMPORAL_TLS_CERT = os.getenv("TEMPORAL_TLS_CERT", "")  # path to client cert (mTLS)
TEMPORAL_TLS_KEY = os.getenv("TEMPORAL_TLS_KEY", "")
TEMPORAL_TLS_CA = os.getenv("TEMPORAL_TLS_CA", "")  # optional server CA (self-hosted)

CORE_QUEUE = os.getenv("CORE_TASK_QUEUE", "vending-core")
ONPREM_QUEUE = os.getenv("ONPREM_TASK_QUEUE", "vending-onprem")
WORKFLOW_ID_PREFIX = os.getenv("WORKFLOW_ID_PREFIX", "vend-")


def is_temporal_cloud() -> bool:
    return "tmprl.cloud" in TEMPORAL_ADDRESS or "temporal.io" in TEMPORAL_ADDRESS


def temporal_ui_base() -> str:
    explicit = os.getenv("TEMPORAL_UI_URL")
    if explicit:
        return explicit.rstrip("/")
    if is_temporal_cloud():
        return "https://cloud.temporal.io"
    host = TEMPORAL_ADDRESS.split(":")[0]
    return f"http://{host}:8233"


# --- Claude ---------------------------------------------------------------
ANTHROPIC_MODEL = os.getenv("ANTHROPIC_MODEL", "claude-sonnet-5-5")
ANTHROPIC_EFFORT = os.getenv("ANTHROPIC_EFFORT", "low")  # low keeps demo latency down
AI_DISABLED = os.getenv("DEMO_AI_DISABLED", "").lower() in {"1", "true", "yes"}


def ai_configured() -> bool:
    return bool(os.getenv("ANTHROPIC_API_KEY")) and not AI_DISABLED


# --- Demo -----------------------------------------------------------------
BRAND_NAME = os.getenv("DEMO_BRAND", "Domainos")
BRAND_PRODUCT = os.getenv("DEMO_PRODUCT", "Cloud account tracker")
# The fictional company the demo provisions accounts for (used in Claude's prompts).
ORG_NAME = os.getenv("DEMO_ORG", "Example Bank")
MANAGE_WORKERS = os.getenv("DEMO_MANAGE_WORKERS", "true").lower() not in {"0", "false", "no"}
START_WORKERS = os.getenv("DEMO_START_WORKERS", "core:2,onprem:2")
WORKER_MAX_ACTIVITIES = int(os.getenv("WORKER_MAX_ACTIVITIES", "3"))
API_HOST = os.getenv("DEMO_HOST", "127.0.0.1")
API_PORT = int(os.getenv("DEMO_PORT", "8800"))


def queue_for_location(location: str) -> str:
    return ONPREM_QUEUE if location == "onprem" else CORE_QUEUE


def parse_start_workers(spec: str = START_WORKERS) -> dict:
    out = {}
    for part in spec.split(","):
        if ":" in part:
            pool, n = part.split(":", 1)
            try:
                out[pool.strip()] = max(0, int(n))
            except ValueError:
                continue
    return out
