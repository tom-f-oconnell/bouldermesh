"""Running reconstructions on other machines, and starting and ending those machines.

The server stays where the site and the data are. A remote worker sends a climb's photos and people
masks to a node, runs the same pipeline there in the same Docker image (remote_job.py), and brings
back the mesh, its cameras and the masks. Nodes are reached over SSH, which also carries the data, so
any machine with Docker (and NVIDIA's container toolkit, for its GPU) can be one. Only starting and
ending cloud machines is particular to a provider:

  ssh    machines kept running elsewhere, as user@host[:port], all reached with one SSH key
  aws    EC2 instances started when there's work (a spot GPU instance by default), ended after
         compute.idle_minutes with nothing to do, and ending themselves after a while without a job,
         in case the server that started them has gone. Any left from before a restart are ended.

A node that can't be had, or is lost partway, makes a climb wait (ComputeUnavailable) rather than
fail. design.md has what offloading costs and saves.
"""
from __future__ import annotations

import json
import os
import re
import secrets as _secrets
import shlex
import shutil
import subprocess
import tempfile
import threading
import time
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

Log = Callable[[str], None]
REMOTE_BASE = "/tmp/bouldermesh"
DEFAULT_COMMAND = "docker run --rm --name bm-{job} {gpus} -v {dir}:/job {image} python3 /app/backend/remote_job.py /job {flags}"
DLAMI = "/aws/service/deeplearning/ami/x86_64/base-oss-nvidia-driver-gpu-ubuntu-24.04/latest/ami-id"
SECURITY_GROUP = "bouldermesh-workers"
TAG = "bouldermesh-server"


class ComputeUnavailable(Exception):
    """No node could be had, or one was lost partway: the work waits, rather than failing."""


@dataclass
class Node:
    host: str
    user: str
    port: int = 22
    id: str | None = None             # the provider's, e.g. an EC2 instance id
    key_file: Path | None = None
    known_hosts: Path | None = None
    gpu: bool | None = None           # found on first use
    busy: bool = False
    last_used: float = field(default_factory=time.time)
    started: float = field(default_factory=time.time)


def ssh_args(node: Node) -> list[str]:
    out = ["ssh", "-o", "BatchMode=yes", "-o", "StrictHostKeyChecking=accept-new", "-o", "ServerAliveInterval=30",
           "-o", "ServerAliveCountMax=6", "-o", "ConnectTimeout=20", "-p", str(node.port)]
    if node.known_hosts:
        out += ["-o", f"UserKnownHostsFile={node.known_hosts}"]
    if node.key_file:
        out += ["-i", str(node.key_file), "-o", "IdentitiesOnly=yes"]
    return out + [f"{node.user}@{node.host}"]


def ssh(node: Node, command: str, timeout: float = 120) -> str:
    """Run a short command on a node and return what it printed. SSH's own failures (it exits 255,
    as when a node is unreachable) are ComputeUnavailable."""
    r = subprocess.run(ssh_args(node) + [command], capture_output=True, text=True, timeout=timeout)
    if r.returncode == 255:
        raise ComputeUnavailable(f"couldn't reach {node.host}: {r.stderr.strip()[-200:]}")
    if r.returncode:
        raise RuntimeError(f"{command.split()[0]} on {node.host} exited {r.returncode}: {r.stderr.strip()[-200:]}")
    return r.stdout


def parse_host(text: str) -> tuple[str, str, int]:
    """user@host[:port] -> (user, host, port)."""
    m = re.fullmatch(r"([^@\s]+)@([^:\s]+)(?::(\d+))?", text.strip())
    if not m:
        raise ValueError(f"{text!r} isn't user@host or user@host:port")
    return m[1], m[2], int(m[3] or 22)


