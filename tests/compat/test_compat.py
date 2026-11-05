"""Unit tests for tests/compat/compat.py. Run: python3 -m unittest discover -s tests/compat -v"""
import importlib.util
import json
import os
import stat
import sys
import tempfile
import unittest
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
_spec = importlib.util.spec_from_file_location("compat", os.path.join(HERE, "compat.py"))
compat = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(compat)

AWS_HOST = "ama" + "zonaws.com"
GOOD = {
    "VWS_COMPAT_TARGET": "fake",
    "VWS_COMPAT_ALLOWED_HOSTS": "127.0.0.1, ec2.vws.lab",
    "VWS_COMPAT_EC2_ENDPOINT": "http://127.0.0.1:8081/",
    "VWS_COMPAT_S3_ENDPOINT": "https://ec2.vws.lab:8082",
    "VWS_COMPAT_ACCESS_KEY_ID": "AKIDEXAMPLE",
    "VWS_COMPAT_SECRET_ACCESS_KEY": "s3cr3t-value",
}


class ConfigTest(unittest.TestCase):
    def test_good_config_strips_trailing_slash(self):
        cfg = compat.load_config(GOOD)
        self.assertEqual(cfg.ec2_endpoint, "http://127.0.0.1:8081")
        self.assertEqual(cfg.region, "vws-1")
        self.assertEqual(cfg.instance_type, "t3.micro")

    def test_rejects_bad_input(self):
        cases = {
            "aws-owned host": {"VWS_COMPAT_EC2_ENDPOINT": f"https://ec2.us-east-1.{AWS_HOST}",
                               "VWS_COMPAT_ALLOWED_HOSTS": f"ec2.us-east-1.{AWS_HOST}"},
            "aws-owned with trailing dot": {"VWS_COMPAT_S3_ENDPOINT": f"https://s3.{AWS_HOST}.",
                                            "VWS_COMPAT_ALLOWED_HOSTS": f"s3.{AWS_HOST}"},
            "host not allowlisted": {"VWS_COMPAT_EC2_ENDPOINT": "http://10.0.0.1:8081"},
            "not http": {"VWS_COMPAT_EC2_ENDPOINT": "ftp://127.0.0.1"},
            "userinfo": {"VWS_COMPAT_EC2_ENDPOINT": "http://u:p@127.0.0.1:8081"},
            "bad target": {"VWS_COMPAT_TARGET": "prod"},
            "empty secret": {"VWS_COMPAT_SECRET_ACCESS_KEY": "  "},
            "bad image": {"VWS_COMPAT_IMAGE_ID": "ami-$(id)"},
            "bad region": {"VWS_COMPAT_REGION": "us east"},
            "bad instance type": {"VWS_COMPAT_INSTANCE_TYPE": "t3 micro"},
        }
        for name, override in cases.items():
            with self.subTest(name), self.assertRaises(compat.ConfigError):
                compat.load_config({**GOOD, **override})

    def test_child_env_takes_nothing_from_the_runner(self):
        cfg = compat.load_config(GOOD)
        with tempfile.TemporaryDirectory() as work, mock.patch.dict(
            os.environ, {"AWS_PROFILE": "prod", "AWS_ACCESS_KEY_ID": "AKIAREAL", "AWS_SESSION_TOKEN": "tok"}
        ):
            env = compat.child_env(cfg, work, "vws-compat-1")
            self.assertNotIn("AWS_PROFILE", env)
            self.assertNotIn("AWS_SESSION_TOKEN", env)
            self.assertEqual(env["AWS_ACCESS_KEY_ID"], "AKIDEXAMPLE")
            self.assertEqual(env["AWS_SHARED_CREDENTIALS_FILE"], os.devnull)
            self.assertEqual(env["AWS_ENDPOINT_URL"], cfg.ec2_endpoint)
            self.assertTrue(env["HOME"].startswith(work))
            with open(env["AWS_CONFIG_FILE"], encoding="utf-8") as f:
                self.assertIn("addressing_style = path", f.read())


