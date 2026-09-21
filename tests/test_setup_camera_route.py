"""The camera-route script must never let the service be reachable unprotected.

`deployment/cloudflare/setup-camera-route.py` publishes the Pi's camera-control
service through a Cloudflare tunnel. Done in the wrong order that puts a camera
on the internet with nothing in front of it, and done carelessly it prints a
credential into a terminal scrollback. So these tests drive the script's real
`main()` against a fake Cloudflare: an `http.server` on loopback that keeps the
account's state, answers the API the way the documented one does, and answers
the outside probe according to whether an Access application covers the
hostname. Nothing here reaches Cloudflare, and nothing here is a credential:
the token and secret below are made-up strings the fake hands out.
"""
import contextlib
import importlib.util
import io
import json
import logging
import os
import re
import subprocess
import sys
import tempfile
import threading
import unittest
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

REPOSITORY_ROOT = Path(__file__).resolve().parent.parent
SCRIPT = REPOSITORY_ROOT / "deployment" / "cloudflare" / "setup-camera-route.py"

_spec = importlib.util.spec_from_file_location("setup_camera_route", SCRIPT)
route = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(route)

# Made up, and shaped like the real things only in length.
API_MARK = "fixtureApiMarkNotReal0123456789abcdefghijklmnop"
ACCESS_MARKS = ["fixtureAccessMarkNotReal%02d" % n + "f" * 40 for n in range(8)]
CLIENT_ID = "0123456789abcdef0123456789abcdef.access"

ACCOUNT = "a" * 32
ZONE = "b" * 32
TUNNEL = "11111111-2222-4333-8444-555555555555"
HOSTNAME = "gate-camera.shopshield.app"
ANCHOR = "gate-command.shopshield.app"
CONFIG_PATH = f"/accounts/{ACCOUNT}/cfd_tunnel/{TUNNEL}/configurations"

ORIGINAL_INGRESS = [
    {"hostname": ANCHOR, "service": "http://127.0.0.1:8765",
     "originRequest": {"connectTimeout": 10, "noTLSVerify": False}},
    {"hostname": "gate-mate-media-origin.shopshield.app", "service": "http://127.0.0.1:8891",
     "path": "", "somethingNew": {"kept": [1, 2, 3]}},
    {"service": "http_status:404"},
]


