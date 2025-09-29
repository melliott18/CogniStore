import os
from pathlib import Path

ROOT = Path("cognistore_repo")

# Directories to create
DIRS = [
    "cognistore/api",
    "cognistore/core",
    "cognistore/drivers",
    "cognistore/utils",
    "cognistore/cli/commands",
    "tests/unit",
    "tests/integration",
    "tests/perf",
    "docs",
    ".github/workflows",
]

# Files to create (blank)
FILES = [
    "README.md",
    "requirements.txt",
    "docker-compose.yml",
    "pyproject.toml",
    "mkdocs.yml",
    "setup.sh",
    "docs/proposal.md",
    "docs/design.md",
    "docs/architecture.md",
    "docs/Manual_Setup_Guide.md",
    "cognistore/__init__.py",
    "cognistore/api/__init__.py",
    "cognistore/api/gateway.py",
    "cognistore/core/__init__.py",
    "cognistore/core/catalog.py",
    "cognistore/drivers/__init__.py",
    "cognistore/drivers/storage_driver.py",
    "cognistore/utils/__init__.py",
    "cognistore/cli/__init__.py",
    "cognistore/cli/cognistore_cli.py",
    "cognistore/cli/commands/__init__.py",
    "tests/unit/test_posix_driver.py",
    "tests/integration/test_mover_catalog.py",
    "tests/perf/perf_async_put_get.py",
    "tests/conftest.py",
    ".github/workflows/ci.yml",
    ".github/workflows/docs.yml",
]

def scaffold():
    for d in DIRS:
        path = ROOT / d
        os.makedirs(path, exist_ok=True)
    for f in FILES:
        path = ROOT / f
        os.makedirs(path.parent, exist_ok=True)
        path.touch(exist_ok=True)
    os.chmod(ROOT / "setup.sh", 0o755)
    print(f"✅ Blank CogniStore scaffold created at {ROOT.absolute()}")

if __name__ == "__main__":
    scaffold()