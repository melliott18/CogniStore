# CogniStore
AI-Powered Data Lifecycle Manager

## Quickstart

- Create a virtual environment and install deps

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

- Run tests

```bash
pytest -q
```

- Try the POSIX driver via CLI

```bash
# Put and get a file using the filesystem as storage
python -m cognistore.cli.cognistore_cli /tmp/cognistore put demo-bucket path/to/key.txt README.md
python -m cognistore.cli.cognistore_cli /tmp/cognistore get demo-bucket path/to/key.txt /tmp/out.txt
python -m cognistore.cli.cognistore_cli /tmp/cognistore ls demo-bucket --prefix path/
```

### Driver configuration (optional)

You can instantiate drivers from a YAML file using `cognistore.drivers.driver_loader.load_drivers`.

Example `drivers.yaml`:

```yaml
tiers:
	hot:
		driver: posix
		path: /tmp/cognistore/hot
	warm:
		driver: posix
		path: /tmp/cognistore/warm
```

Then in Python:

```python
from cognistore.drivers.driver_loader import load_drivers
drivers = load_drivers("drivers.yaml")
hot = drivers["hot"]
hot.put_object("bucket", "key.txt", b"hello")
```

### CLI with tiers

```bash
# Put into default (hot) tier via drivers.yaml
python -m cognistore.cli.cognistore_cli --drivers drivers.yaml put demo-bucket path/key.txt README.md

# List keys in a specific tier
python -m cognistore.cli.cognistore_cli --drivers drivers.yaml ls-tier hot demo-bucket --prefix path/

# Move between tiers
python -m cognistore.cli.cognistore_cli --drivers drivers.yaml move hot warm demo-bucket path/key.txt

# Verify in warm tier
python -m cognistore.cli.cognistore_cli --drivers drivers.yaml ls-tier warm demo-bucket --prefix path/
```

### Catalog and policy runner via CLI

You can build a catalog from an existing tier and then run a simple policy pass to move objects automatically.

```bash
# Optionally use a persistent SQLite catalog
CAT_DB=/tmp/cognistore/catalog.db

# Scan a tier (e.g., hot) and index objects into the catalog
python -m cognistore.cli.cognistore_cli --drivers drivers.yaml --catalog-db "$CAT_DB" \
	catalog-scan hot demo-bucket --prefix path/

# Run a policy pass: files <= threshold go to hot; larger go to warm
python -m cognistore.cli.cognistore_cli --drivers drivers.yaml --catalog-db "$CAT_DB" \
	policy-run demo-bucket --prefix path/ --threshold 1048576

# Inspect tiers after moves
python -m cognistore.cli.cognistore_cli --drivers drivers.yaml ls-tier hot demo-bucket --prefix path/
python -m cognistore.cli.cognistore_cli --drivers drivers.yaml ls-tier warm demo-bucket --prefix path/
```

Notes:
- If `--catalog-db` is omitted, an in-memory catalog is used for the current run only.
- `catalog-scan` captures metadata including sha256, mime, and a small sample length.
- `policy-run` supports:
	- `--policy simple|llm` (default: simple)
	- `--allowed-tiers hot,warm` to constrain decisions
	- `--threshold` (and `--llm-threshold` for the LLM path)
	The LLM mode currently uses a threshold-based mock provider; you can swap in a real provider later.