class FakeCloudflare:
    """One account's worth of state, and a log of every request made to it."""

    def __init__(self):
        self.lock = threading.Lock()
        self.log = []  # (method, path) with the /client/v4 prefix removed
        self.bodies = []  # raw request bodies and header blocks, for the leak tests
        self.token_status = "active"
        self.config_src = "cloudflare"
        self.config = {"ingress": json.loads(json.dumps(ORIGINAL_INGRESS)),
                       "warp-routing": {"enabled": False}}
        self.version = 2
        self.dns = [{"id": "d" * 32, "type": "CNAME", "name": ANCHOR, "proxied": True,
                     "content": f"{TUNNEL}.cfargotunnel.com"},
                    {"id": "e" * 32, "type": "A", "name": "shopshield.app", "proxied": True,
                     "content": "192.0.2.1"}]
        self.tokens, self.policies, self.apps = [], [], []
        self.secrets = {"PI_COMMAND_ACCESS_CLIENT_SECRET": "kept", "SESSION_KEY": "kept"}
        self.serial = 0
        self.access_enforced = True
        self.origin_status = 200
        self.fail = []  # [(method, regex, times_left)] -> HTTP 500 before doing anything
        self.fail_echo = False  # the 500 quotes the request's credentials back
        self.drop = []  # [(method, regex)] -> close the socket without answering
        self.mutate_config_on_get = None  # after this many GETs of the config, change it
        self.config_gets = 0

    def new_id(self):
        self.serial += 1
        return "%032x" % self.serial

    # -- the outside view ------------------------------------------------------

    def probe(self, headers):
        rule = next((r for r in self.config["ingress"] if r.get("hostname") == HOSTNAME), None)
        record = next((r for r in self.dns if r["name"] == HOSTNAME), None)
        application = next((a for a in self.apps if a["domain"] == HOSTNAME), None)
        if application and self.access_enforced:
            for token in self.tokens:
                allowed = any(
                    rule_.get("service_token", {}).get("token_id") == token["id"]
                    for policy in application["policies"] for rule_ in policy["include"])
                if allowed and headers.get("CF-Access-Client-Id") == token["client_id"] \
                        and headers.get("CF-Access-Client-Secret") == token["client_secret"]:
                    break
            else:
                return 403
        if not record or not rule:
            return 404
        return self.origin_status


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *args):
        pass

    def _send(self, status, payload):
        raw = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def _ok(self, result, **extra):
        self._send(200, dict({"success": True, "errors": [], "messages": [],
                              "result": result}, **extra))

    def _error(self, status, code, message):
        self._send(status, {"success": False, "errors": [{"code": code, "message": message}],
                            "result": None})

    def _handle(self):
        fake = self.server.fake
        split = urllib.parse.urlsplit(self.path)
        length = int(self.headers.get("Content-Length") or 0)
        raw_body = self.rfile.read(length) if length else b""
        if not split.path.startswith("/client/v4"):
            with fake.lock:
                fake.log.append(("PROBE", "with-token" if self.headers.get(
                    "CF-Access-Client-Secret") else "bare"))
                status = fake.probe(self.headers)
            return self._send(status, {"probe": status})
        path = split.path[len("/client/v4"):]
        query = urllib.parse.parse_qs(split.query)
        body = json.loads(raw_body) if raw_body else None
        with fake.lock:
            fake.log.append((self.command, path))
            fake.bodies.append(raw_body.decode("utf-8"))
            if self.headers.get("Authorization") != "Bearer " + API_MARK:
                return self._error(403, 9109, "Invalid access token")
            for entry in fake.drop:
                if entry[0] == self.command and re.search(entry[1], path):
                    fake.drop.remove(entry)
                    self.close_connection = True
                    self.connection.close()
                    return None
            for entry in fake.fail:
                if entry[0] == self.command and re.search(entry[1], path) and entry[2] > 0:
                    entry[2] -= 1
                    message = "upstream failure"
                    if fake.fail_echo:
                        # Padded so the token straddles the 200th character, where
                        # the script shortens what it quotes.
                        message += (" " + "x" * 125 + " Authorization: "
                                    + self.headers.get("Authorization", "")
                                    + " body " + raw_body.decode("utf-8"))
                    return self._error(500, 1000, message)
            return self._route(fake, path, query, body)

    do_GET = do_POST = do_PUT = do_DELETE = _handle

    def _route(self, fake, path, query, body):
        method = self.command
        account = f"/accounts/{ACCOUNT}"
        if path == "/user/tokens/verify":
            return self._ok({"id": "f" * 32, "status": fake.token_status})
        if path == "/zones":
            wanted = query.get("name", [None])[0]
            zones = [{"id": ZONE, "name": "shopshield.app", "account": {"id": ACCOUNT}}]
            return self._ok([z for z in zones if wanted in (None, z["name"])],
                            result_info={"page": 1, "total_pages": 1})
        if path == f"/zones/{ZONE}/dns_records":
            if method == "GET":
                wanted = query.get("name.exact", [None])[0]
                return self._ok([r for r in fake.dns if wanted in (None, r["name"])],
                                result_info={"page": 1, "total_pages": 1})
            record = dict(body, id=fake.new_id())
            fake.dns.append(record)
            return self._ok(record)
        match = re.fullmatch(f"/zones/{ZONE}/dns_records/([0-9a-f]{{32}})", path)
        if match and method == "DELETE":
            fake.dns = [r for r in fake.dns if r["id"] != match.group(1)]
            return self._ok({"id": match.group(1)})
        if path == f"{account}/cfd_tunnel/{TUNNEL}":
            return self._ok({"id": TUNNEL, "name": "gate-pi", "config_src": fake.config_src,
                             "status": "healthy", "deleted_at": None})
        if path == CONFIG_PATH:
            if method == "GET":
                fake.config_gets += 1
                if fake.mutate_config_on_get == fake.config_gets:
                    fake.config["ingress"].insert(
                        0, {"hostname": "someone-else.shopshield.app",
                            "service": "http://127.0.0.1:9000"})
                    fake.version += 1
            else:
                fake.config = body["config"]
                fake.version += 1
            return self._ok({"tunnel_id": TUNNEL, "version": fake.version,
                             "source": "cloudflare", "config": fake.config})
        if path == f"{account}/access/service_tokens":
            if method == "GET":
                public = [{k: v for k, v in t.items() if k != "client_secret"}
                          for t in fake.tokens]
                return self._ok(public, result_info={"page": 1, "total_pages": 1})
            token = {"id": fake.new_id(), "name": body["name"], "client_id": CLIENT_ID,
                     "client_secret": ACCESS_MARKS[len(fake.tokens)],
                     "duration": body.get("duration"), "expires_at": "2027-09-21T00:00:00Z"}
            fake.tokens.append(token)
            return self._ok(token)
        match = re.fullmatch(f"{account}/access/service_tokens/([0-9a-f]{{32}})(/rotate)?", path)
        if match:
            token = next((t for t in fake.tokens if t["id"] == match.group(1)), None)
            if not token:
                return self._error(404, 12003, "not found")
            if match.group(2):
                token["rotations"] = token.get("rotations", 0) + 1
                token["client_secret"] = ACCESS_MARKS[4 + token["rotations"] - 1]
                return self._ok(token)
            if method == "DELETE":
                fake.tokens.remove(token)
                return self._ok({"id": token["id"]})
            return self._ok({k: v for k, v in token.items() if k != "client_secret"})
        if path == f"{account}/access/policies":
            if method == "GET":
                return self._ok(fake.policies, result_info={"page": 1, "total_pages": 1})
            policy = dict(body, id=fake.new_id(), exclude=[], require=[], reusable=True)
            fake.policies.append(policy)
            return self._ok(policy)
        match = re.fullmatch(f"{account}/access/policies/([0-9a-f]{{32}})", path)
        if match:
            policy = next((p for p in fake.policies if p["id"] == match.group(1)), None)
            if method == "DELETE":
                fake.policies.remove(policy)
                return self._ok({"id": match.group(1)})
            return self._ok(policy)
        if path == f"{account}/access/apps":
            if method == "GET":
                return self._ok(fake.apps, result_info={"page": 1, "total_pages": 1})
            attached = [dict(next(p for p in fake.policies if p["id"] == link["id"]),
                             precedence=link["precedence"]) for link in body["policies"]]
            application = dict(body, id=fake.new_id(), aud="0" * 64, policies=attached,
                               self_hosted_domains=[body["domain"]],
                               destinations=[{"type": "public", "uri": body["domain"]}])
            fake.apps.append(application)
            return self._ok(application)
        match = re.fullmatch(f"{account}/access/apps/([0-9a-f]{{32}})", path)
        if match:
            application = next((a for a in fake.apps if a["id"] == match.group(1)), None)
            if not application:
                return self._error(404, 12003, "not found")
            if method == "DELETE":
                fake.apps.remove(application)
                return self._ok({"id": match.group(1)})
            return self._ok(application)
        if path == f"{account}/workers/scripts/gate-mate/secrets":
            if method == "GET":
                return self._ok([{"name": name, "type": "secret_text"} for name in fake.secrets])
            if body.get("type") != "secret_text":
                return self._error(400, 10021, "not a secret")
            fake.secrets[body["name"]] = body["text"]
            return self._ok({"name": body["name"], "type": "secret_text"})
        match = re.fullmatch(f"{account}/workers/scripts/gate-mate/secrets/([A-Z_]+)", path)
        if match and method == "DELETE":
            fake.secrets.pop(match.group(1), None)
            return self._ok(None)
        return self._error(404, 7003, "no such route in the fake: " + path)


