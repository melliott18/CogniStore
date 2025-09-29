import sys
from pathlib import Path

# Add project root so `import cognistore` works without installing the package
ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
	sys.path.insert(0, str(ROOT))

import os
import sys

# Add repository root to sys.path so tests can import 'cognistore'
TESTS_DIR = os.path.dirname(__file__)
REPO_ROOT = os.path.abspath(os.path.join(TESTS_DIR, os.pardir))
if REPO_ROOT not in sys.path:
	sys.path.insert(0, REPO_ROOT)

