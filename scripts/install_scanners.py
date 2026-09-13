"""Install pinned upstream tools with release-asset SHA256 verification."""
import hashlib,io,platform,tarfile
import requests
from pathlib import Path
assets={
 ('Linux','x86_64'):[('aquasecurity/trivy','0.74.0','Linux-64bit','2ae6fe3ee734b7fdf11335663e18c75ea12dccc76062f09f164a3b0f8be4371a'),('gitleaks/gitleaks','8.30.1','linux_x64','551f6fc83ea457d62a0d98237cbad105af8d557003051f41f3e7ca7b3f2470eb')],
 ('Darwin','arm64'):[('aquasecurity/trivy','0.74.0','macOS-ARM64','1caada5e0e2091909357c7525d3aa76f4b660b13821bc143b190c7483e31cc11'),('gitleaks/gitleaks','8.30.1','darwin_arm64','b40ab0ae55c505963e365f271a8d3846efbc170aa17f2607f13df610a9aeb6a5')],
}
out=Path('.deployment/tools');out.mkdir(parents=True,exist_ok=True)
for repo,version,suffix,expected in assets[(platform.system(),platform.machine())]:
 name=repo.split('/')[-1]
 url=f'https://github.com/{repo}/releases/download/v{version}/{name}_{version}_{suffix}.tar.gz'
 response=requests.get(url,timeout=120);response.raise_for_status();data=response.content
 if hashlib.sha256(data).hexdigest()!=expected:raise SystemExit('Scanner checksum mismatch')
 with tarfile.open(fileobj=io.BytesIO(data),mode='r:gz') as archive:
  binary=archive.extractfile(name)
  if binary is None:raise SystemExit('Scanner binary missing')
  path=out/name;path.write_bytes(binary.read());path.chmod(0o755)
 print(f'Verified {name} {version}',flush=True)