class RunnerTest(unittest.TestCase):
    def test_detail_redacts_and_truncates(self):
        r = compat.Runner({}, ".", ["s3cr3t-value"])
        self.assertEqual(r.detail("bad  key\ns3cr3t-value"), "bad key <redacted>")
        self.assertEqual(len(r.detail("x" * 5000)), compat.DETAIL_MAX)

    def test_timeout_is_a_fail(self):
        r = compat.Runner(dict(os.environ), ".", [])
        with self.assertRaisesRegex(compat.Fail, "timed out after 1s"):
            r.run([sys.executable, "-c", "import time; time.sleep(5)"], 1)

    def test_aws_error_is_a_fail_with_stderr(self):
        with tempfile.TemporaryDirectory() as d:
            stub(d, "aws", "import sys; sys.stderr.write('An error occurred (AuthFailure)'); sys.exit(254)")
            r = compat.Runner({"PATH": d}, d, [])
            with self.assertRaisesRegex(compat.Fail, "AuthFailure"):
                r.aws("ec2", "describe-vpcs")


class MatrixTest(unittest.TestCase):
    def test_step_records_pass_fail_and_skip(self):
        m = compat.Matrix("aws-cli")
        self.assertEqual(m.step("vpc", "create-vpc", lambda: "vpc-1"), "vpc-1")
        self.assertIsNone(m.step("vpc", "describe-vpcs", lambda: compat.expect(False, "missing")))
        self.assertIsNone(m.step("subnet", "create-subnet", lambda: "x", needs=[("vpc", None)]))
        self.assertEqual([(r.result, r.detail) for r in m.rows],
                         [("pass", ""), ("fail", "missing"), ("skip", "needs vpc")])

    def test_row_rejects_unknown_resource_and_result(self):
        with self.assertRaises(ValueError):
            compat.Row("aws-cli", "lambda", "invoke", "pass")
        with self.assertRaises(ValueError):
            compat.Row("aws-cli", "vpc", "create-vpc", "ok")


class FakeAws:
    """Answers the aws-cli calls of awscli_scenarios from a table; fail_on names actions that error."""

    def __init__(self, fail_on=()):
        self.fail_on = set(fail_on)
        self.calls = []
        self.objects = {}

    def aws(self, *args, timeout=180):
        self.calls.append(args)
        key = " ".join(args[:2])
        if key in self.fail_on:
            raise compat.Fail(f"{key} refused")
        return {
            "ec2 describe-images": {"Images": [{"ImageId": "ami-0123456789abcdef0"}]},
            "ec2 create-vpc": {"Vpc": {"VpcId": "vpc-1"}},
            "ec2 describe-vpcs": {"Vpcs": [{"VpcId": "vpc-1"}]},
            "ec2 create-subnet": {"Subnet": {"SubnetId": "subnet-1"}},
            "ec2 describe-subnets": {"Subnets": [{"SubnetId": "subnet-1"}]},
            "ec2 create-security-group": {"GroupId": "sg-1"},
            "ec2 describe-security-groups": {"SecurityGroups": [{"IpPermissions": [{"FromPort": 22}]}]},
            "ec2 run-instances": {"Instances": [{"InstanceId": "i-1"}]},
            "ec2 describe-instances": {"Reservations": [{"Instances": [
                {"InstanceId": "i-1", "Placement": {"AvailabilityZone": "vws-1a"}}]}]},
            "ec2 create-volume": {"VolumeId": "vol-1"},
            "ec2 describe-volumes": {"Volumes": [{"VolumeId": "vol-1"}]},
            "ec2 allocate-address": {"AllocationId": "eipalloc-1"},
            "ec2 associate-address": {"AssociationId": "eipassoc-1"},
            "ec2 describe-addresses": {"Addresses": [{"InstanceId": "i-1"}]},
            "s3api list-buckets": {"Buckets": [{"Name": self.bucket()}]},
            "s3api head-object": self.head(args),
            "s3api create-multipart-upload": {"UploadId": "up-1"},
            "s3api upload-part": {"ETag": '"e"'},
            "s3api list-objects-v2": self.listing(args),
        }.get(key, {}) if not self.download(args) else {}

    def bucket(self):
        return next((c[2][5:] for c in self.calls if c[:2] == ("s3", "mb")), "")

    def head(self, args):
        if "--key" not in args:
            return {}
        key = args[args.index("--key") + 1]
        return {"ContentLength": compat.MULTIPART_PART + 1024 if key == "big.bin" else self.objects.get(key, 0)}

    def listing(self, args):
        if "--prefix" in args:
            return {}
        return {"Contents": [{"Key": "small.txt"}]}

    def download(self, args):
        if args[:2] != ("s3", "cp"):
            return False
        src, dst = args[2], args[3]
        if src.startswith("s3://"):
            with open(self.uploaded, "rb") as a, open(dst, "wb") as b:
                b.write(a.read())
        else:
            self.uploaded = src
            self.objects["small.txt"] = os.path.getsize(src)
        return True


