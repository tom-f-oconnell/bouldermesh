"""EC2 machines that connect here for work, started and ended by hand from the admin page.

With machines that connect here (compute.provider "pull"), the admin page can also start an EC2 GPU instance. It runs
the worker script with a token of its own, so it works like any other machine there, alongside the rest. Nothing
starts or ends one by itself. It runs until it's ended from the admin page, either at once (its climb goes back to
wait for another machine) or once its climb is done (it's given no new one meanwhile).

AWS can still end one itself, as when it takes back a spot instance. That's noticed within a minute: its token is
revoked, and you're told.

What they cost is taken from a balance you top up on the admin page, estimated here each minute from each instance's
price (on-demand from AWS's price list; spot from its zone's price, looked up hourly), its disk and its public address.
Nothing starts without an hour's worth left. At WARN of a top-up left you're told; at none, the machines are given no
new climb and end once theirs is done; at OVERRUN dollars below none, they end at once. A top-up lets those not yet
ended carry on.

Instances are tagged TAG with this server's id, a tag of their own: the "aws" provider (compute.py) ends any instance
with compute.TAG left from before a restart, and these are kept running.
"""
from __future__ import annotations

import json
import re
import shlex
import sqlite3
import threading
import time
from pathlib import Path
from typing import Callable

import compute

TAG = "bouldermesh-pull"
MACHINE_TAG = "bouldermesh-machine"
HOME = "/opt/bouldermesh"
USER = "ubuntu"             # the Deep Learning AMIs' (Ubuntu) user
NEW_GRACE = 300             # seconds an instance may be missing from AWS's answers just after it starts
ROOM_GB = 60                # at least this much disk beyond the AMI's own: the image (pulled, about 4x its download, as the
                            # script checks), two climbs and the models
ENDED_KEPT = 7 * 86400      # ended ones shown this long
ENDING = ("shutting-down", "terminated", "stopping", "stopped")
DISK_RATE = 0.08 / 730      # dollars a GB-hour of gp3 (us-east-1's $0.08 a GB-month; a little more in some regions)
ADDRESS_RATE = 0.005        # dollars an hour for its public IPv4 address
SPOT_EVERY = 3600           # seconds between looking up a spot instance's price again
MIN_HOURS = 1               # a new instance needs this many hours of itself left in the balance
WARN = 0.2                  # you're told when this share of the balance after the last top-up is left
OVERRUN = 2.0               # dollars below none at which the machines end at once, climbs or not


