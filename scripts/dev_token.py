"""Create the shared dev signing key (once) and print a bearer token for local compose testing.

python scripts/dev_token.py u-ava            -> token for a customer
python scripts/dev_token.py ops policy:write -> token allowed to post policy changes
"""
import os
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))
from sysdesign.auth import LocalIssuer  # noqa: E402

KEY = pathlib.Path(__file__).resolve().parent.parent / "deploy" / "dev-keys" / "jwt.pem"
if not KEY.exists():
    KEY.parent.mkdir(parents=True, exist_ok=True)
    KEY.write_bytes(LocalIssuer().private_pem())
    os.chmod(KEY, 0o644)  # read by the non-root container user; dev-only key, never committed
iss = LocalIssuer.from_pem_file(str(KEY))
sub = sys.argv[1] if len(sys.argv) > 1 else "u-ava"
print(iss.mint(sub, scopes=("chat", *sys.argv[2:]), ttl=3600))
