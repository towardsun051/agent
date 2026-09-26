"""Skills Scanner — Scan skills/ directory and generate SKILLS_SNAPSHOT.md."""

from pathlib import Path

import yaml

from utils.encoding import safe_read_text


def scan_skills(base_dir: Path) -> str:
    """Scan all SKILL.md files and generate SKILLS_SNAPSHOT.md."""
    skills_dir = base_dir / "skills"
    snapshot_path = base_dir / "SKILLS_SNAPSHOT.md"

    if not skills_dir.exists():
        skills_dir.mkdir(parents=True)

    skills = []
    for skill_md in sorted(skills_dir.rglob("SKILL.md")):
        try:
            content = safe_read_text(skill_md)
            # Parse YAML frontmatter
            if content.startswith("---"):
                parts = content.split("---", 2)
                if len(parts) >= 3:
                    meta = yaml.safe_load(parts[1])
                    if meta:
                        rel_path = skill_md.relative_to(base_dir).as_posix()
                        skills.append({
                            "name": meta.get("name", skill_md.parent.name),
                            "description": meta.get("description", ""),
                            "location": rel_path,
                        })
        except Exception as e:
            print(f"⚠️ Error scanning {skill_md}: {e}")

    # Build XML-style snapshot
    lines = ["<available_skills>"]
    for s in skills:
        lines.append("  <skill>")
        lines.append(f"    <name>{s['name']}</name>")
        lines.append(f"    <description>{s['description']}</description>")
        lines.append(f"    <location>{s['location']}</location>")
        lines.append("  </skill>")
    lines.append("</available_skills>")

    snapshot = "\n".join(lines)
    snapshot_path.write_text(snapshot, encoding="utf-8")
    print(f"Skills snapshot: {len(skills)} skills found")
    return snapshot