# ------------------------------------------------------------------ running a job on a node
def run_remote(node: Node, images: Path, out_dir: Path, log: Log, internet: bool, tracker, image: str, command: str,
               jobctl) -> tuple[Path, bool]:
    """Reconstruct a project's photos on a node: what pipeline.reconstruct does, there.

    Args:
        images: the project's photos/, next to its masks/, which go too and come back with new ones.
        command: how the node runs the job, with {dir}, {image}, {job}, {gpus} and {flags} filled in.
        jobctl: stops the transfer and the job if the climb is stopped.

    Returns:
        The mesh file and whether it's a demo, as pipeline.reconstruct does.
    """
    project = images.parent
    job = _secrets.token_hex(6)
    base = f"{REMOTE_BASE}/{job}"
    if node.gpu is None:
        try:
            node.gpu = bool(ssh(node, "nvidia-smi -L 2>/dev/null || true").strip())
        except RuntimeError:
            node.gpu = False
    log(f"remote: {node.host}{' (GPU)' if node.gpu else ''}, job {job}")
    ssh(node, f"mkdir -p {base}")
    try:
        parts = [p for p in ("photos", "masks") if (project / p).is_dir()]
        t0 = time.time()
        _pipe(["tar", "-C", str(project), "-cf", "-", *parts], ssh_args(node) + [f"tar -C {base} -xf -"], jobctl)
        size = sum(f.stat().st_size for p in parts for f in (project / p).iterdir())
        log(f"remote: sent {size / 1e6:.0f} MB in {time.time() - t0:.0f} s")
        cmd = (command or DEFAULT_COMMAND).format(dir=base, image=shlex.quote(image), job=job,
                                                  gpus="--gpus all" if node.gpu else "",
                                                  flags=" ".join(f for f, on in (("--internet", internet), ("--gpu", node.gpu)) if on))
        result, last = None, ""
        proc = jobctl.popen(ssh_args(node) + [cmd], stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, bufsize=1)
        for line in proc.stdout:
            line = line.rstrip("\n")
            result = follow(line, tracker, log) or result
            if not line.startswith("@@"):
                last = line or last
        proc.wait()
        jobctl.check()
        if proc.returncode == 255:
            raise ComputeUnavailable(f"lost {node.host} partway: {last[-200:]}")
        if proc.returncode or not result:
            raise job_failed(last)
        t0 = time.time()
        back = Path(tempfile.mkdtemp(prefix="bm-remote-", dir=project))
        try:
            _pipe(ssh_args(node) + [f"tar -C {base} -cf - out masks"], ["tar", "-C", str(back), "-xf", "-"], jobctl)
            mesh = take_results(back, out_dir, project, result)
        finally:
            shutil.rmtree(back, ignore_errors=True)
        log(f"remote: results back in {time.time() - t0:.0f} s")
        return mesh, bool(result.get("demo"))
    finally:
        try:   # not stoppable: this is the tidying up after a stop
            subprocess.run(ssh_args(node) + [f"docker rm -f bm-{job} >/dev/null 2>&1; rm -rf {base}"], capture_output=True, timeout=60)
        except Exception:
            pass


def follow(line: str, tracker, log: Log) -> dict | None:
    """One line a remote job printed (remote_job.py): its progress goes to tracker, anything that isn't progress or
    the result is logged, and the result is returned."""
    try:
        if line.startswith("@@progress "):
            _, key, frac, detail = (line.split(" ", 3) + [""])[:4]
            tracker.update(key, float(frac), json.loads(detail) if detail else None)
            return None
        if line.startswith("@@result "):
            result = json.loads(line[len("@@result "):])
            if isinstance(result, dict) and isinstance(result.get("mesh"), str):
                return result
    except (ValueError, TypeError, KeyError):
        pass
    log(line)
    return None


def job_failed(last: str) -> RuntimeError:
    """A remote job that ended without a mesh, from the last line it printed (pipeline's "FAILED: why")."""
    return RuntimeError(last[len("FAILED: "):] if last.startswith("FAILED: ") else f"the remote job ended without a mesh: {last[-200:]}")