class Result:
    def __init__(self, code, out, err):
        self.code, self.out, self.err = code, out, err
        self.everything = out + err


class RouteScriptCase(unittest.TestCase):
    def setUp(self):
        self.fake = FakeCloudflare()
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.server.fake = self.fake
        self.server.daemon_threads = True
        threading.Thread(target=self.server.serve_forever, args=(0.02,), daemon=True).start()
        self.base = "http://127.0.0.1:%d" % self.server.server_address[1]
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)
        self.sleeps = []

    def env(self, **extra):
        env = {route.TOKEN_VARIABLE: API_MARK,
               route.TEST_API_VARIABLE: self.base + "/client/v4",
               route.TEST_PROBE_VARIABLE: self.base}
        env.update(extra)
        return env

    def run_main(self, *argv, env=None, typed=HOSTNAME + "\n"):
        out, err = io.StringIO(), io.StringIO()
        code = route.main(list(argv), env=self.env() if env is None else env,
                          stdin=io.StringIO(typed), stdout=out, stderr=err,
                          sleep=self.sleeps.append)
        return Result(code, out.getvalue(), err.getvalue())

    def writes(self):
        return [(m, p) for m, p in self.fake.log if m in ("POST", "PUT", "DELETE")]

    def index_of(self, method, pattern):
        return next(i for i, (m, p) in enumerate(self.fake.log)
                    if m == method and re.search(pattern, p))

    def assert_complete(self):
        fake = self.fake
        self.assertEqual(len(fake.tokens), 1)
        self.assertEqual(len(fake.policies), 1)
        self.assertEqual(len(fake.apps), 1)
        self.assertEqual([r["name"] for r in fake.dns].count(HOSTNAME), 1)
        self.assertEqual([r.get("hostname") for r in fake.config["ingress"]].count(HOSTNAME), 1)
        self.assertEqual(fake.secrets["PI_CAMERA_URL"], "https://" + HOSTNAME)
        self.assertEqual(fake.secrets["PI_CAMERA_ACCESS_CLIENT_ID"], CLIENT_ID)
        self.assertEqual(fake.secrets["PI_CAMERA_ACCESS_CLIENT_SECRET"],
                         fake.tokens[0]["client_secret"])
        self.assertEqual(fake.probe({}), 403)


