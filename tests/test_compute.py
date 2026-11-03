#!/usr/bin/env python3
"""Running reconstructions on other machines: the pool of machines, a whole job through a stand-in for
ssh that runs commands here, and the AWS provider against stand-ins for AWS. Offline."""
from __future__ import annotations

import json
import os
import pathlib
import stat
import sys
import tempfile
import threading
import time

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))
import compute  # noqa: E402
from climbs.common import JobControl  # noqa: E402
from progress import Tracker  # noqa: E402


# --- the pool: at most `limit` machines, reused while idle, ended when idle too long or lost ----------
class FakeProvider:
    def __init__(self, fail=False):
        self.started, self.stopped, self.fail = [], [], fail

    def start(self, log):
        if self.fail:
            raise RuntimeError("no capacity")
        n = compute.Node(host=f"h{len(self.started)}", user="u", id=f"i-{len(self.started)}")
        self.started.append(n)
        return n

    def stop(self, node):
        self.stopped.append(node.id)


prov = FakeProvider()
pool = compute.Pool(prov, limit=2, idle_minutes=10, log=lambda s: None)
a, b = pool.acquire(print), pool.acquire(print)
assert a is not b and len(prov.started) == 2
got = []
t = threading.Thread(target=lambda: got.append(pool.acquire(print)))
t.start()
time.sleep(0.3)
assert not got, "a third waits for one of the two"
pool.release(a)
t.join(5)
assert got == [a] and len(prov.started) == 2, "and gets the one released, rather than a new one"
pool.release(got[0])
pool.release(b, broken=True)
assert prov.stopped == ["i-1"] and len(pool.nodes) == 1, "a lost machine is ended"
pool.reap()
assert len(pool.nodes) == 1, "not idle long enough yet"
pool.idle = 0
pool.reap()
assert pool.nodes == [] and prov.stopped == ["i-1", "i-0"]
assert pool.status()["nodes"] == []
bad = compute.Pool(FakeProvider(fail=True), limit=1, idle_minutes=1, log=lambda s: None)
try:
    bad.acquire(print)
    raise AssertionError("should have failed")
except compute.ComputeUnavailable as e:
    assert "no capacity" in str(e) and bad.error and bad.starting == 0
assert compute.parse_host("ubuntu@10.0.0.9:2222") == ("ubuntu", "10.0.0.9", 2222) and compute.parse_host("a@b") == ("a", "b", 22)
print("pool OK")

# --- a whole job, through an ssh that runs the command here -------------------------------------------
FAKE_SSH = """#!/usr/bin/env python3
import os, sys
args, i = sys.argv[1:], 0
while i < len(args) and args[i].startswith("-"):
    i += 2                                  # every option ssh_args gives takes a value
host, command = args[i], " ".join(args[i + 1:])
if host.endswith("@down"):
    sys.stderr.write("ssh: connect to host down port 22: Connection refused\\n")
    sys.exit(255)
os.execvp("sh", ["sh", "-c", command])     # as ssh's end of it goes when ssh is killed
"""
try:
    from PIL import Image
except ImportError:
    print("Pillow not installed: skipping the remote job test")
    Image = None
with tempfile.TemporaryDirectory() as td:
    d = pathlib.Path(td)
    (d / "bin").mkdir()
    (d / "bin" / "ssh").write_text(FAKE_SSH)
    (d / "bin" / "ssh").chmod(0o755)
    os.environ["PATH"] = f"{d / 'bin'}:{os.environ['PATH']}"
    compute.REMOTE_BASE = str(d / "remote")
    project = d / "project"
    (project / "photos").mkdir(parents=True)
    (project / "masks").mkdir()
    (project / "masks" / "old.jpg.png").write_bytes(b"kept")
    if Image:
        for i in range(3):
            Image.new("RGB", (64, 48), (10 * i, 80, 120)).save(project / "photos" / f"p{i}.jpg")
    node = compute.Node(host="localhost", user="me", gpu=False)
    command = f"cd {ROOT} && {sys.executable} backend/remote_job.py {{dir}} {{flags}}"
    updates = []

    class Seen(Tracker):
        def update(self, key, frac=0.0, detail=None):
            updates.append(key)

    jobctl = JobControl()
    if Image:
        logged = []
        mesh, demo = compute.run_remote(node, project / "photos", d / "out", logged.append, True, Seen([]), "img", command, jobctl)
        assert mesh.exists() and mesh.parent == d / "out" and demo is True, (mesh, demo)   # no COLMAP here: the demo mesh
        assert any("remote: sent" in line for line in logged) and any("results back" in line for line in logged), logged
        assert (project / "masks" / "old.jpg.png").read_bytes() == b"kept", "masks from before stay"
        assert not list((d / "remote").iterdir()), "the job's folder on the machine is removed"
        assert not list(project.glob("bm-remote-*")), "and the one it came back through here"
    # a job that fails says why; a machine that can't be reached makes the climb wait
    try:
        compute.run_remote(node, project / "photos", d / "out2", print, False, Tracker([]), "img", "echo 'FAILED: only 2 image(s) could be registered'; exit 1", jobctl)
        raise AssertionError("should have failed")
    except RuntimeError as e:
        assert "only 2 image(s)" in str(e), e
    try:
        compute.run_remote(compute.Node(host="down", user="me"), project / "photos", d / "out3", print, False, Tracker([]), "img", command, jobctl)
        raise AssertionError("should have been unavailable")
    except compute.ComputeUnavailable as e:
        assert "Connection refused" in str(e), e
    # stopping a climb ends its remote job
    jobctl2 = JobControl()
    threading.Timer(1.0, jobctl2.stop).start()
    t0 = time.time()
    try:
        compute.run_remote(node, project / "photos", d / "out4", print, False, Tracker([]), "img", "exec sleep 30", jobctl2)
        raise AssertionError("should have been stopped")
    except BaseException as e:
        assert type(e).__name__ == "Cancelled" and time.time() - t0 < 10, (e, time.time() - t0)