class Fleet:
    """The EC2 machines this server started for its machines that connect here.

    Args:
        db_path: where it keeps them.
        board: the machines that connect here (pullwork.Board), which makes each one's token.
        settings: the admin page's compute settings (compute.aws.*).
        creds: the AWS credentials saved on the admin page, if any.
        server_id: this server's id, for the instances' tag.
        url: the address machines reach this server at (WORKER_URL), or None.
        notify: called with (title, text) when AWS ends one itself, and as the balance runs down.
        client: makes a boto3 client for a service; compute.aws_client() by default (AWS's price list in us-east-1).
    """

    def __init__(self, db_path: Path, board, settings: Callable[[], dict], creds: Callable[[], dict],
                 server_id: Callable[[], str], url: Callable[[], str | None],
                 notify: Callable[[str, str], None] = lambda title, text: None, log: Callable[[str], None] = print,
                 client: Callable[[str], object] | None = None):
        self.db_path, self.board, self.settings, self.creds = db_path, board, settings, creds
        self.server_id, self.url, self.notify, self.log = server_id, url, notify, log
        self.client = client or (lambda service: compute.aws_client(
            {**self.settings(), "compute.aws.region": "us-east-1"} if service == "pricing" else self.settings(), self.creds(), service))
        self.lock = threading.RLock()
        self._prices: dict[tuple[str, str], float] = {}     # on-demand prices found, by (type, region)
        db_path.parent.mkdir(parents=True, exist_ok=True)
        with self.db() as con:
            con.execute("""CREATE TABLE IF NOT EXISTS ec2(instance_id TEXT PRIMARY KEY, machine_id TEXT, name TEXT,
                             instance_type TEXT, spot INTEGER, region TEXT, zone TEXT, price REAL, started REAL, state TEXT,
                             address TEXT, stop_after_job REAL, ended REAL, ended_why TEXT)""")
            have = {r[1] for r in con.execute("PRAGMA table_info(ec2)")}
            for col, kind in (("volume_gb", "INTEGER"), ("price_at", "REAL"), ("cost", "REAL DEFAULT 0"), ("costed_at", "REAL"),
                              ("stop_why", "TEXT")):
                if col not in have:
                    con.execute(f"ALTER TABLE ec2 ADD COLUMN {col} {kind}")
            con.execute("CREATE TABLE IF NOT EXISTS topups(at REAL, amount REAL)")
            # left: the balance just after the last top-up; warned, out: what you've been told since
            con.execute("CREATE TABLE IF NOT EXISTS budget(id INTEGER PRIMARY KEY CHECK (id=1), left REAL, warned INTEGER, out INTEGER)")
            con.execute("INSERT OR IGNORE INTO budget VALUES(1, 0, 0, 0)")
        for r in self.q("SELECT machine_id FROM ec2 WHERE ended IS NULL AND stop_after_job IS NOT NULL"):
            self.board.parked[r["machine_id"]] = PARKED

    def db(self) -> sqlite3.Connection:
        con = sqlite3.connect(self.db_path, timeout=30)
        con.row_factory = sqlite3.Row
        return con

    def q(self, sql: str, *args) -> list[dict]:
        with self.db() as con:
            return [dict(r) for r in con.execute(sql, args)]

    # -- starting one -------------------------------------------------------------------------------------------------
    def _url(self) -> str:
        url = (self.url() or "").rstrip("/")
        if not re.match(r"https?://[^\s'\"]+$", url):
            raise ValueError("this server's address for machines isn't known: set WORKER_URL, VOLUNTEER_URL or SITE_HOST (such as https://boulderme.sh)")
        return url

    def user_data(self, url: str, token: str) -> str:
        """What a new instance runs first: the worker script, as a service that starts again if it stops."""
        args = " ".join(shlex.quote(a) for a in ("--server", url, "--token", token, "--data", f"{HOME}/data"))
        return f"""#!/bin/bash
# a BoulderMesh machine on EC2: the worker script asks {url} for climbs to mesh, until the admin page ends it
exec >> /var/log/bouldermesh-setup.log 2>&1
install -d -o {USER} -g {USER} -m 700 {HOME} {HOME}/home {HOME}/data
usermod -aG docker {USER}
for i in $(seq 30); do
  curl -fsS -o {HOME}/bouldermesh_worker.py {shlex.quote(url + "/worker/bouldermesh_worker.py")} && break
  sleep 20
done
chown {USER}:{USER} {HOME}/bouldermesh_worker.py
cat > /etc/systemd/system/bouldermesh-worker.service <<'EOS'
[Unit]
Description=BoulderMesh worker
After=docker.service network-online.target
Wants=network-online.target docker.service

[Service]
User={USER}
Environment=BOULDERMESH_WORKER_HOME={HOME}/home
ExecStart=/usr/bin/python3 {HOME}/bouldermesh_worker.py {args}
Restart=always
RestartSec=30

[Install]
WantedBy=multi-user.target
EOS
chmod 600 /etc/systemd/system/bouldermesh-worker.service
systemctl daemon-reload
systemctl enable --now bouldermesh-worker
"""

    def launch_args(self, ec2, instance_type: str, spot: bool, user_data: str, machine: dict) -> dict:
        s = self.settings()
        ami = s["compute.aws.ami"] or self.client("ssm").get_parameter(Name=compute.DLAMI)["Parameter"]["Value"]
        image = ec2.describe_images(ImageIds=[ami])["Images"][0]
        root = image["RootDeviceName"]
        least = max([m["Ebs"].get("VolumeSize", 0) for m in image.get("BlockDeviceMappings", [])
                     if m.get("DeviceName") == root and "Ebs" in m] or [0])
        args = {"ImageId": ami, "InstanceType": instance_type, "MinCount": 1, "MaxCount": 1, "UserData": user_data,
                "InstanceInitiatedShutdownBehavior": "terminate",
                # its jobs' containers can't reach the metadata service, and so its user data with the token
                "MetadataOptions": {"HttpTokens": "required", "HttpPutResponseHopLimit": 1},
                "BlockDeviceMappings": [{"DeviceName": root, "Ebs": {"VolumeSize": max(int(s["compute.aws.volume_gb"]), least + ROOM_GB),
                                                                      "VolumeType": "gp3", "DeleteOnTermination": True}}],
                "TagSpecifications": [{"ResourceType": "instance", "Tags": [
                    {"Key": TAG, "Value": self.server_id()}, {"Key": MACHINE_TAG, "Value": machine["id"]},
                    {"Key": "Name", "Value": f"bouldermesh {machine['name']}"}]}]}
        if spot:
            args["InstanceMarketOptions"] = {"MarketType": "spot", "SpotOptions": {"SpotInstanceType": "one-time",
                                                                                     "InstanceInterruptionBehavior": "terminate"}}
        if s["compute.aws.security_group"]:
            args["SecurityGroupIds"] = [s["compute.aws.security_group"]]
        if s["compute.aws.subnet"]:
            args["SubnetId"] = s["compute.aws.subnet"]
        if s["compute.aws.instance_profile"]:
            args["IamInstanceProfile"] = {"Name": s["compute.aws.instance_profile"]}
        return args

    def _choice(self, instance_type: str | None, spot: bool | None) -> tuple[str, bool]:
        s = self.settings()
        itype = (instance_type or "").strip() or s["compute.aws.instance_type"]
        if not re.match(r"^[a-z][a-z0-9-]*\.[a-z0-9]+$", itype):
            raise ValueError(f"{itype!r} isn't an EC2 instance type, such as g4dn.2xlarge")
        return itype, bool(s["compute.aws.spot"] if spot is None else spot)

    def start(self, instance_type: str | None = None, spot: bool | None = None) -> dict:
        """Start an instance, with a machine (and token) of its own, if the balance has an hour of it left: its row."""
        itype, spot = self._choice(instance_type, spot)
        url = self._url()
        region = self.settings()["compute.aws.region"]
        ec2 = self.client("ec2")
        with self.lock:
            price = self._spot_price(ec2, itype) if spot else self._ondemand_price(itype, region)
            if price is None:
                raise ValueError(f"couldn't find what a {itype}{' spot' if spot else ''} instance costs in {region}, so it "
                                 "can't be taken from the balance: the credentials need ec2:DescribeSpotPriceHistory and "
                                 "pricing:GetProducts")
            args = self.launch_args(ec2, itype, spot, "", {"id": "", "name": ""})
            volume = args["BlockDeviceMappings"][0]["Ebs"]["VolumeSize"]
            rate = rate_of(price, volume)
            left = self.balance()["left"]
            if left < rate * MIN_HOURS:
                raise ValueError(f"the EC2 balance has ${left:.2f} left, less than {MIN_HOURS} h of a {itype}"
                                 f"{' spot' if spot else ''} instance (about ${rate:.2f} an hour): top it up first")
            machine = self.board.add(f"EC2 {itype}")
            try:
                args = self.launch_args(ec2, itype, spot, self.user_data(url, machine["token"]), machine)
                iid = ec2.run_instances(**args)["Instances"][0]["InstanceId"]
            except Exception as e:
                self.board.revoke(machine["id"])
                raise ValueError(f"EC2 wouldn't start a {itype}: {e}") from None
            now = time.time()
            self.q("INSERT INTO ec2(instance_id, machine_id, name, instance_type, spot, region, started, state, price, "
                   "price_at, volume_gb, cost, costed_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)", iid, machine["id"],
                   machine["name"], itype, int(spot), region, now, "pending", price, None if spot else now, volume, 0.0, now)
        self.log(f"ec2: started {iid}, {itype}{' (spot)' if spot else ''}, as machine {machine['name']}, ~${rate:.3f}/h")
        return self.row(iid)

    def test(self, instance_type: str | None = None, spot: bool | None = None) -> str:
        """Check the credentials and that an instance could be started, without starting one or making a token."""
        itype, spot = self._choice(instance_type, spot)
        url = self._url()
        account = self.client("sts").get_caller_identity()["Account"]
        ec2 = self.client("ec2")
        args = self.launch_args(ec2, itype, spot, self.user_data(url, "bmw_test"), {"id": "test", "name": "test"})
        try:
            ec2.run_instances(DryRun=True, **args)
        except Exception as e:
            if "DryRunOperation" not in str(e):
                raise ValueError(f"EC2 wouldn't start a {itype}{' spot' if spot else ''} instance: {e}") from None
        region = self.settings()["compute.aws.region"]
        price = self._spot_price(ec2, itype) if spot else self._ondemand_price(itype, region)
        cost = (f"about ${rate_of(price, args['BlockDeviceMappings'][0]['Ebs']['VolumeSize']):.3f} an hour with its disk "
                "and address" if price is not None else "its price not found (it needs ec2:DescribeSpotPriceHistory and "
                "pricing:GetProducts), so it wouldn't start")
        return (f"account {account}: allowed to start a {itype}{' spot' if spot else ''} instance from {args['ImageId']}, "
                f"its worker reaching {url}; {cost} (AWS's quota for its kind can still refuse it when it's started)")

    # -- ending one ---------------------------------------------------------------------------------------------------
    def row(self, iid: str) -> dict | None:
        rows = self.q("SELECT * FROM ec2 WHERE instance_id=?", iid)
        return rows[0] if rows else None

    def stop(self, iid: str, after_job: bool = False) -> str:
        """End one: at once, or with after_job once its climb is done (it's given no new one meanwhile)."""
        r = self.row(iid)
        if not r or r["ended"]:
            raise ValueError("no such EC2 machine running")
        if after_job and self.board.busy(r["machine_id"]):
            self._park(r, "asked")
            return f"{r['name']} ({iid}) is given no new climb, and ends once its climb is done"
        self._end(r, "ended from the admin page")
        return f"ended {r['name']} ({iid})" + ("; its climb waits for another machine" if self.board.busy(r["machine_id"]) else "")

    def _park(self, r: dict, why: str) -> None:
        self.q("UPDATE ec2 SET stop_after_job=?, stop_why=? WHERE instance_id=?", time.time(), why, r["instance_id"])
        self.board.parked[r["machine_id"]] = PARKED

    def _end(self, r: dict, why: str, terminate: bool = True) -> None:
        self._accrue()
        if terminate:
            try:
                self.client("ec2").terminate_instances(InstanceIds=[r["instance_id"]])
            except Exception as e:
                if "NotFound" not in str(e):
                    raise
        self.board.parked.pop(r["machine_id"], None)
        self.board.revoke(r["machine_id"])
        self.q("UPDATE ec2 SET ended=?, ended_why=? WHERE instance_id=?", time.time(), why, r["instance_id"])
        self.log(f"ec2: {r['name']} ({r['instance_id']}) {why}")

    # -- every minute -------------------------------------------------------------------------------------------------
    def sweep(self) -> None:
        """Take what they cost from the balance, and stop them as it runs out; end those waiting for their climb to be
        done once it is; and notice any AWS ended itself, and their addresses and spot prices."""
        with self.lock:
            self._sweep()

    def _sweep(self) -> None:
        live = self.q("SELECT * FROM ec2 WHERE ended IS NULL")
        if not live:
            return
        self._enforce(live)
        live = self.q("SELECT * FROM ec2 WHERE ended IS NULL")
        for r in live:
            if r["stop_after_job"]:
                self.board.parked[r["machine_id"]] = PARKED
                if not self.board.busy(r["machine_id"]):
                    self._end(r, "ended once its climb was done, as the EC2 balance ran out" if r["stop_why"] == "balance"
                              else "ended once its climb was done, as asked")
        live = [r for r in live if not self.row(r["instance_id"])["ended"]]
        if not live:
            return
        ec2 = self.client("ec2")
        found = {i["InstanceId"]: i for res in ec2.describe_instances(Filters=[{"Name": f"tag:{TAG}", "Values": [self.server_id()]}])
                 ["Reservations"] for i in res["Instances"]}
        now = time.time()
        for r in live:
            i = found.get(r["instance_id"])
            if i is None and now - r["started"] < NEW_GRACE:
                continue
            state = i["State"]["Name"] if i else "gone"
            if state in ENDING or i is None:
                reason = ((i or {}).get("StateReason") or {}).get("Message") or "it's no longer there"
                self._end(r, f"ended by AWS ({reason})", terminate=state in ("stopping", "stopped"))
                self.notify(f"EC2 machine {r['name']} ended", f"AWS ended {r['instance_id']} ({r['instance_type']}"
                            f"{', spot' if r['spot'] else ''}) after {(now - r['started']) / 3600:.1f} h: {reason}. "
                            "Its climb, if any, waits for another machine.")
                continue
            zone = (i.get("Placement") or {}).get("AvailabilityZone")
            price, price_at = r["price"], r["price_at"]
            if r["spot"] and zone and (price_at is None or now - price_at > SPOT_EVERY):
                found_price = self._spot_price(ec2, r["instance_type"], zone)
                if found_price is not None:     # charged at the old price until now (the sweep's _accrue())
                    price, price_at = found_price, now
            self.q("UPDATE ec2 SET state=?, address=?, zone=?, price=?, price_at=? WHERE instance_id=?", state,
                   i.get("PublicIpAddress"), zone, price, price_at, r["instance_id"])

    # -- what they cost -----------------------------------------------------------------------------------------------
    def _spot_price(self, ec2, itype: str, zone: str | None = None) -> float | None:
        """A spot instance's price now in a zone, or without one the dearest of the region's zones."""
        kw = {"AvailabilityZone": zone} if zone else {}
        try:
            h = ec2.describe_spot_price_history(InstanceTypes=[itype], ProductDescriptions=["Linux/UNIX"],
                                                StartTime=time.time(), **kw)
            prices = [float(p["SpotPrice"]) for p in h["SpotPriceHistory"]]
            return max(prices) if prices else None
        except Exception:
            return None

    def _ondemand_price(self, itype: str, region: str) -> float | None:
        """An on-demand Linux instance's price an hour, from AWS's price list."""
        if (itype, region) in self._prices:
            return self._prices[(itype, region)]
        filters = [{"Type": "TERM_MATCH", "Field": f, "Value": v} for f, v in (
            ("instanceType", itype), ("regionCode", region), ("operatingSystem", "Linux"), ("tenancy", "Shared"),
            ("preInstalledSw", "NA"), ("capacitystatus", "Used"), ("licenseModel", "No License required"))]
        try:
            items = self.client("pricing").get_products(ServiceCode="AmazonEC2", Filters=filters, MaxResults=20)["PriceList"]
        except Exception:
            return None
        prices = [float(d["pricePerUnit"]["USD"]) for item in items
                  for term in (json.loads(item) if isinstance(item, str) else item).get("terms", {}).get("OnDemand", {}).values()
                  for d in term.get("priceDimensions", {}).values() if float(d.get("pricePerUnit", {}).get("USD", 0)) > 0]
        if not prices:
            return None
        self._prices[(itype, region)] = max(prices)
        return max(prices)

    def _accrue(self) -> None:
        """Charge each running one for its time since it was last charged, at its rate."""
        now = time.time()
        with self.lock:
            for r in self.q("SELECT * FROM ec2 WHERE ended IS NULL"):
                since = r["costed_at"] or r["started"]
                if now > since:
                    self.q("UPDATE ec2 SET cost=COALESCE(cost, 0)+?, costed_at=? WHERE instance_id=?",
                           (now - since) / 3600 * rate_of(r["price"], r["volume_gb"]), now, r["instance_id"])

    def balance(self) -> dict:
        """{topped (all top-ups), spent (by every instance), left, per_hour (the running ones'), hours_left (at that
        rate, or None), last (the balance just after the last top-up)}."""
        with self.lock:
            self._accrue()
            topped = self.q("SELECT COALESCE(SUM(amount), 0) AS t FROM topups")[0]["t"]
            spent = self.q("SELECT COALESCE(SUM(cost), 0) AS s FROM ec2")[0]["s"]
            per_hour = sum(rate_of(r["price"], r["volume_gb"]) for r in self.q("SELECT * FROM ec2 WHERE ended IS NULL"))
            left = topped - spent
            return {"topped": round(topped, 2), "spent": round(spent, 4), "left": round(left, 4), "per_hour": round(per_hour, 4),
                    "hours_left": round(max(left, 0) / per_hour, 1) if per_hour else None,
                    "last": self.q("SELECT left FROM budget")[0]["left"]}

    def top_up(self, amount: float) -> str:
        """Add to the balance (less than none takes away); machines stopping as it ran out carry on if it's above none."""
        amount = float(amount)
        if not amount or abs(amount) > 10000 or amount != amount:
            raise ValueError("give an amount in dollars, such as 50 (or -10 to take some away)")
        with self.lock:
            self.q("INSERT INTO topups VALUES(?, ?)", time.time(), amount)
            left = self.balance()["left"]
            self.q("UPDATE budget SET left=?, warned=0, out=0", left)
            carried = 0
            if left > 0:
                for r in self.q("SELECT * FROM ec2 WHERE ended IS NULL AND stop_why='balance'"):
                    self.q("UPDATE ec2 SET stop_after_job=NULL, stop_why=NULL WHERE instance_id=?", r["instance_id"])
                    self.board.parked.pop(r["machine_id"], None)
                    carried += 1
        self.log(f"ec2: balance {'topped up by' if amount > 0 else 'lowered by'} ${abs(amount):.2f}, ${left:.2f} left")
        return f"${left:.2f} left" + (f"; {carried} machine(s) stopping as it ran out carry on" if carried else "")

    def _enforce(self, live: list[dict]) -> None:
        """Tell you as the balance runs low; at none, give the machines no new climb (ending once theirs is done); at
        OVERRUN below, end them at once."""
        b = self.balance()
        flags = self.q("SELECT * FROM budget")[0]
        left, rate = b["left"], f"${b['per_hour']:.2f} an hour"
        if left <= -OVERRUN:
            for r in live:
                self._end(r, f"ended at once, ${-left:.2f} past the end of the EC2 balance")
            self.notify("EC2 balance spent: machines ended", f"${-left:.2f} past the end of the balance, so {len(live)} "
                        "EC2 machine(s) ended at once; their climbs wait for other machines. Top it up to start more.")
        elif left <= 0:
            parking = [r for r in live if not r["stop_after_job"]]
            for r in parking:
                self._park(r, "balance")
            if not flags["out"]:
                self.q("UPDATE budget SET out=1")
                self.notify("EC2 balance spent", f"The EC2 balance is spent ({rate} was running): {len(live)} machine(s) get "
                            f"no new climb and end once theirs is done, or at once ${OVERRUN:.0f} past it. A top-up lets them carry on.")
        elif left <= WARN * (flags["left"] or 0) and not flags["warned"]:
            self.q("UPDATE budget SET warned=1")
            self.notify("EC2 balance running low", f"${left:.2f} left of the EC2 balance, at {rate}: about "
                        f"{b['hours_left']} h. Top it up on the admin page to keep the machines going.")

    def state(self) -> dict:
        """For the admin page: those running, and those ended in the last week, newest first, each with hours up and
        cost, and the balance."""
        now = time.time()
        balance = self.balance()
        rows = self.q("SELECT * FROM ec2 WHERE ended IS NULL OR ended>? ORDER BY started DESC", now - ENDED_KEPT)
        for r in rows:
            r["hours"] = round(((r["ended"] or now) - r["started"]) / 3600, 2)
            r["rate"] = round(rate_of(r["price"], r["volume_gb"]), 4) if r["price"] is not None else None
            r["cost"] = round(r["cost"] or 0, 2)
        return {"machines": rows, "url": self.url() or None, "balance": balance}


def rate_of(price: float | None, volume_gb: int | None) -> float:
    """Dollars an hour for an instance at this price, with its disk and public address."""
    return (price or 0) + (volume_gb or 0) * DISK_RATE + ADDRESS_RATE


PARKED = "to be ended once its climb is done (EC2)"