class DryRunTests(RouteScriptCase):
    def test_dry_run_is_the_default_and_changes_nothing(self):
        result = self.run_main()
        self.assertEqual(result.code, 0, result.everything)
        self.assertEqual(self.writes(), [])
        self.assertIn("Dry run: nothing was changed", result.out)
        self.assertIn("CREATE  service token gate-mate-worker-camera", result.out)
        self.assertIn("CREATE  ingress gate-camera.shopshield.app -> http://127.0.0.1:8767",
                      result.out)
        self.assertIn("SESSION_KEY", result.out)  # names of the secrets it will leave alone

    def test_rollback_without_apply_is_a_dry_run_too(self):
        self.run_main("--apply")
        before = len(self.writes())
        result = self.run_main("--rollback")
        self.assertEqual(result.code, 0, result.everything)
        self.assertEqual(len(self.writes()), before)
        self.assertIn("DELETE  DNS record", result.out)

    def test_apply_needs_the_hostname_typed(self):
        result = self.run_main("--apply", typed="yes\n")
        self.assertEqual(result.code, route.EXIT_REFUSED)
        self.assertEqual(self.writes(), [])


class ApplyTests(RouteScriptCase):
    def test_apply_makes_everything_in_the_safe_order(self):
        result = self.run_main("--apply")
        self.assertEqual(result.code, 0, result.everything)
        self.assert_complete()
        account = f"/accounts/{ACCOUNT}"
        self.assertEqual(self.writes(), [
            ("POST", f"{account}/access/service_tokens"),
            ("POST", f"{account}/access/policies"),
            ("POST", f"{account}/access/apps"),
            ("PUT", CONFIG_PATH),
            ("POST", f"/zones/{ZONE}/dns_records"),
            ("PUT", f"{account}/workers/scripts/gate-mate/secrets"),
            ("PUT", f"{account}/workers/scripts/gate-mate/secrets"),
            ("PUT", f"{account}/workers/scripts/gate-mate/secrets"),
        ])
        self.assertEqual(self.sleeps, [10])  # the settle before the probe, and no retries

    def test_the_ingress_put_waits_for_the_application_to_exist_and_be_read_back(self):
        self.run_main("--apply")
        created = self.index_of("POST", r"/access/apps$")
        verified = self.index_of("GET", r"/access/apps/[0-9a-f]{32}$")
        ingress = self.index_of("PUT", r"/configurations$")
        record = self.index_of("POST", r"/dns_records$")
        bare_probe = self.fake.log.index(("PROBE", "bare"))
        token_probe = self.fake.log.index(("PROBE", "with-token"))
        first_secret = self.index_of("PUT", r"/secrets$")
        self.assertLess(created, verified)
        self.assertLess(verified, ingress)
        self.assertLess(ingress, record)
        self.assertLess(record, bare_probe)
        self.assertLess(bare_probe, token_probe)
        self.assertLess(token_probe, first_secret)

    def test_the_url_secret_goes_in_last_and_other_secrets_are_untouched(self):
        self.run_main("--apply")
        names = [json.loads(body)["name"] for body in self.fake.bodies
                 if '"secret_text"' in body]
        self.assertEqual(names, ["PI_CAMERA_ACCESS_CLIENT_ID", "PI_CAMERA_ACCESS_CLIENT_SECRET",
                                 "PI_CAMERA_URL"])
        self.assertEqual(self.fake.secrets["SESSION_KEY"], "kept")
        self.assertEqual(self.fake.secrets["PI_COMMAND_ACCESS_CLIENT_SECRET"], "kept")

    def test_existing_ingress_rules_are_preserved_exactly_and_the_catch_all_stays_last(self):
        self.run_main("--apply")
        sent = next(json.loads(body) for body in self.fake.bodies if '"ingress"' in body)
        ingress = sent["config"]["ingress"]
        self.assertEqual(ingress[:2], ORIGINAL_INGRESS[:2])
        self.assertEqual(json.dumps(ingress[0]), json.dumps(ORIGINAL_INGRESS[0]))  # key order too
        self.assertEqual(json.dumps(ingress[1]), json.dumps(ORIGINAL_INGRESS[1]))
        self.assertEqual(ingress[2], {"hostname": HOSTNAME, "service": "http://127.0.0.1:8767"})
        self.assertEqual(ingress[-1], {"service": "http_status:404"})
        self.assertEqual(len(ingress), 4)
        self.assertEqual(sent["config"]["warp-routing"], {"enabled": False})

    def test_the_application_has_one_service_auth_policy_for_its_own_token_only(self):
        self.run_main("--apply")
        application, token = self.fake.apps[0], self.fake.tokens[0]
        self.assertEqual(application["type"], "self_hosted")
        self.assertEqual(application["domain"], HOSTNAME)
        self.assertIs(application["app_launcher_visible"], False)
        self.assertEqual(len(application["policies"]), 1)
        self.assertEqual(application["policies"][0]["decision"], "non_identity")
        self.assertEqual(application["policies"][0]["include"],
                         [{"service_token": {"token_id": token["id"]}}])

    def test_a_second_apply_changes_nothing(self):
        self.run_main("--apply")
        before = self.writes()
        result = self.run_main("--apply")
        self.assertEqual(result.code, 0, result.everything)
        self.assertEqual(self.writes(), before)
        self.assert_complete()

    def test_a_config_changed_between_read_and_write_is_refused(self):
        self.fake.mutate_config_on_get = 2  # the survey reads it once; step d reads it again
        result = self.run_main("--apply")
        self.assertEqual(result.code, route.EXIT_REFUSED, result.everything)
        self.assertIn("changed between reading it and writing it", result.err)
        self.assertNotIn(("PUT", CONFIG_PATH), self.fake.log)
        self.assertFalse([r for r in self.fake.dns if r["name"] == HOSTNAME])
        self.assertEqual(self.fake.config["ingress"][0]["hostname"], "someone-else.shopshield.app")

    def test_a_locally_managed_tunnel_is_refused_before_anything_is_made(self):
        self.fake.config_src = "local"
        result = self.run_main("--apply")
        self.assertEqual(result.code, route.EXIT_REFUSED)
        self.assertIn("locally managed", result.err)
        self.assertEqual(self.writes(), [])

    def test_a_port_another_hostname_already_publishes_is_refused(self):
        result = self.run_main("--apply", "--origin-port", "8765", "--allow-undocumented-target")
        self.assertEqual(result.code, route.EXIT_REFUSED)
        self.assertIn("refusing to publish it twice", result.err)
        self.assertEqual(self.writes(), [])


