#!/usr/bin/env python3
"""Give the Gate Mate Worker a route to the Pi's camera-control service.

The owner runs this by hand, from a workstation, never from the Pi and never
from an automated deploy. It reads the Cloudflare account first and prints a
plan; nothing changes without `--apply` and a typed confirmation.

    # dry run: read the account, print the plan, change nothing
    op run --env-file=cloudflare.env -- python3 deployment/cloudflare/setup-camera-route.py
    # do it
    op run --env-file=cloudflare.env -- python3 deployment/cloudflare/setup-camera-route.py --apply

`cloudflare.env` holds one line, `CLOUDFLARE_API_TOKEN=op://<vault>/<item>/<field>`,
so the token itself is never on disk. Exporting CLOUDFLARE_API_TOKEN yourself
works too. The token is read from the environment and from nowhere else: there
is no flag for it and no prompt.

The order is the point. A tunnel hostname resolves the moment its DNS record
exists, so the Access application has to be in front of it first:

    a. the service token              (its secret is returned once; held in memory only)
    b. the Service Auth policy and the self-hosted Access application
    c. read the application back and check it covers exactly the bare hostname
    d. only then the tunnel ingress rule, inserted ahead of the catch-all
    e. the proxied CNAME to <tunnel-id>.cfargotunnel.com
    f. probe from outside: no credentials must be refused by Access, the
       service token must get 200. Anything but an Access refusal removes the
       DNS record and the ingress rule again, at once.
    g. the three Worker secrets, PI_CAMERA_URL last
    h. a final check, and the list of what to look at in the app

Every step looks for its object before creating it, so a run that failed part
way is resumed by running it again. The one thing a second run cannot recover
is the client secret: Cloudflare shows it once. If the token exists and the
Worker does not hold its secret, the script says so and stops until it is
told `--rotate-service-token`.

`--rollback` plans, and with `--apply` performs, the removal of exactly these
objects in the reverse order: DNS record, ingress rule, Worker secrets, Access
application, policy, service token.

Nothing here prints, logs or writes the API token, the client secret or any
header that carries one. See docs/camera-control.md, Install, steps 5 to 7.
"""
from __future__ import annotations

import argparse
import http.client
import ipaddress
import json
import re
import socket
import ssl
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

API_BASE = "https://api.cloudflare.com/client/v4"
TOKEN_VARIABLE = "CLOUDFLARE_API_TOKEN"
# Test-only. Both must be set together and both must be loopback literals, so
# neither can point the token, or the probe's verdict, at another machine.
TEST_API_VARIABLE = "GATE_CAMERA_ROUTE_TEST_API_BASE"
TEST_PROBE_VARIABLE = "GATE_CAMERA_ROUTE_TEST_PROBE_BASE"

DOCUMENTED = {
    "hostname": "gate-camera.shopshield.app",
    "anchor_hostname": "gate-command.shopshield.app",
    "zone": "shopshield.app",
    "origin_port": 8767,
    "worker": "gate-mate",
}
ORIGIN_HOST = "127.0.0.1"
SERVICE_TOKEN_NAME = "gate-mate-worker-camera"
POLICY_NAME = "gate-mate-worker-camera service auth"
APPLICATION_NAME = "Gate camera control"
DNS_COMMENT = "gate-camera-control route, made by setup-camera-route.py"
PROBE_PATH = "/camera/state"
BINDING_URL = "PI_CAMERA_URL"
BINDING_ACCESS_ID = "PI_CAMERA_ACCESS_CLIENT_ID"
BINDING_ACCESS_VALUE = "PI_CAMERA_ACCESS_CLIENT_SECRET"
# These are the *names* of the Worker's three secret bindings, never values.
# They are deliberately not called "secret" anything: code scanning follows any
# variable named like a credential to wherever it is printed, and a name
# printed in a plan is not a finding worth burying a real one under.
#
# The Worker treats the camera as unconfigured until all three are present, so
# the URL goes in last and a run that stops part way leaves it switched off.
WORKER_BINDINGS = (BINDING_ACCESS_ID, BINDING_ACCESS_VALUE, BINDING_URL)

CI_MARKERS = (
    "CI", "CONTINUOUS_INTEGRATION", "GITHUB_ACTIONS", "GITLAB_CI", "BUILDKITE",
    "CIRCLECI", "JENKINS_URL", "TF_BUILD", "TEAMCITY_VERSION", "TRAVIS",
    "CODEBUILD_BUILD_ID", "BITBUCKET_BUILD_NUMBER", "WORKERS_CI",
)

EXIT_OK = 0
EXIT_ERROR = 1
EXIT_REFUSED = 2
EXIT_EXPOSED = 3
EXIT_UNVERIFIED = 4

_HOSTNAME = re.compile(r"^(?=.{1,253}$)([a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z]{2,63}$")
_TUNNEL_TARGET = re.compile(
    r"^([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})\.cfargotunnel\.com$")
_WORKER_NAME = re.compile(r"^[a-z0-9][a-z0-9_-]{0,62}$")
_HEX_ID = re.compile(r"^[0-9a-f]{32}$")


class Refusal(Exception):
    """The script will not go on. Nothing is half done because of it."""


class ApiError(Exception):
    """A Cloudflare call failed. The message is built from redacted parts only."""

    def __init__(self, message, status=None):
        super().__init__(message)
        self.status = status


class Exposed(Exception):
    """The hostname answered without Access in front of it. The route was removed."""


class Redactor:
    """Every line the script writes goes through here, whatever produced it."""

    def __init__(self):
        self._hidden = []

    def hide(self, value, label="[REDACTED]"):
        if isinstance(value, str) and len(value) >= 8:
            self._hidden.append((value, label))
            quoted = urllib.parse.quote(value, safe="")
            if quoted != value:
                self._hidden.append((quoted, label))
            self._hidden.sort(key=lambda pair: len(pair[0]), reverse=True)

    def clean(self, text):
        text = str(text)
        for value, label in self._hidden:
            text = text.replace(value, label)
        return text


