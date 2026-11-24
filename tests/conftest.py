import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

# Keep test runs away from the demo's real runtime folder and API key.
os.environ.setdefault("DEMO_RUNTIME_DIR", tempfile.mkdtemp(prefix="domainos-test-"))
os.environ["DEMO_AI_DISABLED"] = "1"