class ProbeTests(RouteScriptCase):
    def test_an_unprotected_200_removes_the_dns_record_and_the_ingress_rule_at_once(self):
        self.fake.access_enforced = False
        result = self.run_main("--apply")
        self.assertEqual(result.code, route.EXIT_EXPOSED, result.everything)
        self.assertIn("UNPROTECTED", result.err)
        self.assertFalse([r for r in self.fake.dns if r["name"] == HOSTNAME])
        self.assertEqual(self.fake.config["ingress"], ORIGINAL_INGRESS)
        after_probe = self.fake.log[self.fake.log.index(("PROBE", "bare")) + 1:]
        removals = [(m, p) for m, p in after_probe if m in ("DELETE", "PUT", "POST")]
        self.assertEqual([m for m, _ in removals], ["DELETE", "PUT"])  # DNS first, then the rule
        self.assertIn("/dns_records/", removals[0][1])
        self.assertNotIn("PI_CAMERA_URL", self.fake.secrets)
        self.assertNotIn(("PROBE", "with-token"), self.fake.log)

    def test_a_tunnel_404_means_access_is_not_in_front_and_the_route_is_removed(self):
        self.fake.access_enforced = False
        real_probe = self.fake.probe
        self.fake.probe = lambda headers: 404
        result = self.run_main("--apply")
        self.fake.probe = real_probe
        self.assertEqual(result.code, route.EXIT_EXPOSED)
        self.assertIn("has not applied", result.err)
        self.assertEqual(self.fake.config["ingress"], ORIGINAL_INGRESS)
        self.assertFalse([r for r in self.fake.dns if r["name"] == HOSTNAME])

    def test_an_origin_that_is_down_still_stores_the_secret_and_says_so(self):
        self.fake.origin_status = 502
        result = self.run_main("--apply")
        self.assertEqual(result.code, route.EXIT_UNVERIFIED, result.everything)
        self.assertIn("with the service token: 502", result.err)
        self.assert_complete()

    def test_what_counts_as_an_access_refusal(self):
        login = "https://team.cloudflareaccess.com/cdn-cgi/access/login/gate-camera.shopshield.app"
        self.assertEqual(route.classify_unauthenticated(403, None), "protected")
        self.assertEqual(route.classify_unauthenticated(302, login), "protected")
        self.assertEqual(route.classify_unauthenticated(302, "https://example.com/"), "open")
        self.assertEqual(route.classify_unauthenticated(302, None), "open")
        self.assertEqual(route.classify_unauthenticated(200, None), "open")
        self.assertEqual(route.classify_unauthenticated(404, None), "not_applied")
        self.assertEqual(route.classify_unauthenticated(530, None), "unexpected")

    def test_a_resolver_that_does_not_know_the_name_yet_borrows_the_anchor_address(self):
        def resolve(name, port, type=None):
            if name == HOSTNAME:
                raise OSError("no such name")
            return [(2, 1, 6, "", ("198.51.100.7", 443))]
        self.assertEqual(route.edge_addresses(HOSTNAME, ANCHOR, resolve),
                         (["198.51.100.7"], True))