print("remote job OK")

# --- AWS, against stand-ins for its clients --------------------------------------------------------------
class FakeEC2:
    def __init__(self):
        self.calls = []

    def describe_security_groups(self, **kw):
        return {"SecurityGroups": [{"GroupId": "sg-1"}]}

    def authorize_security_group_ingress(self, **kw):
        self.calls.append(("ingress", kw))
        raise Exception("InvalidPermission.Duplicate")

    def run_instances(self, **kw):
        self.calls.append(("run", kw))
        if kw.get("DryRun"):
            raise Exception("An error occurred (DryRunOperation) when calling the RunInstances operation: Request would have succeeded")
        return {"Instances": [{"InstanceId": "i-abc"}]}

    def describe_instances(self, **kw):
        self.calls.append(("describe", kw))
        return {"Reservations": [{"Instances": [{"InstanceId": "i-old"}, {"InstanceId": "i-older"}]}]}

    def terminate_instances(self, **kw):
        self.calls.append(("terminate", kw))


class FakeSSM:
    def get_parameter(self, Name):
        assert Name == compute.DLAMI
        return {"Parameter": {"Value": "ami-123"}}


class FakeSTS:
    def get_caller_identity(self):
        return {"Account": "1234"}


import settings as settings_mod  # noqa: E402
import urllib.request  # noqa: E402

with tempfile.TemporaryDirectory() as td:
    s = {**settings_mod.DEFAULTS, "compute.provider": "aws", "compute.image": "ghcr.io/me/bouldermesh:latest"}
    aws = compute.AWSProvider(s, {}, pathlib.Path(td), "srv1")
    assert stat.S_IMODE((pathlib.Path(td) / "aws_worker").stat().st_mode) == 0o600
    ec2 = FakeEC2()
    clients = {"ec2": ec2, "ssm": FakeSSM(), "sts": FakeSTS()}
    aws._client = lambda name: clients[name]
    urllib.request.urlopen = lambda url, timeout=0: type("R", (), {"read": lambda self: b"203.0.113.7\n"})()
    args = aws._launch_args(ec2)
    assert args["ImageId"] == "ami-123" and args["InstanceType"] == "g4dn.2xlarge" and args["SecurityGroupIds"] == ["sg-1"]
    assert args["InstanceMarketOptions"]["MarketType"] == "spot" and args["InstanceInitiatedShutdownBehavior"] == "terminate"
    assert {"Key": compute.TAG, "Value": "srv1"} in args["TagSpecifications"][0]["Tags"]
    assert ec2.calls[0][1]["IpPermissions"][0]["IpRanges"][0]["CidrIp"] == "203.0.113.7/32", "SSH only from this server"
    ud = args["UserData"]
    pub = (pathlib.Path(td) / "aws_worker.pub").read_text().strip()
    assert pub in ud and "ghcr.io/me/bouldermesh:latest" in ud and "shutdown -h now" in ud and "docker login" not in ud
    assert "test" in aws.test() or "allowed" in aws.test()
    assert aws.leftovers() == ["i-old", "i-older"]
    filters = [c for c in ec2.calls if c[0] == "describe"][-1][1]["Filters"]
    assert {"Name": f"tag:{compute.TAG}", "Values": ["srv1"]} in filters, "only this server's"
    aws.stop(compute.Node(host="", user="", id="i-old"))
    assert ec2.calls[-1] == ("terminate", {"InstanceIds": ["i-old"]})
    ecr = compute.AWSProvider({**s, "compute.image": "123456789012.dkr.ecr.us-west-2.amazonaws.com/bm:1", "compute.aws.instance_profile": "bm"},
                              {}, pathlib.Path(td), "srv1")
    assert "aws ecr get-login-password --region us-west-2" in ecr.user_data()
    assert compute.provider_for({**s, "compute.provider": "none"}, lambda k: None, pathlib.Path(td), "x") is None
    no_image = compute.AWSProvider({**s, "compute.image": ""}, {}, pathlib.Path(td), "srv1")
    try:
        no_image.start(print)
        raise AssertionError("should have refused")
    except compute.ComputeUnavailable as e:
        assert "compute.image" in str(e)
print("aws OK")
print("ALL COMPUTE TESTS PASSED")
