import hashlib
import json
import os
import pathlib
import subprocess
import sys
import time

root=pathlib.Path('/tmp/cognistore-155-validation')
env=dict(os.environ)
for key in list(env):
    if key.startswith(('COGNISTORE_','AWS_','AZURE_','GOOGLE_')):
        env.pop(key)
env.update(json.loads((root/'fixture-env.json').read_text()))
paths=subprocess.check_output(['git','ls-files','cognistore','tests','scripts','release','pyproject.toml'],text=True).splitlines()
(root/'tested-files.json').write_text(json.dumps({p:hashlib.sha256(pathlib.Path(p).read_bytes()).hexdigest() for p in paths},indent=2)+'\n')
command=[sys.executable,'-m','pytest','-p','pytest_cov','--cov=cognistore','--cov-report=term-missing','--cov-report=xml:'+str(root/'coverage.xml'),'--junitxml='+str(root/'full-suite.xml'),'-q','-o','addopts=','--import-mode=importlib','-ra']
record={'command':command,'started_at':time.strftime('%Y-%m-%dT%H:%M:%SZ',time.gmtime()),'base_revision':subprocess.check_output(['git','rev-parse','HEAD'],text=True).strip(),'scope':'isolated-local-development','services':'services.json','source_hashes':'tested-files.json'}
(root/'full-suite-run.json').write_text(json.dumps(record,indent=2)+'\n')
with (root/'full-suite.log').open('w') as log:
    result=subprocess.run(command,env=env,stdout=log,stderr=subprocess.STDOUT)
record.update({'exit_code':result.returncode,'ended_at':time.strftime('%Y-%m-%dT%H:%M:%SZ',time.gmtime())})
(root/'full-suite-run.json').write_text(json.dumps(record,indent=2)+'\n')
print(json.dumps(record,indent=2))
print('\n'.join((root/'full-suite.log').read_text().splitlines()[-20:]))
raise SystemExit(result.returncode)