# Every write the script makes, in order: a failure at each must be resumable.
WRITE_STEPS = [
    ("POST", r"/access/service_tokens$"),
    ("POST", r"/access/policies$"),
    ("POST", r"/access/apps$"),
    ("PUT", r"/configurations$"),
    ("POST", r"/dns_records$"),
    ("PUT", r"/secrets$"),
]


class ResumeTests(RouteScriptCase):
    def test_a_rerun_after_a_failure_at_each_step_resumes_without_duplicates(self):
        for method, pattern in WRITE_STEPS:
            with self.subTest(step=f"{method} {pattern}"):
                self.setUp()
                self.fake.fail = [[method, pattern, 1]]
                failed = self.run_main("--apply")
                self.assertEqual(failed.code, route.EXIT_ERROR, failed.everything)
                # Whatever failed, the service is not reachable unprotected.
                self.assertIn(self.fake.probe({}), (403, 404))
                if self.fake.tokens:
                    # The secret died with the failed run; this must be said, not papered over.
                    refused = self.run_main("--apply")
                    self.assertEqual(refused.code, route.EXIT_REFUSED, refused.everything)
                    self.assertIn("--rotate-service-token", refused.err)
                    self.assertFalse(self.fake.tokens[0].get("rotations"))
                    again = self.run_main("--apply", "--rotate-service-token")
                else:
                    again = self.run_main("--apply")
                self.assertEqual(again.code, 0, again.everything)
                self.assert_complete()
                self.doCleanups()

    def test_a_failure_after_the_ingress_rule_takes_the_rule_out_again(self):
        self.fake.fail = [["POST", r"/dns_records$", 1]]
        result = self.run_main("--apply")
        self.assertEqual(result.code, route.EXIT_ERROR)
        self.assertEqual(self.fake.config["ingress"], ORIGINAL_INGRESS)

    def test_an_existing_token_with_an_unknown_secret_is_never_rotated_silently(self):
        self.fake.fail = [["POST", r"/access/policies$", 1]]
        self.run_main("--apply")
        first = self.fake.tokens[0]["client_secret"]
        dry = self.run_main()
        self.assertIn("--rotate-service-token", dry.out)
        refused = self.run_main("--apply")
        self.assertEqual(refused.code, route.EXIT_REFUSED)
        self.assertEqual(self.fake.tokens[0]["client_secret"], first)
        self.assertNotIn(("POST", f"/accounts/{ACCOUNT}/access/service_tokens/"
                          + self.fake.tokens[0]["id"] + "/rotate"), self.fake.log)

    def test_a_route_with_no_application_in_front_of_it_is_refused_loudly(self):
        self.run_main("--apply")
        self.fake.apps.clear()
        result = self.run_main("--apply")
        self.assertEqual(result.code, route.EXIT_REFUSED)
        self.assertIn("--rollback --apply", result.err)


class RollbackTests(RouteScriptCase):
    def test_rollback_removes_only_its_own_objects_in_the_reverse_order(self):
        self.run_main("--apply")
        self.fake.tokens.append({"id": "9" * 32, "name": "gate-mate-worker", "client_id": "x",
                                 "client_secret": "y"})
        self.fake.apps.append({"id": "8" * 32, "domain": ANCHOR, "type": "self_hosted",
                               "policies": []})
        self.fake.policies.append({"id": "7" * 32, "name": "gate-command service auth",
                                   "decision": "non_identity", "include": []})
        start = len(self.fake.log)
        result = self.run_main("--rollback", "--apply")
        self.assertEqual(result.code, 0, result.everything)
        account = f"/accounts/{ACCOUNT}"
        done = [(m, re.sub(r"[0-9a-f]{32}$", "<id>", p)) for m, p in self.fake.log[start:]
                if m in ("POST", "PUT", "DELETE")]
        self.assertEqual(done, [
            ("DELETE", f"/zones/{ZONE}/dns_records/<id>"),
            ("PUT", CONFIG_PATH),
            ("DELETE", f"{account}/workers/scripts/gate-mate/secrets/PI_CAMERA_URL"),
            ("DELETE", f"{account}/workers/scripts/gate-mate/secrets/PI_CAMERA_ACCESS_CLIENT_SECRET"),
            ("DELETE", f"{account}/workers/scripts/gate-mate/secrets/PI_CAMERA_ACCESS_CLIENT_ID"),
            ("DELETE", f"{account}/access/apps/<id>"),
            ("DELETE", f"{account}/access/policies/<id>"),
            ("DELETE", f"{account}/access/service_tokens/<id>"),
        ])
        self.assertEqual(self.fake.config["ingress"], ORIGINAL_INGRESS)
        self.assertEqual([r["name"] for r in self.fake.dns], [ANCHOR, "shopshield.app"])
        self.assertEqual(self.fake.secrets, {"PI_COMMAND_ACCESS_CLIENT_SECRET": "kept",
                                             "SESSION_KEY": "kept"})
        self.assertEqual([t["name"] for t in self.fake.tokens], ["gate-mate-worker"])
        self.assertEqual([a["domain"] for a in self.fake.apps], [ANCHOR])
        self.assertEqual([p["name"] for p in self.fake.policies], ["gate-command service auth"])

    def test_rollback_of_nothing_does_nothing(self):
        result = self.run_main("--rollback", "--apply")
        self.assertEqual(result.code, 0, result.everything)
        self.assertEqual(self.writes(), [])


