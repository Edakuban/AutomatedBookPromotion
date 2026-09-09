"""Exercise Git's real matcher without creating a repository in the project."""

from pathlib import Path
import shutil
import subprocess

import pytest


def test_private_files_are_ignored_and_project_files_remain_trackable(tmp_path):
    git = shutil.which("git")
    if git is None:
        pytest.skip("Git ist für diesen Check erforderlich")
    subprocess.run([git, "init", "--quiet", str(tmp_path)], check=True, capture_output=True)
    project = Path(__file__).resolve().parents[1]
    shutil.copyfile(project / ".gitignore", tmp_path / ".gitignore")
    private = [
        ".env", ".env.local", ".env.production", "server.env", "nested/.env",
        "data/originals/book.docx", "data/context.json", "novel.docx", "novel.pdf",
        "uploads/book.txt", "outputs/post.jpg", "logs/import.log", "backup.dump",
        ".venv/Scripts/python.exe", ".uv-cache/uv.exe", "private.key",
    ]
    public = [".env.example", "pyproject.toml", "uv.lock", "README.md",
              "src/bookpromo/config.py", "tests/test_config.py", "check-config.bat"]
    result = subprocess.run(
        [git, "-C", str(tmp_path), "check-ignore", "--no-index", "--stdin", "-z"],
        input=("\0".join(private + public) + "\0").encode(), capture_output=True, check=True,
    )
    assert set(result.stdout.decode().rstrip("\0").split("\0")) == set(private)
