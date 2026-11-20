from pathlib import Path
import shutil
import subprocess
import sys


packaging, tmp = map(Path, sys.argv[1:])
checker = packaging / "check-ops-ports.py"
fixture = tmp / "ports"
for name in ("vws-region", "vws-global"):
    shutil.copytree(packaging / name, fixture / name)


def run(ok, message=""):
    result = subprocess.run([sys.executable, str(checker), str(fixture)],
                            capture_output=True, text=True, check=False)
    assert (result.returncode == 0) == ok, result.stdout + result.stderr
    assert message in result.stderr, result.stderr


run(True)
unit = fixture / "vws-global/vws-iam.service"
probe = fixture / "vws-global/check-ready"
original = unit.read_text()
unit.write_text(original.replace("127.0.0.1:9096", "127.0.0.1:9097"))
run(False, "!= readiness")
probe.write_text(probe.read_text().replace("127.0.0.1:9096", "127.0.0.1:9097"))
run(True)
env = fixture / "vws-region/region.env"
env.write_text(env.read_text().replace("127.0.0.1:8085", "127.0.0.1:8083"))
run(False, "share 127.0.0.1:8083")
print("listener mismatch and cross-service collision rejected")