class GuardTests(RouteScriptCase):
    def test_it_refuses_to_run_under_ci(self):
        for marker in ("CI", "GITHUB_ACTIONS"):
            result = self.run_main("--apply", env=self.env(**{marker: "true"}))
            self.assertEqual(result.code, route.EXIT_REFUSED)
            self.assertIn("automated", result.err)
        self.assertEqual(self.fake.log, [])

    def test_it_refuses_another_hostname_port_or_worker_without_the_override(self):
        for flags in (["--hostname", "gate-command.shopshield.app"], ["--origin-port", "8765"],
                      ["--worker", "other"], ["--zone", "example.com"]):
            result = self.run_main("--apply", *flags)
            self.assertEqual(result.code, route.EXIT_REFUSED, flags)
            self.assertIn("--allow-undocumented-target", result.err)
        self.assertEqual(self.fake.log, [])

    def test_the_base_url_override_must_be_loopback_and_come_as_a_pair(self):
        for api, probe in (("http://192.0.2.10:8080/client/v4", self.base),
                           ("http://localhost:8080/client/v4", self.base),
                           ("https://evil.example/client/v4", self.base),
                           (self.base + "/client/v4", "http://192.0.2.10"),
                           (self.base + "/client/v4", "")):
            env = self.env(**{route.TEST_API_VARIABLE: api, route.TEST_PROBE_VARIABLE: probe})
            result = self.run_main("--apply", env=env)
            self.assertEqual(result.code, route.EXIT_REFUSED, (api, probe))
        self.assertEqual(self.fake.log, [])

    def test_it_refuses_without_a_token_and_with_an_unresolved_reference(self):
        for value in ("", "short", "op://Private/Cloudflare/credential"):
            result = self.run_main(env=self.env(**{route.TOKEN_VARIABLE: value}))
            self.assertEqual(result.code, route.EXIT_REFUSED, value)
        self.assertEqual(self.fake.log, [])

    def test_there_is_no_way_to_pass_the_token_as_an_argument(self):
        options = {option for action in route.build_parser()._actions
                   for option in action.option_strings}
        self.assertFalse([o for o in options if "token" in o and o != "--rotate-service-token"
                          and o != "--token-duration"])
        self.assertFalse([o for o in options if "secret" in o or "key" in o])