class ScenarioTest(unittest.TestCase):
    def setUp(self):
        self.cfg = compat.load_config(GOOD)
        self.work = tempfile.mkdtemp()

    def test_every_row_passes_on_a_compatible_endpoint(self):
        rows, left, image = compat.awscli_scenarios(FakeAws(), self.cfg, "vws-compat-1", self.work)
        self.assertEqual([r for r in rows if r.result != "pass"], [])
        self.assertEqual(left, [])
        self.assertEqual(image, "ami-0123456789abcdef0")
        self.assertEqual(len({(r.resource, r.action) for r in rows}), len(rows), "row keys are unique")

    def test_failed_create_skips_dependents_and_keeps_independent_rows(self):
        rows, left, _ = compat.awscli_scenarios(FakeAws(fail_on={"ec2 run-instances"}), self.cfg, "x", self.work)
        by = {(r.resource, r.action): r for r in rows}
        self.assertEqual(by[("instance", "run-instances")].result, "fail")
        self.assertEqual(by[("volume", "attach-volume")].detail, "needs volume, instance")
        self.assertEqual(by[("address", "release-address")].result, "pass")
        self.assertEqual(by[("vpc", "delete-vpc")].result, "pass")
        self.assertEqual(by[("bucket", "rb")].result, "pass")
        self.assertEqual(left, [])

    def test_failed_delete_is_retried_by_cleanup_and_reported(self):
        fake = FakeAws(fail_on={"ec2 delete-vpc"})
        rows, left, _ = compat.awscli_scenarios(fake, self.cfg, "x", self.work)
        self.assertEqual([r.action for r in rows if r.result == "fail"], ["delete-vpc"])
        self.assertEqual(sum(1 for c in fake.calls if c[:2] == ("ec2", "delete-vpc")), 2)
        self.assertEqual(left, ["vpc-1: ec2 delete-vpc refused"])

    def test_given_image_id_wins_over_describe_images(self):
        cfg = compat.load_config({**GOOD, "VWS_COMPAT_IMAGE_ID": "ami-00000000000000001"})
        _, _, image = compat.awscli_scenarios(FakeAws(), cfg, "x", self.work)
        self.assertEqual(image, "ami-00000000000000001")


TF_APPLY = "\n".join(json.dumps(e) for e in [
    {"type": "version"},
    {"type": "apply_complete", "hook": {"resource": {"addr": "aws_s3_bucket.this"}, "action": "create"}},
    {"type": "diagnostic", "@level": "error",
     "diagnostic": {"summary": "creating EC2 VPC", "detail": "AuthFailure key=s3cr3t-value", "address": "aws_vpc.this"}},
    {"type": "diagnostic", "@level": "warning", "diagnostic": {"summary": "w", "address": "aws_eip.this"}},
]) + "\nnot json\n"


class TerraformParseTest(unittest.TestCase):
    def test_errors_and_completions(self):
        r = compat.Runner({}, ".", ["s3cr3t-value"])
        self.assertEqual(compat.tf_errors(TF_APPLY, r), {"aws_vpc.this": "creating EC2 VPC: AuthFailure key=<redacted>"})
        self.assertEqual(compat.tf_completed(TF_APPLY, "create"), {"aws_s3_bucket.this"})
        self.assertEqual(compat.tf_completed(TF_APPLY, "delete"), set())

    def test_config_resources_all_have_owners(self):
        types = compat.tf_addresses(os.path.join(HERE, "terraform"))
        self.assertGreaterEqual(len(types), 10)
        self.assertEqual([t for t in types if t not in compat.OWNERS], [])

    def test_init_retries_then_reports(self):
        with tempfile.TemporaryDirectory() as d:
            stub(d, "terraform", "import sys; sys.stderr.write('TLS handshake timeout'); sys.exit(1)")
            r = compat.Runner({"PATH": d}, d, [])
            with mock.patch.object(compat.time, "sleep") as sleep:
                self.assertEqual(compat.tf_init(r, d), "TLS handshake timeout")
            self.assertEqual([c.args for c in sleep.call_args_list], [(10,), (20,)])