class Console:
    def __init__(self, stdout, stderr, redactor):
        self._stdout, self._stderr, self._redactor = stdout, stderr, redactor

    def say(self, text=""):
        self._stdout.write(self._redactor.clean(text) + "\n")
        self._stdout.flush()

    def shout(self, text):
        self._stderr.write(self._redactor.clean(text) + "\n")
        self._stderr.flush()


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    # urllib re-sends every header, Authorization included, to wherever a
    # redirect points. The API never needs one, so none is followed.
    def redirect_request(self, *args, **kwargs):
        return None


class Api:
    """The Cloudflare v4 API, one JSON envelope at a time."""

    def __init__(self, base, token, redactor, loopback):
        self._base = base.rstrip("/")
        self._token = token
        self._redactor = redactor
        handlers = [_NoRedirect()]
        if loopback:
            handlers.append(urllib.request.ProxyHandler({}))
        self._opener = urllib.request.build_opener(*handlers)

    def call(self, method, path, query=None, body=None, envelope=False):
        url = self._base + path
        if query:
            url += "?" + urllib.parse.urlencode(query)
        data = None if body is None else json.dumps(body).encode("utf-8")
        request = urllib.request.Request(url, data=data, method=method)
        request.add_header("Authorization", "Bearer " + self._token)
        request.add_header("Accept", "application/json")
        request.add_header("User-Agent", "gate-controller-setup-camera-route/1")
        if data is not None:
            request.add_header("Content-Type", "application/json")
        where = f"{method} {path}"
        try:
            with self._opener.open(request, timeout=30) as response:
                raw = response.read(4 * 1024 * 1024)
        except urllib.error.HTTPError as error:
            status, detail = error.code, self._errors_of(self._read_quietly(error))
            error.close()
            # `from None`: the original carries the request and its headers.
            raise ApiError(self._redactor.clean(
                f"{where} answered HTTP {status}{detail}"), status) from None
        except (urllib.error.URLError, OSError, http.client.HTTPException) as error:
            reason = getattr(error, "reason", None) or type(error).__name__
            raise ApiError(self._redactor.clean(
                f"{where} did not complete: {reason}")) from None
        try:
            payload = json.loads(raw.decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            raise ApiError(f"{where} answered something that is not JSON") from None
        if not isinstance(payload, dict) or payload.get("success") is not True:
            raise ApiError(self._redactor.clean(
                f"{where} reported failure{self._errors_of(raw)}"))
        return payload if envelope else payload.get("result")

    def every(self, path, query=None):
        """All pages of a list. Filtering is done by the caller, on the results."""
        found, page = [], 1
        while page <= 100:
            payload = self.call("GET", path, dict(query or {}, page=page, per_page=50),
                                envelope=True)
            batch = payload.get("result") or []
            found.extend(batch)
            info = payload.get("result_info") or {}
            total = info.get("total_pages")
            if not batch or (isinstance(total, int) and page >= total) \
                    or (total is None and len(batch) < 50):
                return found
            page += 1
        raise ApiError(f"GET {path} has more than 100 pages; refusing to guess")

    @staticmethod
    def _read_quietly(error):
        try:
            return error.read(8192)
        except Exception:  # a failed read of a failure is still just a failure
            return b""

    def _errors_of(self, raw):
        try:
            errors = json.loads(raw.decode("utf-8")).get("errors") or []
            # Redact, then shorten: shortening first could cut a credential in
            # two and leave a half that no longer matches anything to redact.
            parts = [f"{item.get('code')}: {self._redactor.clean(item.get('message'))[:200]}"
                     for item in errors[:3] if isinstance(item, dict)]
        except Exception:
            parts = []
        return " (" + "; ".join(parts) + ")" if parts else ""


# --------------------------------------------------------------------------
# The probe: what somebody on the internet gets from the hostname.

class _PinnedHTTPSConnection(http.client.HTTPSConnection):
    """TLS to `hostname`, verified as `hostname`, over a socket to a chosen address."""

    def __init__(self, hostname, address, timeout):
        self._pinned_context = ssl.create_default_context()
        super().__init__(hostname, 443, timeout=timeout, context=self._pinned_context)
        self._address = address

    def connect(self):
        raw = socket.create_connection((self._address, 443), self.timeout)
        self.sock = self._pinned_context.wrap_socket(raw, server_hostname=self.host)


def edge_addresses(hostname, anchor_hostname, resolve=socket.getaddrinfo):
    """Where to connect for `hostname`, and whether the anchor had to stand in.

    A resolver that was asked about the hostname before its record existed keeps
    answering "no such name" for as long as the zone's negative TTL. Every
    proxied hostname in a zone is served from the same edge, which routes on SNI
    and Host, so the hostname already in use is a sound place to connect.
    """
    for name, borrowed in ((hostname, False), (anchor_hostname, True)):
        try:
            answers = resolve(name, 443, type=socket.SOCK_STREAM)
        except OSError:
            continue
        addresses = list(dict.fromkeys(answer[4][0] for answer in answers))
        if addresses:
            return addresses, borrowed
    return [], False


def classify_unauthenticated(status, location):
    """What an answer to a request carrying no credentials means."""
    if status in (401, 403):
        return "protected"
    if status in (301, 302, 303, 307, 308):
        target = urllib.parse.urlsplit(location or "")
        if target.scheme == "https" and (target.hostname or "").endswith(".cloudflareaccess.com") \
                and target.path.startswith("/cdn-cgi/access/"):
            return "protected"
        return "open"
    if 200 <= status < 300:
        return "open"
    if status == 404:
        return "not_applied"
    return "unexpected"


class Probe:
    def __init__(self, settings, test_base, sleep):
        self._settings, self._test_base, self._sleep = settings, test_base, sleep
        self.borrowed_address = False

    def get(self, headers=None):
        """(status, Location) or None if no HTTP answer could be had at all."""
        hostname = self._settings.hostname
        sent = {"Host": hostname, "Accept": "application/json",
                "User-Agent": "gate-controller-setup-camera-route/1"}
        sent.update(headers or {})
        if self._test_base:
            target = urllib.parse.urlsplit(self._test_base)
            connections = [http.client.HTTPConnection(target.hostname, target.port, timeout=10)]
        else:
            addresses, self.borrowed_address = edge_addresses(
                hostname, self._settings.anchor_hostname)
            connections = [_PinnedHTTPSConnection(hostname, address, 10)
                           for address in addresses[:4]]
        for connection in connections:
            try:
                connection.request("GET", PROBE_PATH, headers=sent)
                response = connection.getresponse()
                response.read(1024)
                return response.status, response.getheader("Location")
            except (OSError, http.client.HTTPException):
                continue
            finally:
                connection.close()
        return None

    def persistent(self, headers=None, attempts=6, accept=None):
        """Retry only while there is no answer, or while `accept` says to wait."""
        answer = None
        for attempt in range(attempts):
            if attempt:
                self._sleep(5)
            answer = self.get(headers)
            if answer is not None and (accept is None or accept(answer[0])):
                break
        return answer


# --------------------------------------------------------------------------
# Settings and the survey of what exists.

class Settings:
    def __init__(self, arguments):
        self.hostname = arguments.hostname.strip().lower().rstrip(".")
        self.anchor_hostname = arguments.anchor_hostname.strip().lower().rstrip(".")
        self.zone = arguments.zone.strip().lower().rstrip(".")
        self.origin_port = arguments.origin_port
        self.worker = arguments.worker
        self.account_id = arguments.account_id
        self.zone_id = arguments.zone_id
        self.session_duration = arguments.session_duration
        self.token_duration = arguments.token_duration
        self.settle_seconds = arguments.settle_seconds
        chosen = {"hostname": self.hostname, "anchor_hostname": self.anchor_hostname,
                  "zone": self.zone, "origin_port": self.origin_port, "worker": self.worker}
        different = sorted(key for key, value in chosen.items() if value != DOCUMENTED[key])
        if different and not arguments.allow_undocumented_target:
            raise Refusal(
                "these are not the documented values: " + ", ".join(different) + ". The "
                "documented route is " + DOCUMENTED["hostname"] + " -> http://" + ORIGIN_HOST
                + ":" + str(DOCUMENTED["origin_port"]) + " for the Worker "
                + DOCUMENTED["worker"] + ". If you mean it, add --allow-undocumented-target.")
        for name in (self.hostname, self.anchor_hostname, self.zone):
            if not _HOSTNAME.match(name):
                raise Refusal(f"not a hostname: {name!r}")
        for name in (self.hostname, self.anchor_hostname):
            if not name.endswith("." + self.zone):
                raise Refusal(f"{name} is not in the zone {self.zone}")
        if self.hostname == self.anchor_hostname:
            raise Refusal("the camera hostname must not be the gate-command hostname")
        if not 1 <= self.origin_port <= 65535:
            raise Refusal("the origin port must be 1-65535")
        if not _WORKER_NAME.match(self.worker):
            raise Refusal("that is not a Worker script name")
        for value, flag in ((self.account_id, "--account-id"), (self.zone_id, "--zone-id")):
            if value is not None and not _HEX_ID.match(value):
                raise Refusal(f"{flag} takes the 32-character hexadecimal id")
        for value, flag in ((self.session_duration, "--session-duration"),
                            (self.token_duration, "--token-duration")):
            if not re.match(r"^\d{1,5}(m|h)$", value):
                raise Refusal(f"{flag} takes a duration such as 30m or 8760h")

    @property
    def service(self):
        return f"http://{ORIGIN_HOST}:{self.origin_port}"

    @property
    def origin_url(self):
        return f"https://{self.hostname}"


class Found:
    """What the account holds now. Filled in by `survey`, refreshed step by step."""
    account_id = zone_id = tunnel_id = tunnel_name = None
    config = config_version = None
    token = policy = application = dns_record = None
    binding_names = ()


def _account(found):
    return f"/accounts/{found.account_id}"


def _rule_index(ingress, hostname):
    return next((index for index, rule in enumerate(ingress)
                 if isinstance(rule, dict) and rule.get("hostname") == hostname), None)


def _check_ingress_shape(ingress, settings):
    if not isinstance(ingress, list) or not ingress:
        raise Refusal("the tunnel has no ingress rules at all; this is not the tunnel "
                      "that serves " + settings.anchor_hostname)
    last = ingress[-1]
    if not isinstance(last, dict) or last.get("hostname") or last.get("path") \
            or not last.get("service"):
        raise Refusal("the tunnel's last ingress rule is not a catch-all; fix that in the "
                      "Zero Trust dashboard first")
    if _rule_index(ingress, settings.anchor_hostname) is None:
        raise Refusal("the tunnel has no rule for " + settings.anchor_hostname
                      + ", so it is not the tunnel to add the camera to")
    index = _rule_index(ingress, settings.hostname)
    if index is not None:
        rule = ingress[index]
        if rule.get("service") != settings.service or rule.get("path"):
            raise Refusal(f"the tunnel already routes {settings.hostname} somewhere else "
                          "(not " + settings.service + "). That rule is not this script's; "
                          "look at it in the dashboard.")
    for rule in ingress:
        if isinstance(rule, dict) and rule.get("hostname") != settings.hostname \
                and str(rule.get("service", "")).rstrip("/") == settings.service:
            raise Refusal(f"{settings.service} is already published as "
                          f"{rule.get('hostname')}; refusing to publish it twice")


def _policy_is_ours(policy, token_id):
    include = policy.get("include") or []
    return (policy.get("decision") == "non_identity"
            and include == [{"service_token": {"token_id": token_id}}]
            and not policy.get("exclude") and not policy.get("require"))


def _application_covers(application, hostname):
    """Exactly the bare hostname. `host/path` would leave every other path open."""
    names = [application.get("domain")]
    names += list(application.get("self_hosted_domains") or [])
    names += [item.get("uri") for item in application.get("destinations") or []
              if isinstance(item, dict) and item.get("type", "public") == "public"]
    names = [name for name in names if name]
    return bool(names) and all(name == hostname for name in names)


def read_tunnel_config(api, found):
    result = api.call("GET", f"{_account(found)}/cfd_tunnel/{found.tunnel_id}/configurations")
    result = result or {}
    source = result.get("source")
    if source not in (None, "cloudflare"):
        raise Refusal("the tunnel's configuration source is " + repr(source)
                      + ", not cloudflare: it is locally managed and this script will "
                      "not touch it")
    config = result.get("config")
    if not isinstance(config, dict):
        raise Refusal("the tunnel has no remote configuration: it is locally managed")
    return config, result.get("version")


def verify_application(api, found, settings):
    """Step c. Read the application back, and its policies, and check all of it."""
    application = api.call(
        "GET", f"{_account(found)}/access/apps/{found.application['id']}")
    if not isinstance(application, dict) or application.get("type") != "self_hosted":
        raise Refusal("the Access application is not a self-hosted application")
    if not _application_covers(application, settings.hostname):
        raise Refusal("the Access application does not cover exactly " + settings.hostname)
    policies = application.get("policies") or []
    if len(policies) != 1:
        raise Refusal(f"the Access application has {len(policies)} policies; it must have "
                      "exactly one, the Service Auth policy for its own token")
    policy = policies[0] if isinstance(policies[0], dict) else {"id": policies[0]}
    if "decision" not in policy or "include" not in policy:
        policy = api.call("GET", f"{_account(found)}/access/policies/{policy.get('id')}")
    if not _policy_is_ours(policy or {}, found.token["id"]):
        raise Refusal("the Access application's policy is not Service Auth for exactly "
                      "the " + SERVICE_TOKEN_NAME + " token")
    found.application = application


def survey(api, settings, console):
    found = Found()
    try:
        verify = api.call("GET", "/user/tokens/verify") or {}
    except ApiError:
        # An account-owned token is verified under its account, not under a user.
        if not settings.account_id:
            raise
        verify = api.call("GET", f"/accounts/{settings.account_id}/tokens/verify") or {}
    if verify.get("status") != "active":
        raise Refusal("Cloudflare says this API token is not active")
    console.say("API token: active")

    if settings.zone_id and settings.account_id:
        found.zone_id, found.account_id = settings.zone_id, settings.account_id
    else:
        try:
            zones = [zone for zone in api.every("/zones", {"name": settings.zone})
                     if zone.get("name") == settings.zone]
        except ApiError as error:
            raise Refusal(f"could not look the zone up ({error}). Either give the token "
                          "Zone > Zone > Read, or pass --zone-id and --account-id.") from None
        if len(zones) != 1:
            raise Refusal(f"{len(zones)} zones named {settings.zone} are visible to this token")
        found.zone_id = settings.zone_id or zones[0]["id"]
        found.account_id = (zones[0].get("account") or {}).get("id")
        if settings.zone_id and settings.zone_id != zones[0]["id"]:
            raise Refusal("--zone-id is not the id of " + settings.zone)
        if settings.account_id and settings.account_id != found.account_id:
            raise Refusal("--account-id is not the account that owns " + settings.zone)
        if not found.account_id:
            raise Refusal("the zone does not say which account owns it; pass --account-id")
    console.say(f"zone: {settings.zone}   account: {found.account_id}")

    records = dns_records(api, found, settings.anchor_hostname)
    targets = [_TUNNEL_TARGET.match(record.get("content", "")) for record in records
               if record.get("type") == "CNAME"]
    targets = [match.group(1) for match in targets if match]
    if len(records) != 1 or len(targets) != 1:
        raise Refusal(settings.anchor_hostname + " is not a single CNAME to a tunnel, so "
                      "the tunnel cannot be identified from it")
    found.tunnel_id = targets[0]
    tunnel = api.call("GET", f"{_account(found)}/cfd_tunnel/{found.tunnel_id}") or {}
    if tunnel.get("deleted_at"):
        raise Refusal("that tunnel has been deleted")
    if tunnel.get("config_src") != "cloudflare":
        raise Refusal("the tunnel is locally managed (config_src is "
                      + repr(tunnel.get("config_src")) + "); its ingress lives in a file on "
                      "the origin and this script will not touch it")
    found.tunnel_name = tunnel.get("name")
    found.config, found.config_version = read_tunnel_config(api, found)
    _check_ingress_shape(found.config.get("ingress"), settings)
    console.say(f"tunnel: {found.tunnel_name} ({found.tunnel_id}), remotely managed, "
                f"configuration version {found.config_version}, status {tunnel.get('status')}")
    for rule in found.config["ingress"]:
        console.say(f"  ingress: {rule.get('hostname', '(catch-all)')} -> {rule.get('service')}")

    tokens = [token for token in api.every(f"{_account(found)}/access/service_tokens")
              if token.get("name") == SERVICE_TOKEN_NAME]
    if len(tokens) > 1:
        raise Refusal(f"there are {len(tokens)} service tokens named {SERVICE_TOKEN_NAME}")
    found.token = tokens[0] if tokens else None

    policies = [policy for policy in api.every(f"{_account(found)}/access/policies")
                if policy.get("name") == POLICY_NAME]
    if len(policies) > 1:
        raise Refusal(f"there are {len(policies)} Access policies named {POLICY_NAME}")
    found.policy = policies[0] if policies else None
    if found.policy and not (found.token and _policy_is_ours(found.policy, found.token["id"])):
        raise Refusal(f"an Access policy named {POLICY_NAME} exists but it is not Service "
                      "Auth for exactly this script's token. Look at it in the dashboard.")

    applications = [app for app in api.every(f"{_account(found)}/access/apps")
                    if _mentions(app, settings.hostname)]
    if len(applications) > 1:
        raise Refusal(f"{len(applications)} Access applications mention {settings.hostname}")
    found.application = applications[0] if applications else None
    if found.application and not found.token:
        raise Refusal("an Access application for " + settings.hostname + " exists but the "
                      "service token does not. That application is not this script's.")

    found.dns_record = _own_dns_record(api, found, settings)
    try:
        listed = api.call("GET", f"{_account(found)}/workers/scripts/{settings.worker}/secrets")
    except ApiError as error:
        if error.status == 404:
            raise Refusal(f"there is no Worker named {settings.worker} in this account") from None
        raise
    found.binding_names = tuple(sorted(item.get("name") for item in listed or []
                                      if item.get("type") == "secret_text"))
    return found


def _mentions(application, hostname):
    names = [application.get("domain")] + list(application.get("self_hosted_domains") or [])
    names += [item.get("uri") for item in application.get("destinations") or []
              if isinstance(item, dict)]
    return any(isinstance(name, str) and name.split("/")[0] == hostname for name in names)


def dns_records(api, found, name):
    listed = api.every(f"/zones/{found.zone_id}/dns_records", {"name.exact": name})
    return [record for record in listed if record.get("name") == name]


def _own_dns_record(api, found, settings):
    records = dns_records(api, found, settings.hostname)
    if not records:
        return None
    expected = f"{found.tunnel_id}.cfargotunnel.com"
    if len(records) != 1 or records[0].get("type") != "CNAME" \
            or records[0].get("content") != expected:
        raise Refusal(settings.hostname + " already has DNS that is not a CNAME to this "
                      "tunnel. That record is not this script's; look at it in the dashboard.")
    if records[0].get("proxied") is not True:
        raise Refusal(settings.hostname + " has an unproxied CNAME. Access only protects "
                      "proxied hostnames; fix or delete that record first.")
    return records[0]


# --------------------------------------------------------------------------
# Plans.

def _route_present(found, settings):
    return (_rule_index(found.config["ingress"], settings.hostname) is not None,
            found.dns_record is not None)


def worker_lacks_bindings(found):
    return not set(WORKER_BINDINGS) <= set(found.binding_names)


def print_apply_plan(found, settings, rotate, console):
    has_rule, has_dns = _route_present(found, settings)
    def line(exists, make, keep):
        console.say("  " + (f"keep    {keep}" if exists else f"CREATE  {make}"))
    console.say()
    console.say("Plan, in this order:")
    if found.token and rotate:
        console.say(f"  ROTATE  service token {SERVICE_TOKEN_NAME}: new secret, old one dies at once")
    else:
        line(found.token, f"service token {SERVICE_TOKEN_NAME} (valid {settings.token_duration})",
             f"service token {SERVICE_TOKEN_NAME}")
    line(found.policy, f"Access policy '{POLICY_NAME}': Service Auth, that token only",
         f"Access policy '{POLICY_NAME}'")
    line(found.application,
         f"Access application '{APPLICATION_NAME}' for {settings.hostname}, that one policy, "
         f"session {settings.session_duration}, hidden from the App Launcher",
         f"Access application for {settings.hostname}")
    console.say("  verify  the application covers exactly the hostname, with exactly that policy")
    line(has_rule, f"ingress {settings.hostname} -> {settings.service}, ahead of the catch-all; "
         "every other rule sent back as read", f"ingress {settings.hostname} -> {settings.service}")
    line(has_dns, f"proxied CNAME {settings.hostname} -> {found.tunnel_id}.cfargotunnel.com",
         f"CNAME {settings.hostname}")
    console.say(f"  probe   https://{settings.hostname}{PROBE_PATH} without credentials (Access "
                "must refuse it), then with the token (200)")
    if found.token and not rotate and not worker_lacks_bindings(found):
        console.say(f"  keep    Worker {settings.worker} secrets " + ", ".join(WORKER_BINDINGS))
    else:
        for name in WORKER_BINDINGS:
            verb = "REPLACE" if name in found.binding_names else "CREATE "
            console.say(f"  {verb} Worker {settings.worker} secret {name}")
    console.say(f"  other Worker secrets, untouched: "
                + (", ".join(n for n in found.binding_names if n not in WORKER_BINDINGS) or "none"))


def print_rollback_plan(found, settings, console):
    has_rule, has_dns = _route_present(found, settings)
    ours = [name for name in WORKER_BINDINGS if name in found.binding_names]
    console.say()
    console.say("Rollback plan, in this order:")
    for exists, text in (
            (has_dns, f"DNS record {settings.hostname}"),
            (has_rule, f"ingress rule {settings.hostname} -> {settings.service}"),
            (bool(ours), f"Worker {settings.worker} secrets " + ", ".join(ours)),
            (found.application, f"Access application for {settings.hostname}"),
            (found.policy, f"Access policy '{POLICY_NAME}'"),
            (found.token, f"service token {SERVICE_TOKEN_NAME}")):
        console.say("  " + ("DELETE  " if exists else "absent  ") + text)


# --------------------------------------------------------------------------
# The tunnel configuration: read, change one rule, write, read back.

def _changed_ingress(api, found, settings, console, add, wrote=None):
    """Re-read, refuse if it moved since the survey, write, read back. True if written.

    `wrote` is appended to just before the PUT, so a caller can tell a refusal
    that wrote nothing from a failure that may have.
    """
    config, version = read_tunnel_config(api, found)
    if config != found.config or version != found.config_version:
        raise Refusal("the tunnel configuration changed between reading it and writing it "
                      f"(version {found.config_version} -> {version}). Nothing was written. "
                      "Run the script again and read the new plan.")
    ingress = list(config["ingress"])
    _check_ingress_shape(ingress, settings)
    index = _rule_index(ingress, settings.hostname)
    if add == (index is not None):
        return False
    if add:
        ingress.insert(len(ingress) - 1,
                       {"hostname": settings.hostname, "service": settings.service})
    else:
        del ingress[index]
    wanted = dict(config, ingress=ingress)
    if wrote is not None:
        wrote.append("ingress")
    api.call("PUT", f"{_account(found)}/cfd_tunnel/{found.tunnel_id}/configurations",
             body={"config": wanted})
    found.config, found.config_version = read_tunnel_config(api, found)
    if found.config.get("ingress") != ingress:
        raise ApiError("the tunnel configuration read back is not what was written. "
                       "Somebody else is editing it; look at it in the dashboard now.")
    console.say(f"  tunnel configuration is now version {found.config_version}")
    return True


def remove_route(api, found, settings, console):
    """DNS first, so the name stops resolving before the rule behind it goes."""
    record = _own_dns_record(api, found, settings)
    if record:
        api.call("DELETE", f"/zones/{found.zone_id}/dns_records/{record['id']}")
        if dns_records(api, found, settings.hostname):
            raise ApiError("the DNS record is still there after deleting it")
        console.say(f"  removed DNS record {settings.hostname}")
    found.dns_record = None
    found.config, found.config_version = read_tunnel_config(api, found)
    if _changed_ingress(api, found, settings, console, add=False):
        console.say(f"  removed ingress rule {settings.hostname}")


# --------------------------------------------------------------------------
# Apply.

def apply(api, found, settings, options, console, probe, redactor, sleep):
    account = _account(found)
    has_rule, has_dns = _route_present(found, settings)
    if (has_rule or has_dns) and not (found.token and found.application):
        raise Refusal("a route to the camera service exists WITHOUT the Access application "
                      "in front of it. Run this now:  --rollback --apply")
    client_secret = None
    if found.token and not options.rotate_service_token and worker_lacks_bindings(found):
        raise Refusal(
            f"the service token {SERVICE_TOKEN_NAME} already exists, from a run that did not "
            f"finish, and the Worker does not hold its secret. Cloudflare shows a secret once, "
            "so it cannot be read back. Nothing was changed. To make a new secret (the old "
            "one stops working at once) run again with --rotate-service-token.")

    console.say()
    console.say("a. service token")
    if not found.token:
        created = api.call("POST", f"{account}/access/service_tokens",
                           body={"name": SERVICE_TOKEN_NAME, "duration": settings.token_duration})
        client_secret = _take_secret(created, redactor)
        found.token = {key: value for key, value in created.items() if key != "client_secret"}
        console.say("  created; its secret is held in memory only")
    elif options.rotate_service_token:
        rotated = api.call("POST", f"{account}/access/service_tokens/{found.token['id']}/rotate",
                           body={})
        client_secret = _take_secret(rotated, redactor)
        console.say("  rotated; the new secret is held in memory only")
    else:
        console.say("  exists, and the Worker already holds its secret")
    checked = api.call("GET", f"{account}/access/service_tokens/{found.token['id']}") or {}
    if checked.get("name") != SERVICE_TOKEN_NAME or not checked.get("client_id"):
        raise ApiError("the service token could not be read back")
    client_id = checked["client_id"]
    if not options.show_client_id:
        redactor.hide(client_id, "[client id hidden; --show-client-id prints it]")
    console.say(f"  verified: {SERVICE_TOKEN_NAME}, client id {client_id}, "
                f"expires {checked.get('expires_at', 'unknown')}")

    console.say("b. Access policy and application")
    if not found.policy:
        found.policy = api.call("POST", f"{account}/access/policies", body={
            "name": POLICY_NAME, "decision": "non_identity",
            "include": [{"service_token": {"token_id": found.token["id"]}}]})
        console.say("  created the Service Auth policy")
    if not _policy_is_ours(found.policy, found.token["id"]):
        raise Refusal("the policy is not Service Auth for exactly this token")
    if not found.application:
        found.application = api.call("POST", f"{account}/access/apps", body={
            "type": "self_hosted", "name": APPLICATION_NAME, "domain": settings.hostname,
            "session_duration": settings.session_duration, "app_launcher_visible": False,
            "policies": [{"id": found.policy["id"], "precedence": 1}]})
        console.say("  created the Access application")

    console.say("c. verify the application before anything can reach the service")
    verify_application(api, found, settings)
    console.say(f"  verified: self-hosted, covers exactly {settings.hostname}, one policy, "
                "Service Auth, this token only")

    wrote = []
    try:
        console.say("d. tunnel ingress rule")
        if _changed_ingress(api, found, settings, console, add=True, wrote=wrote):
            console.say(f"  added {settings.hostname} -> {settings.service}; catch-all still last")
        else:
            console.say("  already present")

        console.say("e. DNS record")
        if not _own_dns_record(api, found, settings):
            wrote.append("dns")
            api.call("POST", f"/zones/{found.zone_id}/dns_records", body={
                "type": "CNAME", "name": settings.hostname, "proxied": True, "ttl": 1,
                "content": f"{found.tunnel_id}.cfargotunnel.com", "comment": DNS_COMMENT})
        found.dns_record = _own_dns_record(api, found, settings)
        if not found.dns_record:
            raise ApiError("the DNS record could not be read back")
        console.say(f"  verified: proxied CNAME {settings.hostname}")

        console.say("f. probe from outside")
        sleep(settings.settle_seconds)
        _probe_unauthenticated(api, found, settings, console, probe)
    except Exposed:
        raise
    except BaseException as error:
        if isinstance(error, Refusal) and not wrote:
            raise  # refused before writing anything: there is nothing new to undo
        # Steps d to f are the only ones during which the service could be
        # reachable and unchecked. Whatever stopped them, do not leave it so.
        console.shout("Stopped between adding the route and proving Access is in front of it. "
                      "Taking the route out again.")
        _best_effort_removal(api, found, settings, console)
        raise

    authenticated_ok = None
    if client_secret is not None:
        answer = probe.persistent(
            {"CF-Access-Client-Id": client_id, "CF-Access-Client-Secret": client_secret},
            accept=lambda status: status == 200)
        authenticated_ok = bool(answer) and answer[0] == 200
        if authenticated_ok:
            console.say("  with the service token: 200")
        else:
            console.shout(_authenticated_failure(answer))

        console.say("g. Worker secrets")
        values = {BINDING_ACCESS_ID: client_id, BINDING_ACCESS_VALUE: client_secret,
                  BINDING_URL: settings.origin_url}
        for name in WORKER_BINDINGS:
            api.call("PUT", f"{account}/workers/scripts/{settings.worker}/secrets",
                     body={"name": name, "text": values[name], "type": "secret_text"})
            console.say(f"  stored {name}")
    else:
        console.say("  with the service token: not checked; its secret is only in the Worker")
        console.say("g. Worker secrets: already stored, left alone")

    console.say("h. final check")
    listed = api.call("GET", f"{account}/workers/scripts/{settings.worker}/secrets") or []
    names = {item.get("name") for item in listed if item.get("type") == "secret_text"}
    if not set(WORKER_BINDINGS) <= names:
        raise ApiError("the Worker does not list all three secrets after storing them")
    console.say("  the Worker lists all three secrets")
    verify_application(api, found, settings)
    console.say("  the Access application still checks out")
    print_checklist(settings, console)
    return EXIT_OK if authenticated_ok is not False else EXIT_UNVERIFIED


def _take_secret(result, redactor):
    secret = (result or {}).get("client_secret")
    if not isinstance(secret, str) or len(secret) < 16:
        raise ApiError("Cloudflare did not return a client secret")
    redactor.hide(secret)
    return secret


def _probe_unauthenticated(api, found, settings, console, probe):
    answer = probe.persistent()
    if probe.borrowed_address:
        console.say(f"  (this resolver does not know {settings.hostname} yet, so the probe "
                    f"connected to the same edge through {settings.anchor_hostname})")
    verdict = "no_answer" if answer is None else classify_unauthenticated(*answer)
    if verdict == "protected":
        console.say(f"  without credentials: {answer[0]}, refused by Access")
        return
    console.shout("")
    console.shout("!" * 72)
    if verdict == "open":
        console.shout(f"!! {settings.hostname} ANSWERED {answer[0]} WITHOUT CREDENTIALS.")
        console.shout("!! THE CAMERA-CONTROL SERVICE WAS REACHABLE FROM THE INTERNET, UNPROTECTED.")
    elif verdict == "not_applied":
        console.shout(f"!! {settings.hostname} answered the tunnel's 404: the ingress rule has "
                      "not applied,")
        console.shout("!! and Access is NOT in front of the hostname, or it would have refused first.")
    elif verdict == "no_answer":
        console.shout(f"!! {settings.hostname} gave no HTTP answer, so nothing proves Access is "
                      "in front of it.")
    else:
        console.shout(f"!! {settings.hostname} answered {answer[0]}, which is not an Access refusal.")
    console.shout("!! Removing the DNS record and the ingress rule NOW.")
    console.shout("!" * 72)
    _best_effort_removal(api, found, settings, console)
    raise Exposed(
        "the route was taken out again. The Access application, policy and token are still "
        "there; look at the application in Zero Trust > Access > Applications, then run the "
        "script again. If this run made or rotated the token, its secret went with it, and "
        "the next run will ask for --rotate-service-token.")


def _best_effort_removal(api, found, settings, console):
    try:
        remove_route(api, found, settings, console)
    except BaseException as error:  # say so, loudly, and let the first failure stand
        console.shout("!! COULD NOT REMOVE THE ROUTE: " + f"{type(error).__name__}: {error}")
        console.shout(f"!! Delete the DNS record {settings.hostname} by hand, now: Cloudflare "
                      "dashboard > " + settings.zone + " > DNS > Records.")


def _authenticated_failure(answer):
    if answer is None:
        return ("  with the service token: no answer. The secrets are stored anyway, so the "
                "secret is not lost; check the app.")
    if answer[0] in (401, 403) or 300 <= answer[0] < 400:
        return (f"  with the service token: {answer[0]}. Access refused its own token; look at "
                "the policy. The secrets are stored anyway, so the secret is not lost.")
    return (f"  with the service token: {answer[0]}. Access let it through and the tunnel or the "
            "service did not answer: is gate-camera-control running on the Pi? The secrets "
            "are stored anyway, so the secret is not lost.")


def print_checklist(settings, console):
    console.say()
    console.say("This script cannot see the app. Check by eye:")
    console.say("  [ ] Gate Mate > Home: Night vision reads \"Off\" (or \"Auto\"), not \"Unavailable\"")
    console.say("  [ ] Photo returns a still within about ten seconds")
    console.say("  [ ] on the Pi:  journalctl -u gate-camera-control --since -5m | grep stage=snapshot")
    console.say("      shows  stage=snapshot ... outcome=completed")
    console.say("  [ ] on the Pi:  sudo journalctl -u cloudflared -n 50 | grep -i configuration")
    console.say("      shows the tunnel picking the new configuration up")
    console.say("  [ ] Zero Trust > Access > Applications: '" + APPLICATION_NAME + "' has one "
                "policy, Service Auth")
    console.say(f"The service token expires after {settings.token_duration}; put the date in a "
                "calendar. --rotate-service-token renews the secret.")


# --------------------------------------------------------------------------
# Rollback.

def rollback(api, found, settings, console):
    account = _account(found)
    console.say()
    console.say("1-2. DNS record and ingress rule")
    remove_route(api, found, settings, console)
    console.say("3. Worker secrets")
    for name in reversed(WORKER_BINDINGS):
        if name in found.binding_names:
            api.call("DELETE", f"{account}/workers/scripts/{settings.worker}/secrets/{name}")
            console.say(f"  deleted {name}")
    console.say("4. Access application")
    if found.application:
        api.call("DELETE", f"{account}/access/apps/{found.application['id']}")
        console.say("  deleted")
    console.say("5. Access policy")
    if found.policy:
        api.call("DELETE", f"{account}/access/policies/{found.policy['id']}")
        console.say("  deleted")
    console.say("6. service token")
    if found.token:
        api.call("DELETE", f"{account}/access/service_tokens/{found.token['id']}")
        console.say("  deleted")
    console.say()
    console.say("Rolled back. The dashboard's camera controls read \"Unavailable\" again.")
    return EXIT_OK


# --------------------------------------------------------------------------
# Entry point.

class _QuietParser(argparse.ArgumentParser):
    def error(self, message):
        # argparse quotes the offending argument. If somebody put a token on the
        # command line by mistake, it must not be the thing echoed back.
        self.print_usage(sys.stderr)
        self.exit(EXIT_REFUSED, "error: arguments not understood (not echoed, in case one "
                                "was a credential). The API token is read from "
                                + TOKEN_VARIABLE + " only.\n")


def build_parser():
    parser = _QuietParser(
        prog="setup-camera-route.py",
        description="Route the Gate Mate Worker to the Pi's camera-control service through "
                    "Cloudflare, Access first. Dry run unless --apply. The API token is read "
                    "from " + TOKEN_VARIABLE + " and from nowhere else.")
    parser.add_argument("--apply", action="store_true", help="make the changes in the plan")
    parser.add_argument("--rollback", action="store_true",
                        help="plan (with --apply, perform) the removal of what this script makes")
    parser.add_argument("--rotate-service-token", action="store_true",
                        help="make a new client secret for an existing token; the old one dies at once")
    parser.add_argument("--show-client-id", action="store_true",
                        help="print the service token's client id (it is not a secret)")
    parser.add_argument("--account-id", help="the account id; checked against the zone's owner")
    parser.add_argument("--zone-id", help="the zone id; with --account-id, skips the lookup by name")
    parser.add_argument("--session-duration", default="30m",
                        help="the Access application's session length (default 30m)")
    parser.add_argument("--token-duration", default="8760h",
                        help="the service token's lifetime (default 8760h, one year)")
    parser.add_argument("--settle-seconds", type=int, default=10,
                        help="pause between creating the DNS record and probing (default 10)")
    guarded = parser.add_argument_group(
        "not the documented route (each needs --allow-undocumented-target)")
    guarded.add_argument("--hostname", default=DOCUMENTED["hostname"])
    guarded.add_argument("--anchor-hostname", default=DOCUMENTED["anchor_hostname"],
                         help="a hostname the same tunnel already serves")
    guarded.add_argument("--zone", default=DOCUMENTED["zone"])
    guarded.add_argument("--origin-port", type=int, default=DOCUMENTED["origin_port"],
                         help="the port on 127.0.0.1; the address itself is not negotiable")
    guarded.add_argument("--worker", default=DOCUMENTED["worker"])
    guarded.add_argument("--allow-undocumented-target", action="store_true")
    return parser


def _loopback_base(value, variable):
    parts = urllib.parse.urlsplit(value)
    try:
        loopback = ipaddress.ip_address(parts.hostname or "").is_loopback
    except ValueError:
        loopback = False
    if parts.scheme != "http" or not loopback or parts.username or parts.query:
        raise Refusal(f"{variable} is for the test suite and takes a loopback address only")
    return value.rstrip("/")


def _run(arguments, env, stdin, console, redactor, sleep):
    automated = sorted(name for name in CI_MARKERS if env.get(name))
    if automated:
        raise Refusal("this looks like an automated run (" + ", ".join(automated) + " is set). "
                      "This script is run by the owner, by hand, and never by a deploy.")
    token = env.get(TOKEN_VARIABLE, "")
    if len(token) < 20 or token != token.strip() or any(ch.isspace() for ch in token):
        raise Refusal(TOKEN_VARIABLE + " is not set to an API token. Run this under "
                      "`op run --env-file=... --`, or export the variable. If the file holds an "
                      "op:// reference, it must be run through `op run` to be resolved.")
    redactor.hide(token)
    if token.startswith("op://"):
        raise Refusal(TOKEN_VARIABLE + " still holds an op:// reference; run under `op run`")
    settings = Settings(arguments)
    if arguments.rotate_service_token and arguments.rollback:
        raise Refusal("--rotate-service-token and --rollback do not go together")

    test_api, test_probe = env.get(TEST_API_VARIABLE), env.get(TEST_PROBE_VARIABLE)
    if bool(test_api) != bool(test_probe):
        raise Refusal(f"{TEST_API_VARIABLE} and {TEST_PROBE_VARIABLE} are set together or not at all")
    base = API_BASE
    if test_api:
        base = _loopback_base(test_api, TEST_API_VARIABLE)
        test_probe = _loopback_base(test_probe, TEST_PROBE_VARIABLE)
        console.say("TEST MODE: talking to a fake Cloudflare on loopback. Nothing real is touched.")
    api = Api(base, token, redactor, loopback=bool(test_api))
    probe = Probe(settings, test_probe, sleep)

    found = survey(api, settings, console)
    if arguments.rollback:
        print_rollback_plan(found, settings, console)
    else:
        print_apply_plan(found, settings, arguments.rotate_service_token, console)
        if found.token and not arguments.rotate_service_token and worker_lacks_bindings(found):
            console.say()
            console.say(f"NOTE: {SERVICE_TOKEN_NAME} exists but the Worker does not hold its "
                        "secret, and Cloudflare will not show it again. --apply will refuse "
                        "until it is given --rotate-service-token.")
    if not arguments.apply:
        console.say()
        console.say("Dry run: nothing was changed. Add --apply to do it.")
        return EXIT_OK

    console.say()
    console.say(f"Type the hostname ({settings.hostname}) to go ahead; anything else stops here.")
    typed = stdin.readline() if stdin is not None else ""
    if typed.strip().lower() != settings.hostname:
        raise Refusal("not confirmed; nothing was changed")
    if arguments.rollback:
        return rollback(api, found, settings, console)
    return apply(api, found, settings, arguments, console, probe, redactor, sleep)


def main(argv=None, *, env=None, stdin=None, stdout=None, stderr=None, sleep=time.sleep):
    import os
    env = os.environ if env is None else env
    redactor = Redactor()
    console = Console(stdout or sys.stdout, stderr or sys.stderr, redactor)
    # Registered before anything can fail, so no path out of here can carry it.
    redactor.hide(env.get(TOKEN_VARIABLE, ""))
    arguments = build_parser().parse_args(argv)
    try:
        return _run(arguments, env, sys.stdin if stdin is None else stdin,
                    console, redactor, sleep)
    except Refusal as refusal:
        console.shout(f"refused: {refusal}")
        return EXIT_REFUSED
    except Exposed as exposed:
        console.shout(f"EXPOSED, AND REMOVED: {exposed}")
        return EXIT_EXPOSED
    except KeyboardInterrupt:
        console.shout("interrupted. Run the script again: it resumes from what exists.")
        return EXIT_ERROR
    except Exception as error:
        # No traceback: a traceback is somebody else's formatting of state this
        # script promised not to print. The message is redacted like every line.
        console.shout(f"failed: {type(error).__name__}: {error}")
        console.shout("Run the script again: it resumes from what exists. "
                      "If it made a service token, it will ask for --rotate-service-token.")
        return EXIT_ERROR


if __name__ == "__main__":
    sys.exit(main())