def take_results(back: Path, out_dir: Path, project: Path, result: dict) -> Path:
    """Move what a remote job sent back from back/ into place: out/ (the mesh, its textures, cameras.json,
    orientation.json) into out_dir, and new people masks into the project's masks/.

    Returns:
        The mesh file its result named.
    """
    mesh = Path(result["mesh"]).name
    if not (back / "out" / mesh).is_file():
        raise RuntimeError(f"the remote job named a mesh it didn't send back: {mesh!r}")
    out_dir.mkdir(parents=True, exist_ok=True)
    for f in (back / "out").iterdir():
        shutil.move(str(f), out_dir / f.name)
    if (back / "masks").is_dir():
        (project / "masks").mkdir(exist_ok=True)
        for f in (back / "masks").iterdir():
            if f.is_file():
                os.replace(f, project / "masks" / f.name)
    return out_dir / mesh


def _pipe(source: list[str], sink: list[str], jobctl) -> None:
    """Run source | sink, stoppably, raising if either fails (ComputeUnavailable for SSH's own failures)."""
    a = subprocess.Popen(source, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    b = jobctl.popen(sink, stdin=a.stdout, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    a.stdout.close()
    _, err_b = b.communicate()
    _, err_a = a.communicate()
    jobctl.check()
    for cmd, p, err in ((source, a, err_a), (sink, b, err_b)):
        if p.returncode == 255 and cmd[0] == "ssh":
            raise ComputeUnavailable(f"lost the connection: {err.decode(errors='replace').strip()[-200:]}")
        if p.returncode:
            raise RuntimeError(f"{cmd[0]} exited {p.returncode}: {err.decode(errors='replace').strip()[-200:]}")


# ------------------------------------------------------------------ providers
class SSHProvider:
    """Machines kept running elsewhere, all reached with one SSH key."""
    name = "ssh"

    def __init__(self, hosts: list[str], key_text: str | None, keys_dir: Path):
        self.hosts = [parse_host(h) for h in hosts]
        self.keys_dir = keys_dir
        self.key_file = _key_file(keys_dir, "ssh_worker", key_text) if key_text else None
        self.in_use: set[tuple] = set()
        self._lock = threading.Lock()

    def start(self, log: Log) -> Node:
        with self._lock:
            free = [h for h in self.hosts if h not in self.in_use]
            if not free:
                raise ComputeUnavailable("every machine in compute.ssh_hosts is in use")
            user, host, port = free[0]
            self.in_use.add(free[0])
        node = Node(host=host, user=user, port=port, id=f"{user}@{host}:{port}", key_file=self.key_file,
                    known_hosts=self.keys_dir / "known_hosts")
        try:
            ssh(node, "true", timeout=40)
        except Exception:
            self.stop(node)
            raise
        return node

    def stop(self, node: Node) -> None:
        with self._lock:
            self.in_use.discard((node.user, node.host, node.port))

    def leftovers(self) -> list[str]:
        return []

    def test(self) -> str:
        if not self.hosts:
            raise ValueError("compute.ssh_hosts is empty")
        out = []
        for user, host, port in self.hosts:
            node = Node(host=host, user=user, port=port, key_file=self.key_file, known_hosts=self.keys_dir / "known_hosts")
            out.append(f"{host}: " + ssh(node, "docker --version; nvidia-smi -L 2>/dev/null | head -1 || true", timeout=40).strip().replace("\n", "; "))
        return " | ".join(out)


class AWSProvider:
    """EC2 instances, started for work and ended when idle. Needs boto3 and credentials: from the admin
    page, or the usual AWS environment variables or instance role."""
    name = "aws"

    def __init__(self, settings: dict, credentials: dict, keys_dir: Path, server_id: str):
        self.s, self.creds, self.keys_dir, self.server_id = settings, credentials, keys_dir, server_id
        self.key_file = keys_dir / "aws_worker"
        if not self.key_file.exists():
            keys_dir.mkdir(parents=True, exist_ok=True)
            subprocess.run(["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-C", "bouldermesh-worker", "-f", str(self.key_file)], check=True)

    def _client(self, service: str):
        import boto3
        kw = {"region_name": self.s["compute.aws.region"]}
        if self.creds.get("aws.access_key_id") and self.creds.get("aws.secret_access_key"):
            kw.update(aws_access_key_id=self.creds["aws.access_key_id"], aws_secret_access_key=self.creds["aws.secret_access_key"])
        return boto3.client(service, **kw)

    def _ami(self) -> str:
        return self.s["compute.aws.ami"] or self._client("ssm").get_parameter(Name=DLAMI)["Parameter"]["Value"]

    def _security_group(self, ec2) -> str:
        if self.s["compute.aws.security_group"]:
            return self.s["compute.aws.security_group"]
        found = ec2.describe_security_groups(Filters=[{"Name": "group-name", "Values": [SECURITY_GROUP]}])["SecurityGroups"]
        if found:
            sg = found[0]["GroupId"]
        else:
            vpc = ec2.describe_vpcs(Filters=[{"Name": "isDefault", "Values": ["true"]}])["Vpcs"][0]["VpcId"]
            sg = ec2.create_security_group(GroupName=SECURITY_GROUP, VpcId=vpc,
                                           Description="BoulderMesh compute nodes: SSH from the BoulderMesh server")["GroupId"]
        ip = urllib.request.urlopen("https://checkip.amazonaws.com", timeout=20).read().decode().strip()
        try:   # this server's address, which may have changed since the group was made
            ec2.authorize_security_group_ingress(GroupId=sg, IpPermissions=[{
                "IpProtocol": "tcp", "FromPort": 22, "ToPort": 22, "IpRanges": [{"CidrIp": f"{ip}/32", "Description": "BoulderMesh server"}]}])
        except Exception as e:
            if "Duplicate" not in str(e):
                raise
        return sg

    def user_data(self) -> str:
        """The script a new instance runs first: let this server in, get the image, end itself when idle."""
        pub = (self.key_file.with_suffix(".pub")).read_text().strip()
        image = self.s["compute.image"]
        ecr = re.match(r"(\d+\.dkr\.ecr\.([a-z0-9-]+)\.amazonaws\.com)/", image)
        login = (f"aws ecr get-login-password --region {ecr[2]} | docker login --username AWS --password-stdin {ecr[1]}\n"
                 if ecr and self.s["compute.aws.instance_profile"] else "")
        idle = int(self.s["compute.idle_minutes"]) + 10
        return f"""#!/bin/bash
# a BoulderMesh compute node: lets the server in, gets the image ready, and ends itself when idle
install -d -m 700 -o ubuntu -g ubuntu /home/ubuntu/.ssh
echo {shlex.quote(pub)} >> /home/ubuntu/.ssh/authorized_keys
chown ubuntu:ubuntu /home/ubuntu/.ssh/authorized_keys && chmod 600 /home/ubuntu/.ssh/authorized_keys
usermod -aG docker ubuntu
{login}(docker pull {shlex.quote(image)} > /var/log/bouldermesh-pull.log 2>&1 && touch /var/run/bouldermesh-ready) &
cat > /usr/local/bin/bouldermesh-watchdog <<'EOS'
#!/bin/bash
# ends this machine after {idle} minutes with no job running or data moving, in case its server has gone
idle=0
while sleep 60; do
  if [ -n "$(docker ps -q)" ] || pgrep -f "tar -C {REMOTE_BASE}" > /dev/null; then idle=0; else idle=$((idle + 1)); fi
  [ "$idle" -ge {idle} ] && shutdown -h now
done
EOS
chmod +x /usr/local/bin/bouldermesh-watchdog
nohup /usr/local/bin/bouldermesh-watchdog > /dev/null 2>&1 &
"""

    def _launch_args(self, ec2) -> dict:
        args = {"ImageId": self._ami(), "InstanceType": self.s["compute.aws.instance_type"], "MinCount": 1, "MaxCount": 1,
                "UserData": self.user_data(), "SecurityGroupIds": [self._security_group(ec2)],
                "InstanceInitiatedShutdownBehavior": "terminate",
                "BlockDeviceMappings": [{"DeviceName": "/dev/sda1", "Ebs": {"VolumeSize": int(self.s["compute.aws.volume_gb"]),
                                                                            "VolumeType": "gp3", "DeleteOnTermination": True}}],
                "TagSpecifications": [{"ResourceType": "instance", "Tags": [{"Key": TAG, "Value": self.server_id},
                                                                             {"Key": "Name", "Value": "bouldermesh-worker"}]}]}
        if self.s["compute.aws.spot"]:
            args["InstanceMarketOptions"] = {"MarketType": "spot", "SpotOptions": {"SpotInstanceType": "one-time",
                                                                                     "InstanceInterruptionBehavior": "terminate"}}
        if self.s["compute.aws.subnet"]:
            args["SubnetId"] = self.s["compute.aws.subnet"]
        if self.s["compute.aws.instance_profile"]:
            args["IamInstanceProfile"] = {"Name": self.s["compute.aws.instance_profile"]}
        return args

    def start(self, log: Log) -> Node:
        if not self.s["compute.image"]:
            raise ComputeUnavailable("set compute.image to the Docker image nodes should run")
        ec2 = self._client("ec2")
        try:
            iid = ec2.run_instances(**self._launch_args(ec2))["Instances"][0]["InstanceId"]
        except Exception as e:
            raise ComputeUnavailable(f"EC2 wouldn't start an instance: {e}") from None
        log(f"remote: started EC2 {self.s['compute.aws.instance_type']} {iid}{' (spot)' if self.s['compute.aws.spot'] else ''}")
        node = None
        try:
            ec2.get_waiter("instance_running").wait(InstanceIds=[iid], WaiterConfig={"Delay": 5, "MaxAttempts": 60})
            ip = ec2.describe_instances(InstanceIds=[iid])["Reservations"][0]["Instances"][0].get("PublicIpAddress")
            if not ip:
                raise ComputeUnavailable("the instance has no public address: use a subnet that gives one")
            node = Node(host=ip, user="ubuntu", id=iid, key_file=self.key_file, known_hosts=self.keys_dir / f"known_hosts_{iid}")
            t0 = time.time()
            while True:     # SSH up, then the image pulled
                try:
                    if ssh(node, "test -e /var/run/bouldermesh-ready && echo ready || echo pulling", timeout=30).strip() == "ready":
                        break
                except (ComputeUnavailable, RuntimeError, subprocess.TimeoutExpired):
                    pass
                if time.time() - t0 > 900:
                    raise ComputeUnavailable(f"{iid} wasn't ready after 15 minutes")
                time.sleep(10)
            log(f"remote: {iid} ready at {ip} after {time.time() - node.started:.0f} s")
            return node
        except BaseException:
            self.stop(node or Node(host="", user="", id=iid))
            raise

    def stop(self, node: Node) -> None:
        if node.id:
            self._client("ec2").terminate_instances(InstanceIds=[node.id])

    def leftovers(self) -> list[str]:
        """This server's instances still running, as after a restart, when their jobs were lost."""
        r = self._client("ec2").describe_instances(Filters=[{"Name": f"tag:{TAG}", "Values": [self.server_id]},
                                                            {"Name": "instance-state-name", "Values": ["pending", "running"]}])
        return [i["InstanceId"] for res in r["Reservations"] for i in res["Instances"]]

    def test(self) -> str:
        """Check the credentials and that an instance could be started, without starting one."""
        ident = self._client("sts").get_caller_identity()
        ec2 = self._client("ec2")
        try:
            ec2.run_instances(DryRun=True, **self._launch_args(ec2))
        except Exception as e:
            if "DryRunOperation" not in str(e):
                raise
        return f"account {ident['Account']}: allowed to start {self.s['compute.aws.instance_type']} from {self._ami()}"


def _key_file(keys_dir: Path, name: str, text: str) -> Path:
    keys_dir.mkdir(parents=True, exist_ok=True)
    path = keys_dir / name
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        f.write(text.strip() + "\n")
    return path


def provider_for(settings: dict, credential: Callable[[str], str | None], keys_dir: Path, server_id: str):
    """The provider the settings ask for, or None to do everything here."""
    kind = settings["compute.provider"]
    if kind == "ssh":
        return SSHProvider(settings["compute.ssh_hosts"], credential("ssh.private_key"), keys_dir)
    if kind == "aws":
        return AWSProvider(settings, {k: credential(k) for k in ("aws.access_key_id", "aws.secret_access_key")}, keys_dir, server_id)
    return None


# ------------------------------------------------------------------ the nodes in use
class Pool:
    """Nodes from a provider: at most `limit` at once, kept while there's work, ended when idle."""

    def __init__(self, provider, limit: int, idle_minutes: float, log: Log = print):
        self.provider, self.limit, self.idle = provider, limit, idle_minutes * 60
        self.nodes: list[Node] = []
        self.starting = 0
        self.error: str | None = None
        self._cond = threading.Condition()
        self.log = log

    def acquire(self, log: Log) -> Node:
        """An idle node, or a new one if under the limit. Raises ComputeUnavailable if one can't be had."""
        with self._cond:
            while True:
                idle = [n for n in self.nodes if not n.busy]
                if idle:
                    idle[0].busy = True
                    return idle[0]
                if len(self.nodes) + self.starting < self.limit:
                    self.starting += 1
                    break
                self._cond.wait(30)
        try:
            node = self.provider.start(log)
        except BaseException as e:
            with self._cond:
                self.starting -= 1
                self.error = f"{time.strftime('%H:%M')}: {e}"
                self._cond.notify_all()
            if isinstance(e, ComputeUnavailable):
                raise
            raise ComputeUnavailable(str(e)) from None
        with self._cond:
            self.starting -= 1
            node.busy = True
            self.nodes.append(node)
            self.error = None
        return node

    def release(self, node: Node, broken: bool = False) -> None:
        with self._cond:
            node.busy, node.last_used = False, time.time()
            if broken and node in self.nodes:
                self.nodes.remove(node)
            self._cond.notify_all()
        if broken:
            self._stop(node)

    def _stop(self, node: Node) -> None:
        try:
            self.provider.stop(node)
        except Exception as e:
            self.error = f"{time.strftime('%H:%M')}: couldn't end {node.id or node.host}: {e}"

    def reap(self) -> None:
        """End nodes idle for longer than allowed."""
        with self._cond:
            done = [n for n in self.nodes if not n.busy and time.time() - n.last_used > self.idle]
            for n in done:
                self.nodes.remove(n)
        for n in done:
            self.log(f"compute: ending {n.id or n.host}, idle for {self.idle / 60:.0f} minutes")
            self._stop(n)

    def close(self) -> None:
        """End every node not in use; those in use end when released."""
        self.idle = 0
        self.reap()

    def status(self) -> dict:
        with self._cond:
            return {"limit": self.limit, "starting": self.starting, "error": self.error,
                    "nodes": [{"id": n.id, "host": n.host, "busy": n.busy, "gpu": n.gpu, "up_minutes": round((time.time() - n.started) / 60, 1),
                               "idle_minutes": None if n.busy else round((time.time() - n.last_used) / 60, 1)} for n in self.nodes]}


class RemoteExecutor:
    """Runs pipeline.reconstruct's work on a node from a pool, with the same arguments and result."""

    def __init__(self, pool: Pool, image: str, command: str, jobctl):
        self.pool, self.image, self.command, self.jobctl = pool, image, command, jobctl

    def reconstruct(self, images: Path, work: Path, out_dir: Path, log: Log, use_gpu: bool = True, internet: bool = False,
                    tracker=None) -> tuple[Path, bool]:
        from progress import Tracker
        node = self.pool.acquire(log)
        broken = False
        try:
            return run_remote(node, images, out_dir, log, internet, tracker or Tracker([]), self.image, self.command, self.jobctl)
        except ComputeUnavailable:
            broken = True
            raise
        finally:
            self.pool.release(node, broken)
