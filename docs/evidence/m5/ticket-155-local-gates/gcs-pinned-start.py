import json
import socket
import subprocess
import time
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

root = Path('/tmp/cognistore-155-validation')
image = json.loads(subprocess.check_output(['docker', 'image', 'inspect', 'cognistore/gcs-emulator:155-3c29d20789f6']))[0]
expected = '3c29d20789f6475f65d2554ad6898c4afd7124bb'
assert image['Config']['Labels']['org.opencontainers.image.revision'] == expected
with socket.socket() as sock:
    sock.bind(('127.0.0.1', 0))
    port = sock.getsockname()[1]
endpoint = f'http://127.0.0.1:{port}'
command = ['docker', 'run', '--detach', '--pull=never', '--name', 'cognistore-155-gcs-pinned', '--label', 'cognistore.validation=155', '--publish', f'127.0.0.1:{port}:4443', image['Id'], '-scheme', 'http', '-backend', 'memory', '-external-url', endpoint]
metadata = {
    'source_revision': expected,
    'source_context': f'https://github.com/fsouza/fake-gcs-server.git#{expected}',
    'image_id': image['Id'],
    'image_labels': image['Config']['Labels'],
    'platform': f"{image['Os']}/{image['Architecture']}",
    'started_at': datetime.now(timezone.utc).isoformat(),
    'endpoint': endpoint,
    'command': command,
    'source_provenance': 'Built from exact repository-pinned Git context; see gcs-pinned-build.log and gcs-pinned-build.sh. Existing cached tag did not embed source revision, so not treated as proven.',
}
metadata['container_id'] = subprocess.check_output(command, text=True).strip()
(root / 'gcs-pinned-metadata.json').write_text(json.dumps(metadata, indent=2) + '\n')
(root / 'gcs-pinned-image.json').write_text(json.dumps(image, indent=2) + '\n')
for attempt in range(30):
    try:
        with urllib.request.urlopen(endpoint + '/storage/v1/b', timeout=2) as response:
            assert response.status == 200
        break
    except Exception:
        if attempt == 29:
            raise
        time.sleep(1)
print(json.dumps({'endpoint': endpoint, 'image_id': image['Id'], 'container_id': metadata['container_id']}))
