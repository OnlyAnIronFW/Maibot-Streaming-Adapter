import sys

from pathlib import Path

_TESTS_DIR = Path(__file__).resolve().parent
_PLUGINS_DIR = _TESTS_DIR.parents[1]
_WORKSPACE_DIR = _TESTS_DIR.parents[3]
_CORE_REPO_DIR = _WORKSPACE_DIR / "MaiBot-r-dev"

for candidate in (_PLUGINS_DIR, _CORE_REPO_DIR):
    candidate_text = str(candidate)
    if candidate.exists() and candidate_text not in sys.path:
        sys.path.insert(0, candidate_text)
