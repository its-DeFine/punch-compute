#!/usr/bin/env python3
"""Create-once native setup for the currently accepted private strict profile.
Persistent operation identity, atomic private files and actual native readback.
This is initialization, not a complete installer or arbitrary-model support.
"""
import argparse
import hashlib
import fcntl
import json
import os
import re
import secrets
import stat
import subprocess
import sys
import urllib.request
from urllib.error import HTTPError
from urllib.parse import urlsplit

MODEL_DEFAULT = "punch-test-model"
CHANNEL_DEFAULT = "fixture-rtx5080"
BACKEND_DEFAULT = "http://model:8080"
INPUT_RATIO = 1.0
OUTPUT_RATIO = 5.0

CRED_FILES = ("admin_user", "admin_pass", "seller.token")
STATE_FILE = "setup-state.json"


class Failure(Exception):
    def __init__(self, code, hint=""):
        super().__init__(code)
        self.code = code
        self.hint = hint


def write_private(path, data):
    parent = os.path.dirname(path)
    if os.path.lexists(path):
        read_private(path)
    tmp = path + ".tmp"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        os.fchmod(f.fileno(), 0o600)
        f.write(data)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)
    fd = os.open(parent, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def read_private(path):
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    with os.fdopen(fd, encoding="utf-8") as f:
        st = os.fstat(f.fileno())
        if not stat.S_ISREG(st.st_mode) or st.st_mode & 0o077 or st.st_nlink != 1:
            raise Failure("PRIVATE_FILE_REQUIRED")
        return f.read().strip()


class Transport:
    """Native HTTP API; refuse redirects so credentials cannot change destination."""

    class NoRedirect(urllib.request.HTTPRedirectHandler):
        def redirect_request(self, *args, **kwargs):
            return None

    def call(self, base, method, path, body=None, headers=None):
        data = None if body is None else json.dumps(body).encode()
        req = urllib.request.Request(base.rstrip("/") + path, data=data,
                                     headers=headers or {}, method=method)
        try:
            with urllib.request.build_opener(self.NoRedirect).open(req, timeout=30) as r:
                return r.status, json.load(r)
        except HTTPError as e:
            try:
                return e.code, json.loads(e.read().decode())
            except ValueError:
                return e.code, {"success": False}


def load_state(app_dir):
    path = os.path.join(app_dir, "data", STATE_FILE)
    try:
        state = json.loads(read_private(path))
    except FileNotFoundError:
        return {}
    except (OSError, ValueError):
        raise Failure("SETUP_STATE_UNREADABLE")
    if not isinstance(state, dict):
        raise Failure("SETUP_STATE_INVALID")
    return state


def save_state(app_dir, state):
    write_private(os.path.join(app_dir, "data", STATE_FILE), json.dumps(state, sort_keys=True))


def mark_step(app_dir, state, step):
    state.setdefault("steps", {})[step] = True
    save_state(app_dir, state)


MODEL_PROFILE_MODEL = re.compile(r"^punch-[0-9a-f]{64}$")
MODEL_PROFILE_SHA = re.compile(r"^[0-9a-fA-F]{64}$")


def load_model_profile(path):
    """Read a prepared public default-model manifest (local non-secret data).

    Never reads app credentials: the caller passes the explicit CLI file path,
    opened as plain data. Returns the pinned {model, identity, profile} triple;
    any other manifest fields (offer terms for publication) pass through
    untouched and unconsumed here.
    """
    try:
        with open(path, encoding="utf-8") as f:
            manifest = json.load(f)
    except (OSError, ValueError):
        raise Failure("MODEL_PROFILE_UNREADABLE")
    if not isinstance(manifest, dict):
        raise Failure("MODEL_PROFILE_INVALID")
    model = manifest.get("model")
    if not isinstance(model, str) or not MODEL_PROFILE_MODEL.fullmatch(model):
        raise Failure("MODEL_PROFILE_INVALID", "model")
    identity = manifest.get("identity")
    if not isinstance(identity, dict) or not identity:
        raise Failure("MODEL_PROFILE_INVALID", "identity")
    if not isinstance(identity.get("sha256"), str) or not MODEL_PROFILE_SHA.fullmatch(identity["sha256"]):
        raise Failure("MODEL_PROFILE_INVALID", "identity.sha256")
    profile = manifest.get("profile")
    if not isinstance(profile, dict) or not profile:
        raise Failure("MODEL_PROFILE_INVALID", "profile")
    return {"model": model, "identity": identity, "profile": profile}


def ensure_credentials(app_dir, transport, base, state):
    code, body = transport.call(base, "GET", "/api/setup")
    if code != 200 or body.get("success") is not True or not isinstance(body.get("data"), dict):
        raise Failure("SETUP_STATUS_UNREACHABLE")
    native = body["data"]
    present = {n: os.path.lexists(os.path.join(app_dir, n)) for n in CRED_FILES}
    saved = state.get("credentials")
    if saved is not None:
        if not isinstance(saved, dict) or set(saved) != set(CRED_FILES) or not all(isinstance(v,str) and v for v in saved.values()):
            raise Failure("SETUP_STATE_INVALID")
        for n in CRED_FILES:
            if present[n] and read_private(os.path.join(app_dir,n)) != saved[n]:
                raise Failure("CREDENTIAL_IDENTITY_MISMATCH")
    elif all(present.values()):
        saved = {n: read_private(os.path.join(app_dir,n)) for n in CRED_FILES}
    elif any(present.values()):
        raise Failure("AMBIGUOUS_CREDENTIALS", "Restore the matching credential set; do not reset identities.")
    else:
        if native.get("status") or native.get("root_init"):
            raise Failure("RETAINED_DB_NO_CREDS", "Restore the existing administrator credentials.")
        saved = {"admin_user":"punch-admin", "admin_pass":secrets.token_urlsafe(24),
                 "seller.token":secrets.token_urlsafe(32)}
        state["created_native"] = True
    state["credentials"] = saved
    save_state(app_dir,state)  # Recovery identity durable before file publication or API writes.
    for n in CRED_FILES:
        if not present[n]:
            write_private(os.path.join(app_dir,n),saved[n])
    username,password = saved["admin_user"],saved["admin_pass"]
    if not native.get("status"):
        if native.get("root_init"):
            login_verify(transport,base,username,password)
        mark_step(app_dir,state,"setup_requested")
        code, body = transport.call(base,"POST","/api/setup",
            {"username":username,"password":password,"confirmPassword":password},
            {"Content-Type":"application/json"})
        if code != 200 or body.get("success") is not True:
            raise Failure("SETUP_CREATE_FAILED")
    return username,password


def login_verify(transport, base, username, password):
    code, body = transport.call(base, "POST", "/api/user/login",
                                {"username": username, "password": password},
                                {"Content-Type": "application/json"})
    if code != 200 or not body.get("success"):
        raise Failure("LOGIN_FAILED")
    token = body["data"]["access_token"]
    code, me = transport.call(base, "GET", "/api/user/self",
                              headers={"Authorization": "Bearer " + token})
    if code != 200 or not me.get("success"):
        raise Failure("SELF_VERIFY_FAILED")
    if me["data"].get("id") != body["data"].get("user", {}).get("id"):
        raise Failure("IDENTITY_MISMATCH")
    return token


def ensure_channel(transport, base, admin_headers, app_dir, state, name, model, backend):
    code, body = transport.call(base, "GET", "/api/channel/", headers=admin_headers)
    if code != 200 or not body.get("success"):
        raise Failure("CHANNEL_LIST_FAILED")
    items = body.get("data", {}).get("items")
    if not isinstance(items, list):
        raise Failure("CHANNEL_LIST_INVALID")
    if body["data"].get("total", len(items)) > len(items):
        raise Failure("CHANNEL_LIST_INCOMPLETE")
    if sum(ch.get("name") == name for ch in items) > 1:
        raise Failure("CHANNEL_IDENTITY_AMBIGUOUS")
    for ch in items:
        if ch.get("name") == name:
            want = {"base_url": backend, "models": model}
            got = {k: ch.get(k) for k in want}
            if got != want:
                raise Failure("CHANNEL_MISMATCH",
                              "owned channel differs; operator must reconcile, never silently overwrite")
            if ch.get("status") != 1:
                raise Failure("CHANNEL_DISABLED")
            return ch.get("id")
    mark_step(app_dir, state, "channel:" + name)
    code, body = transport.call(base, "POST", "/api/channel/",
                                {"channel": {"name": name, "type": 1, "key": "sk-fixture",
                                             "base_url": backend, "models": model, "group": "default", "status": 1},
                                 "mode": "single"}, admin_headers)
    if code != 200 or not body.get("success"):
        raise Failure("CHANNEL_CREATE_FAILED", "Native channel creation was refused.")
    code, body = transport.call(base, "GET", "/api/channel/", headers=admin_headers)
    for ch in body.get("data", {}).get("items", []):
        if ch.get("name") == name:
            if ch.get("base_url") != backend or ch.get("models") != model:
                raise Failure("CHANNEL_READBACK_MISMATCH")
            return ch.get("id")
    raise Failure("CHANNEL_READBACK_MISSING")


def ensure_ratios(transport, base, admin_headers, model):
    code, body = transport.call(base, "GET", "/api/option/", headers=admin_headers)
    if code != 200 or not body.get("success"):
        raise Failure("OPTION_LIST_FAILED")
    opts = {o["key"]: o.get("value") for o in body["data"]}
    changed = []
    for key, want in (("ModelRatio", INPUT_RATIO), ("CompletionRatio", OUTPUT_RATIO), ("CacheRatio", 1.0)):
        try:
            cur = json.loads(opts.get(key) or "{}")
        except ValueError:
            raise Failure("RATIO_UNPARSEABLE", key)
        if not isinstance(cur, dict):
            raise Failure("RATIO_UNPARSEABLE", key)
        if cur.get(model) == want:
            continue
        if model in cur:
            raise Failure("RATIO_MISMATCH", key)
        cur[model] = want
        code, body = transport.call(base, "PUT", "/api/option/",
                                    {"key": key, "value": json.dumps(cur)}, admin_headers)
        if code != 200 or not body.get("success"):
            raise Failure("RATIO_SET_FAILED", key)
        changed.append(key)
    code, result = transport.call(base, "GET", "/api/option/", headers=admin_headers)
    if code != 200 or result.get("success") is not True:
        raise Failure("OPTION_READBACK_FAILED")
    values = {o["key"]: o.get("value") for o in result["data"]}
    for key, want in (("ModelRatio",1.0),("CompletionRatio",5.0),("CacheRatio",1.0)):
        if json.loads(values.get(key) or "{}").get(model) != want:
            raise Failure("RATIO_READBACK_MISMATCH",key)
    return changed


NATIVE_INIT_TIMEOUT = 90


def ensure_native_account(app_dir, bundle_root, state, node_account=None):
    """Opt-in Livepeer node account preparation. Pins state["native_account"]
    durably under the setup lock before the caller may start the node.
    Unfunded default behavior is untouched when the caller does not opt in."""
    HEX = set("0123456789abcdefABCDEF")
    selected = node_account or None
    if selected is not None and not (
            isinstance(selected, str) and len(selected) == 42
            and selected[:2] == "0x" and all(c in HEX for c in selected[2:])):
        raise Failure("NATIVE_ACCOUNT_INVALID", "Selected address must be 0x-prefixed 20-byte hex.")
    saved = state.get("native_account")
    if saved is not None:
        if not isinstance(saved, dict) or not isinstance(saved.get("address"), str) or not saved["address"]:
            raise Failure("SETUP_STATE_INVALID")
        if selected is not None and selected.lower() != saved["address"].lower():
            raise Failure("NATIVE_ACCOUNT_MISMATCH", "Supplied address disagrees with the pinned native account.")
        selected = saved["address"]
    bundle_root = os.path.realpath(bundle_root)
    native_dir = os.path.join(bundle_root, "native")
    keystore = os.path.join(native_dir, "keystore")
    passfile = os.path.join(native_dir, "password")
    if saved is not None and (not os.path.lexists(passfile) or not os.path.lexists(keystore)):
        raise Failure("NATIVE_STATE_INCOMPLETE", "Restore the native password and keystore; identities are never replaced.")
    if not os.path.lexists(passfile) and os.path.isdir(keystore) and os.listdir(keystore):
        raise Failure("NATIVE_STATE_INCOMPLETE", "Restore the existing password before selecting this keystore.")
    os.makedirs(native_dir, mode=0o700, exist_ok=True)
    if os.path.islink(native_dir) or not os.path.isdir(native_dir) or os.stat(native_dir).st_mode & 0o077:
        raise Failure("PRIVATE_DIRECTORY_REQUIRED")
    keystore = os.path.join(native_dir, "keystore")
    os.makedirs(keystore, mode=0o700, exist_ok=True)
    if os.path.islink(keystore) or not os.path.isdir(keystore) or os.stat(keystore).st_mode & 0o077:
        raise Failure("PRIVATE_DIRECTORY_REQUIRED")
    passfile = os.path.join(native_dir, "password")
    if saved is not None:
        # Pinned identity: every private file must still exist; never replace.
        if not os.path.lexists(passfile) or not os.path.lexists(keystore):
            raise Failure("NATIVE_STATE_INCOMPLETE", "Restore the native password and keystore; identities are never replaced.")
        read_private(passfile)
    elif not os.path.lexists(passfile):
        write_private(passfile, secrets.token_urlsafe(32))
    else:
        read_private(passfile)
    # A timed-out Docker client may leave its initializer running. A stable
    # container name prevents a concurrent retry from creating another identity.
    init_name = "punch-node-init-" + hashlib.sha256(bundle_root.encode()).hexdigest()[:16]
    argv = ["docker", "compose", "run", "--rm", "--no-deps", "--name", init_name,
            "--entrypoint", "/usr/local/bin/livepeer", "livepeer",
            "-initAccount", "-ethKeystorePath", "/data/keystore",
            "-ethPassword", "/data/password"]
    if selected is not None:
        argv += ["-ethAcctAddr", selected]
    try:
        proc = subprocess.run(argv, cwd=os.path.abspath(bundle_root),
                              capture_output=True, text=True, timeout=NATIVE_INIT_TIMEOUT)
    except subprocess.TimeoutExpired:
        raise Failure("NATIVE_INIT_TIMEOUT", "Native initializer exceeded 90s; retry reuses the existing account.")
    except OSError:
        raise Failure("NATIVE_COMPOSE_UNAVAILABLE", "Fixed Compose command could not start.")
    if proc.returncode != 0:
        raise Failure("NATIVE_INIT_FAILED", "exit=%d" % proc.returncode)
    try:
        address = json.loads(proc.stdout).get("address")
    except (ValueError, AttributeError):
        raise Failure("NATIVE_INIT_INVALID", "Native initializer did not return address JSON.")
    if not isinstance(address, str) or len(address) != 42 or address[:2] != "0x" or any(c not in HEX for c in address[2:]):
        raise Failure("NATIVE_INIT_INVALID", "Native initializer did not return a checksum address.")
    if selected is not None and address.lower() != selected.lower():
        raise Failure("NATIVE_ACCOUNT_MISMATCH", "Native initializer returned a different account; pinned identity kept.")
    if saved is None:
        state["native_account"] = {"address": address}
        save_state(app_dir, state)  # Pinned before the caller may start the node.
    return address


NATIVE_CONFIG_KEYS = ("recipient", "network", "rpc", "service_url")


def ensure_native_config(app_dir, bundle_root, state, native_config=None):
    """Prepare real native flags; RPC and registration material stay private."""
    native_dir = os.path.join(os.path.realpath(bundle_root), "native")
    private_config = os.path.join(native_dir, "config.json")
    saved = state.get("native_config")
    def read_config(path):
        try:
            value = json.loads(read_private(path))
        except (OSError, ValueError):
            raise Failure("NATIVE_CONFIG_UNREADABLE")
        if not isinstance(value, dict) or set(value) - set(NATIVE_CONFIG_KEYS) - {"account"}:
            raise Failure("NATIVE_CONFIG_INVALID")
        return value
    retained = read_config(private_config) if os.path.lexists(private_config) else {}
    if saved is not None and not retained:
        raise Failure("NATIVE_STATE_INCOMPLETE", "Restore the private native configuration.")
    raw = read_config(native_config) if native_config is not None else {}
    account = state.get("native_account", {}).get("address")
    address = re.compile(r"^0x[0-9a-fA-F]{40}$")
    if not isinstance(account, str) or not address.fullmatch(account):
        raise Failure("NATIVE_ACCOUNT_REQUIRED")
    if (not isinstance(raw.get("account", account), str) or raw.get("account", account).lower() != account.lower()):
        raise Failure("NATIVE_ACCOUNT_MISMATCH")
    effective = dict(retained)
    for key in NATIVE_CONFIG_KEYS:
        if key in raw:
            if retained and raw[key] != retained.get(key):
                raise Failure("NATIVE_CONFIG_MISMATCH", key)
            effective[key] = raw[key]
    if any(k not in effective for k in NATIVE_CONFIG_KEYS):
        raise Failure("NATIVE_CONFIG_INCOMPLETE")
    if any(not isinstance(effective[k], str) or not effective[k]
           or any(c.isspace() or ord(c) < 32 or ord(c) == 127 for c in effective[k])
           for k in NATIVE_CONFIG_KEYS):
        raise Failure("NATIVE_CONFIG_INVALID")
    if not address.fullmatch(effective["recipient"]) or int(effective["recipient"], 16) == 0:
        raise Failure("NATIVE_CONFIG_INVALID", "recipient")
    if effective["network"] != "arbitrum-one-mainnet":
        raise Failure("NATIVE_CONFIG_INVALID", "Supported payment network is arbitrum-one-mainnet.")
    for key in ("rpc", "service_url"):
        try:
            parsed = urlsplit(effective[key])
            port = parsed.port
        except ValueError:
            raise Failure("NATIVE_CONFIG_INVALID", key)
        if (parsed.scheme not in ("http", "https") or not parsed.hostname
                or parsed.username or parsed.password or parsed.fragment
                or '#' in effective[key]
                or (key == "service_url" and (parsed.query or parsed.path not in ("", "/")))):
            raise Failure("NATIVE_CONFIG_INVALID", key)
    public = {k: effective[k] for k in ("recipient", "network", "service_url")}
    public.update(account=account, rpc_digest=hashlib.sha256(effective["rpc"].encode()).hexdigest())
    if saved is not None and saved != public:
        raise Failure("NATIVE_CONFIG_MISMATCH")
    os.makedirs(native_dir, mode=0o700, exist_ok=True)
    if os.path.islink(native_dir) or not os.path.isdir(native_dir) or os.stat(native_dir).st_mode & 0o077:
        raise Failure("PRIVATE_DIRECTORY_REQUIRED")
    secret_file = os.path.join(app_dir, "native.registration")
    if not os.path.lexists(secret_file):
        if saved is not None:
            raise Failure("NATIVE_STATE_INCOMPLETE", "Restore the registration secret.")
        write_private(secret_file, secrets.token_urlsafe(32))
    secret = read_private(secret_file).strip()
    if not re.fullmatch(r"[A-Za-z0-9_-]{32,128}", secret):
        raise Failure("NATIVE_STATE_INCOMPLETE", "Registration secret invalid.")
    flags = {
        "network": effective["network"], "ethUrl": effective["rpc"],
        "serviceAddr": effective["service_url"], "ethOrchAddr": effective["recipient"],
        "ethKeystorePath": "/data/keystore", "ethPassword": "/data/password",
        "ethAcctAddr": account, "orchSecret": secret,
        "orchestrator": "true", "useLiveRunners": "true",
        "datadir": "/data/state", "httpAddr": "http://0.0.0.0:8935",
        "cliAddr": "127.0.0.1:7935", "liveRunnerAddr": "http://livepeer:8935",
        "startupAvailabilityCheck": "false", "enableCliTxRoutes": "false",
        # Go requires an explicit node base price even with live runners.
        # Fixed token-order prices remain pinned by each runner registration.
        "pricePerUnit": "1", "pixelsPerUnit": "1",
        # Certain fixed tickets use deposited funds, not only probabilistic reserve float.
        "fullFundsCoverage": "true",
    }
    contents = "".join(key + " " + value + "\n" for key, value in flags.items())
    node_conf = os.path.join(native_dir, "node.conf")
    if os.path.lexists(node_conf):
        retained_contents = read_private(node_conf)
        legacy_contents = "".join(key + " " + value + "\n" for key, value in flags.items()
                                  if key not in ("pricePerUnit", "pixelsPerUnit"))
        previous_contents = contents.replace("httpAddr http://0.0.0.0:8935\n", "httpAddr 0.0.0.0:8935\n")
        legacy_contents = legacy_contents.replace("httpAddr http://0.0.0.0:8935\n", "httpAddr 0.0.0.0:8935\n")
        known = (contents, previous_contents, legacy_contents)
        known += tuple(value.replace("fullFundsCoverage true\n", "") for value in known)
        if retained_contents not in tuple(value.strip() for value in known):
            raise Failure("NATIVE_CONFIG_MISMATCH", "Retained node.conf differs.")
    write_private(private_config, json.dumps(effective, sort_keys=True))
    write_private(node_conf, contents)
    state["native_config"] = public
    save_state(app_dir, state)
    return public


def init_bundle(bundle_root, api_base, transport=None, channel_name=CHANNEL_DEFAULT,
                model=MODEL_DEFAULT, backend=BACKEND_DEFAULT,
                livepeer=False, node_account=None, native_config=None,
                model_profile=None):
    if (node_account is not None or native_config is not None) and not livepeer:
        raise Failure("LIVEPEER_OPT_IN_REQUIRED")
    manifest = load_model_profile(model_profile) if model_profile is not None else None
    if manifest is not None:
        if model != MODEL_DEFAULT and model != manifest["model"]:
            raise Failure("MODEL_PROFILE_MISMATCH", "Explicit --model disagrees with the profile manifest.")
        model = manifest["model"]
    if backend != BACKEND_DEFAULT:
        raise Failure("PROFILE_NOT_SUPPORTED", "Only the accepted strict profile is configured in Phase 1.")
    if manifest is None and model != MODEL_DEFAULT:
        raise Failure("PROFILE_NOT_SUPPORTED", "Only the accepted strict profile is configured in Phase 1.")
    parsed=urlsplit(api_base)
    if parsed.scheme != "http" or parsed.hostname not in ("127.0.0.1","localhost","new-api") or parsed.username or parsed.password or parsed.path not in ("","/") or parsed.query or parsed.fragment:
        raise Failure("PRIVATE_LOCAL_API_REQUIRED")
    os.umask(0o077)
    app_dir = os.path.join(os.path.abspath(bundle_root), "app")
    os.makedirs(os.path.join(app_dir,"data"),mode=0o700,exist_ok=True)
    for directory in (app_dir,os.path.join(app_dir,"data")):
        if os.path.islink(directory) or not os.path.isdir(directory):
            raise Failure("PRIVATE_DIRECTORY_REQUIRED")
    lockfd=os.open(os.path.join(app_dir,"data","setup.lock"),os.O_RDWR|os.O_CREAT|os.O_NOFOLLOW,0o600)
    with os.fdopen(lockfd,"w") as lock:
        try:fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
        except BlockingIOError:raise Failure("SETUP_BUSY")
        state=load_state(app_dir)
        binding={"api_base":api_base.rstrip("/"),"model":model,"backend":backend,"channel":channel_name}
        if state.get("binding",binding)!=binding:raise Failure("SETUP_BINDING_MISMATCH")
        state["binding"]=binding
        if manifest is not None:
            if state.get("model_profile",manifest)!=manifest:
                raise Failure("MODEL_PROFILE_MISMATCH","Retained profile binding differs; never overwrite.")
            state["model_profile"]=manifest
        transport=transport or Transport()
        username,password=ensure_credentials(app_dir,transport,api_base,state)
        token=login_verify(transport,api_base,username,password)
        headers={"Authorization":"Bearer "+token,"Content-Type":"application/json"}
        channel_id=ensure_channel(transport,api_base,headers,app_dir,state,channel_name,model,backend)
        # Profile mode installs the per-model channel only: strict per-order
        # policy supplies rates, so global ratios are never written here.
        changed=[] if manifest is not None else ensure_ratios(transport,api_base,headers,model)
        native_account = None
        if livepeer:
            native_account = ensure_native_account(app_dir, bundle_root, state, node_account)
        native_config_result = None
        # Retained native/node.conf enables --livepeer with no input file:
        # revalidate and reuse the retained binding without secret re-entry.
        if native_config is not None or (livepeer and state.get("native_config") is not None):
            native_config_result = ensure_native_config(app_dir, bundle_root, state, native_config)
        state.update(status="READY",channel_id=channel_id)
        save_state(app_dir,state)
        return {"ok":True,"channel_id":channel_id,"ratios_changed":changed,"model":model,"state":"READY",**({"native_account":native_account} if livepeer else {}),**({"native_config":native_config_result} if native_config is not None else {})}


def main(argv):
    ap = argparse.ArgumentParser(description="idempotent Phase 1 bundle init")
    ap.add_argument("--bundle-root", required=True)
    ap.add_argument("--api-base", required=True)
    ap.add_argument("--channel-name", default=CHANNEL_DEFAULT)
    ap.add_argument("--model", default=MODEL_DEFAULT)
    ap.add_argument("--livepeer", action="store_true")
    ap.add_argument("--node-account", default=None)
    ap.add_argument("--native-config", default=None)
    ap.add_argument("--model-profile", metavar="FILE", default=None)
    args = ap.parse_args(argv)
    try:
        result = init_bundle(args.bundle_root, args.api_base,
                             channel_name=args.channel_name, model=args.model,
                             livepeer=args.livepeer, node_account=args.node_account,
                             native_config=args.native_config,
                             model_profile=args.model_profile)
    except Failure as e:
        print(json.dumps({"ok": False, "code": e.code, "hint": e.hint}))
        return 1
    except (OSError, ValueError, KeyError, TypeError):
        print(json.dumps({"ok":False,"code":"SETUP_IO_OR_PROTOCOL_ERROR"}))
        return 1
    print(json.dumps(result))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