class NothingLeaksTests(RouteScriptCase):
    """The API token and the client secret appear nowhere: not in any stream, not in a file."""

    def marks(self):
        return [API_MARK] + ACCESS_MARKS

    def assert_clean(self, text, where):
        for mark in self.marks():
            self.assertNotIn(mark, text, where)
            self.assertNotIn(mark[:16], text, where)  # nor a piece of one

        self.assertNotIn("CF-Access-Client-Secret", text, where)

    def captured(self, *argv, **kwargs):
        """main(), with the process's own streams and the root logger captured as well."""
        stream_out, stream_err, logged = io.StringIO(), io.StringIO(), io.StringIO()
        handler = logging.StreamHandler(logged)
        root = logging.getLogger()
        level = root.level
        root.addHandler(handler)
        root.setLevel(logging.DEBUG)
        try:
            with contextlib.redirect_stdout(stream_out), contextlib.redirect_stderr(stream_err):
                result = self.run_main(*argv, **kwargs)
        finally:
            root.removeHandler(handler)
            root.setLevel(level)
        return result, result.everything + stream_out.getvalue() + stream_err.getvalue() \
            + logged.getvalue()

    def test_no_stream_and_no_file_ever_carries_the_token_or_the_secret(self):
        scenarios = [
            ("dry run", [], lambda fake: None),
            ("apply", ["--apply"], lambda fake: None),
            ("apply again", ["--apply"], lambda fake: None),
            ("rotate", ["--apply", "--rotate-service-token", "--show-client-id"],
             lambda fake: None),
            ("rollback", ["--rollback", "--apply"], lambda fake: None),
            ("unprotected", ["--apply"], lambda fake: setattr(fake, "access_enforced", False)),
        ]
        scratch = tempfile.mkdtemp()
        here = os.getcwd()
        os.chdir(scratch)
        try:
            for name, argv, prepare in scenarios:
                prepare(self.fake)
                result, text = self.captured(*argv)
                self.assertTrue(text, name)
                self.assert_clean(text, name)
            # The secret really was in play, so its absence above means something.
            self.assertTrue(any(mark in body for body in self.fake.bodies
                                for mark in ACCESS_MARKS))
            self.assertEqual(os.listdir(scratch), [])
        finally:
            os.chdir(here)
            os.rmdir(scratch)

    def test_a_failure_that_quotes_the_credentials_back_is_redacted(self):
        self.fake.fail_echo = True
        for method, pattern in WRITE_STEPS:
            with self.subTest(step=f"{method} {pattern}"):
                self.setUp()
                self.fake.fail_echo = True
                self.fake.fail = [[method, pattern, 1]]
                result, text = self.captured("--apply")
                self.assertEqual(result.code, route.EXIT_ERROR, text)
                self.assertIn("HTTP 500", text)
                self.assert_clean(text, pattern)
                self.doCleanups()

    def test_a_dropped_connection_and_a_rejected_token_are_reported_without_either(self):
        self.fake.drop = [("PUT", r"/secrets$")]
        result, text = self.captured("--apply")
        self.assertEqual(result.code, route.EXIT_ERROR, text)
        self.assert_clean(text, "dropped connection")
        wrong = "wrongApiMarkNotReal0123456789abcdefghijklmnopq"
        result, text = self.captured("--apply", env=self.env(**{route.TOKEN_VARIABLE: wrong}))
        self.assertEqual(result.code, route.EXIT_ERROR, text)
        self.assertNotIn(wrong, text)

    def test_an_unexpected_exception_inside_the_script_is_reported_without_either(self):
        original = route.print_checklist

        def explode(settings, console):
            secret = self.fake.secrets["PI_CAMERA_ACCESS_CLIENT_SECRET"]
            raise RuntimeError(f"boom {API_MARK} {secret} Authorization: Bearer {API_MARK}")
        route.print_checklist = explode
        try:
            result, text = self.captured("--apply")
        finally:
            route.print_checklist = original
        self.assertEqual(result.code, route.EXIT_ERROR)
        self.assertIn("boom", text)
        for mark in self.marks():
            self.assertNotIn(mark, text)

    def test_the_client_id_is_hidden_unless_asked_for(self):
        hidden = self.run_main("--apply")
        self.assertNotIn(CLIENT_ID, hidden.everything)
        shown = self.run_main("--apply", "--show-client-id")
        self.assertIn(CLIENT_ID, shown.out)

    def test_a_token_put_on_the_command_line_by_mistake_is_not_echoed(self):
        result = self.as_a_process(["--apply", "--token", API_MARK], typed="")
        self.assertEqual(result.returncode, route.EXIT_REFUSED)
        self.assertNotIn(API_MARK, result.stdout + result.stderr)

    def as_a_process(self, argv, typed=HOSTNAME + "\n", cwd=None):
        env = {key: value for key, value in os.environ.items()
               if key not in route.CI_MARKERS and key != route.TOKEN_VARIABLE}
        env.update(self.env())
        return subprocess.run([sys.executable, str(SCRIPT)] + argv, input=typed, env=env,
                              capture_output=True, text=True, timeout=120, cwd=cwd)

    def test_the_real_process_leaks_nothing_on_success_or_on_failure(self):
        with tempfile.TemporaryDirectory() as scratch:
            self.fake.fail_echo = True
            self.fake.fail = [["PUT", r"/secrets$", 1]]
            failed = self.as_a_process(["--apply", "--settle-seconds", "0"], cwd=scratch)
            self.assertEqual(failed.returncode, route.EXIT_ERROR, failed.stderr)
            self.assertNotIn("Traceback", failed.stderr)
            passed = self.as_a_process(
                ["--apply", "--rotate-service-token", "--settle-seconds", "0"], cwd=scratch)
            self.assertEqual(passed.returncode, 0, passed.stderr)
            self.assert_clean(failed.stdout + failed.stderr + passed.stdout + passed.stderr,
                              "subprocess")
            self.assertEqual(os.listdir(scratch), [])
        self.assert_complete()


if __name__ == "__main__":
    unittest.main()
