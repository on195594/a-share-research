"""The installable Skill distributed by the web and identified by the MCP server."""
import hashlib
from io import BytesIO
from pathlib import Path
from zipfile import ZIP_DEFLATED, ZipFile

SKILL_DIR = Path(__file__).resolve().parents[1] / "skill" / "a-share"


def version() -> str:
    return hashlib.sha256((SKILL_DIR / "SKILL.md").read_bytes()).hexdigest()[:12]


def archive() -> bytes:
    package = BytesIO()
    with ZipFile(package, "w", compression=ZIP_DEFLATED) as bundle:
        for path in sorted(SKILL_DIR.rglob("*")):
            if path.is_file():
                bundle.write(path, path.relative_to(SKILL_DIR.parent))
    return package.getvalue()
