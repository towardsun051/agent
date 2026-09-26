"""GET/POST/DELETE /api/files + GET/DELETE /api/skills — File read/write for Monaco editor."""

import asyncio
import shutil
from pathlib import Path

import yaml
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from utils.encoding import safe_read_text

router = APIRouter()

BASE_DIR = Path(__file__).resolve().parent.parent

# Whitelist of editable directories (relative to backend/)
ALLOWED_PREFIXES = ["workspace/", "memory/", "skills/", "knowledge/"]

# Whitelist of specific root-level files that can be accessed
ALLOWED_ROOT_FILES = {"SKILLS_SNAPSHOT.md"}


def _validate_path(rel_path: str) -> Path:
    """Validate and resolve file path within allowed directories."""
    normalized = rel_path.replace("\\", "/").lstrip("./")
    if not (
        any(normalized.startswith(prefix) for prefix in ALLOWED_PREFIXES)
        or normalized in ALLOWED_ROOT_FILES
    ):
        raise HTTPException(status_code=403, detail=f"Access denied: {rel_path}")
    full_path = (BASE_DIR / normalized).resolve()
    if not str(full_path).startswith(str(BASE_DIR)):
        raise HTTPException(status_code=403, detail="Path traversal detected")
    return full_path


@router.get("/files")
async def read_file(path: str):
    file_path = _validate_path(path)
    if not file_path.exists():
        raise HTTPException(status_code=404, detail=f"File not found: {path}")
    content = safe_read_text(file_path)
    return {"path": path, "content": content}


class FileSaveRequest(BaseModel):
    path: str
    content: str


@router.post("/files")
async def save_file(request: FileSaveRequest):
    file_path = _validate_path(request.path)
    file_path.parent.mkdir(parents=True, exist_ok=True)
    file_path.write_text(request.content, encoding="utf-8")

    # Trigger memory index rebuild when MEMORY.md is saved
    normalized = request.path.replace("\\", "/").lstrip("./")
    if normalized == "memory/MEMORY.md":
        try:
            from graph.memory_indexer import get_memory_indexer

            indexer = get_memory_indexer(BASE_DIR)
            await asyncio.to_thread(indexer.rebuild_index)
        except Exception:
            pass

    if normalized.startswith("skills/") and normalized.endswith("/SKILL.md"):
        from tools.skills_scanner import scan_skills
        scan_skills(BASE_DIR)

    return {"path": request.path, "status": "saved"}


@router.get("/skills")
async def list_skills():
    """Scan skills/ directory and return skill list with name, path, description."""
    skills_dir = BASE_DIR / "skills"
    if not skills_dir.exists():
        return {"skills": []}

    skills: list[dict[str, str]] = []
    for skill_dir in sorted(skills_dir.iterdir()):
        if not skill_dir.is_dir():
            continue
        skill_md = skill_dir / "SKILL.md"
        if not skill_md.exists():
            continue

        name = skill_dir.name
        description = ""
        rel_path = f"skills/{name}/SKILL.md"

        # Parse YAML frontmatter
        try:
            text = safe_read_text(skill_md)
            if text.startswith("---"):
                parts = text.split("---", 2)
                if len(parts) >= 3:
                    meta = yaml.safe_load(parts[1])
                    if isinstance(meta, dict):
                        name = meta.get("name", name)
                        description = meta.get("description", "")
        except Exception:
            pass

        skills.append({"name": name, "path": rel_path, "description": description})

    return {"skills": skills}


@router.delete("/skills/{skill_name}")
async def delete_skill(skill_name: str):
    """Delete a skill directory entirely."""
    # Validate name (alphanumeric, hyphens, underscores only)
    safe_name = "".join(c for c in skill_name if c.isalnum() or c in "-_")
    if not safe_name or safe_name != skill_name:
        raise HTTPException(status_code=400, detail="Invalid skill name")

    skill_dir = BASE_DIR / "skills" / safe_name
    if not skill_dir.exists() or not skill_dir.is_dir():
        raise HTTPException(status_code=404, detail=f"Skill not found: {skill_name}")

    # Safety: ensure path is inside skills/
    resolved = skill_dir.resolve()
    if not str(resolved).startswith(str((BASE_DIR / "skills").resolve())):
        raise HTTPException(status_code=403, detail="Path traversal detected")

    shutil.rmtree(skill_dir)

    # Regenerate SKILLS_SNAPSHOT.md
    try:
        from tools.skills_scanner import scan_skills
        scan_skills(BASE_DIR)
    except Exception:
        pass

    return {"status": "deleted", "name": skill_name}
