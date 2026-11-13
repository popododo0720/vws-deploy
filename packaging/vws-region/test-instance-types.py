#!/usr/bin/env python3
"""The shipped region.yaml passes the instance-type check; drifted values fail it."""
import pathlib
import subprocess
import sys

checker, region, work = sys.argv[1:4]
work = pathlib.Path(work) / "instance-types"
work.mkdir()
shipped = pathlib.Path(region).read_text()


def run(text):
    candidate = work / "region.yaml"
    candidate.write_text(text)
    return subprocess.run(["python3", checker, str(candidate)],
                          capture_output=True, text=True, timeout=10)


result = run(shipped)
assert result.returncode == 0, result.stderr
assert "memory_mib: 2048" not in shipped.split("instance_types:")[1].split("scheduler:")[0]
old = shipped.replace("cores: 1\n    memory_mib: 1024", "cores: 2\n    memory_mib: 2048")
assert old != shipped, "shipped region.yaml no longer lists vws.small 1/1024"
for bad, why in ((old, "old vws.small 2/2048"),
                 (shipped.replace("cores: 1", "cores: 2"), "wrong cores"),
                 (shipped.replace("memory_mib: 1024", "memory_mib: 2048"), "wrong memory"),
                 (shipped.replace("vws.small:", "vws.tiny:"), "unknown type")):
    result = run(bad)
    assert result.returncode != 0, f"drifted instance type accepted: {why}"
    assert "instance_types." in result.stderr, result.stderr
print("instance types: shipped region.yaml accepted; 4 drifted variants rejected")