def doc(target="vws"):
    rows = [
        compat.Row("aws-cli", "instance", "run-instances", "fail", "InvalidParameterValue"),
        compat.Row("aws-cli", "instance", "describe-instances", "skip", "needs instance"),
        compat.Row("aws-cli", "vpc", "create-vpc", "pass"),
        compat.Row("terraform", "aws_vpc", "apply", "fail", "a | b"),
    ]
    return {"target": target, "run_id": "vws-compat-1", "started": "2026-11-05T00:00:00Z",
            "tools": {"aws-cli": "2.39.4", "terraform": "1.17.1"}, "leftovers": ["vpc-1: refused"],
            "rows": [dict(compat.dataclasses.asdict(r), owner=r.owner) for r in rows]}


class OutputTest(unittest.TestCase):
    def test_matrix_counts_escapes_and_lists_leftovers(self):
        md = compat.render_matrix(doc())
        self.assertIn("pass 1, fail 2, skip 1.", md)
        self.assertIn("| terraform | aws_vpc | apply | fail | vws-network | a \\| b |", md)
        self.assertIn("- vpc-1: refused", md)

    def test_plan_issues_one_per_failing_row(self):
        planned = compat.plan_issues(doc())
        self.assertEqual([(o, t) for o, t, _ in planned], [
            ("vws-compute", "compat: aws-cli instance run-instances fails against VWS"),
            ("vws-network", "compat: terraform aws_vpc apply fails against VWS"),
        ])
        self.assertIn("Part of popododo0720/vws-deploy#133", planned[0][2])
        self.assertNotRegex(planned[0][2], r"(?i)\b(closes|fixes|resolves)\b")

    def test_issues_refuse_a_fake_run(self):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "results.json")
            with open(path, "w", encoding="utf-8") as f:
                json.dump(doc("fake"), f)
            self.assertEqual(compat.cmd_issues(path, True, "popododo0720"), 2)

    def test_issues_apply_opens_only_missing_ones(self):
        with tempfile.TemporaryDirectory() as d:
            log = os.path.join(d, "gh.log")
            open_issues = os.path.join(d, "open.json")
            with open(open_issues, "w", encoding="utf-8") as f:
                json.dump([["popododo0720/vws-compute", "compat: aws-cli instance run-instances fails against VWS"]], f)
            stub(d, "gh", f"""import json, sys
args = sys.argv[1:]
open({log!r}, "a").write(json.dumps(args) + "\\n")
known = json.load(open({open_issues!r}))
repo = args[args.index("-R") + 1]
if args[:2] == ["issue", "list"]:
    print(json.dumps([{{"title": t, "url": "u"}} for r, t in known if r == repo]))
else:
    known.append([repo, args[args.index("--title") + 1]])
    json.dump(known, open({open_issues!r}, "w"))
    print("https://github.com/x/y/issues/9")
""")
            path = os.path.join(d, "results.json")
            with open(path, "w", encoding="utf-8") as f:
                json.dump(doc(), f)
            with mock.patch.dict(os.environ, {"PATH": d + os.pathsep + os.environ["PATH"]}):
                self.assertEqual(compat.cmd_issues(path, True, "popododo0720"), 0)
                self.assertEqual(compat.cmd_issues(path, True, "popododo0720"), 0)
            with open(log, encoding="utf-8") as f:
                calls = [json.loads(line) for line in f]
            creates = [c for c in calls if c[:2] == ["issue", "create"]]
            self.assertEqual(len(creates), 1, "the second run finds the issue the first one opened")
            self.assertIn("popododo0720/vws-network", creates[0])


def stub(directory, name, body):
    path = os.path.join(directory, name)
    with open(path, "w", encoding="utf-8") as f:
        f.write(f"#!{sys.executable}\n{body}\n")
    os.chmod(path, os.stat(path).st_mode | stat.S_IEXEC)


if __name__ == "__main__":
    unittest.main()
