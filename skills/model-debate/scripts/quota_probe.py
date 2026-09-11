"""Bounded, read-only native allowance probes. Never starts an inference turn."""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import queue
import subprocess
import sys
import threading
import time
import tomllib
from datetime import datetime, timezone


class ProbeError(ValueError):
    pass


def iso(value):
    return datetime.fromtimestamp(value, timezone.utc).isoformat()


def rpc(executable, args, calls):
    path = Path(executable).resolve(strict=True)
    if os.name == "nt" and path.suffix.lower() != ".exe":
        raise ProbeError("DIRECT_EXECUTABLE_REQUIRED")
    proc = subprocess.Popen([str(path), *args], stdin=subprocess.PIPE,
                            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                            text=True, encoding="utf-8", errors="strict",
                            creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0)
    messages = queue.Queue(maxsize=128)

    def read():
        try:
            while line := proc.stdout.readline(1048577):
                if len(line) > 1048576:
                    break
                messages.put_nowait(json.loads(line))
        except (ValueError, OSError, queue.Full):
            pass

    reader = threading.Thread(target=read, daemon=True)
    reader.start()
    deadline = time.monotonic() + 20
    results = []
    try:
        for index, (method, params) in enumerate(calls, 1):
            proc.stdin.write(json.dumps({"jsonrpc": "2.0", "id": index,
                                         "method": method, "params": params}) + "\n")
            proc.stdin.flush()
            while True:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise ProbeError("PROBE_DEADLINE")
                try:
                    reply = messages.get(timeout=min(.25, remaining))
                except queue.Empty:
                    continue
                if isinstance(reply, dict) and reply.get("id") == index:
                    if "error" in reply or not isinstance(reply.get("result"), dict):
                        raise ProbeError("NATIVE_METHOD_UNAVAILABLE")
                    results.append(reply["result"])
                    break
            if method == "initialize" and "app-server" in args:
                proc.stdin.write('{"jsonrpc":"2.0","method":"initialized"}\n')
                proc.stdin.flush()
        return results
    finally:
        if proc.poll() is None:
            proc.terminate()
        try:
            proc.wait(timeout=3)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(timeout=3)
        reader.join(timeout=1)
        proc.stdin.close()
        proc.stdout.close()


def percent(name, used, reset):
    if type(used) not in (int, float) or not 0 <= used <= 100:
        raise ProbeError("INVALID_PERCENT")
    return {"name": name, "unit": "percent", "remaining": 100 - used,
            "reset_at": reset}


def codex_windows(raw, limit_id):
    buckets = raw.get("rateLimitsByLimitId")
    if buckets is not None:
        snap = buckets[limit_id]
    else:
        snap = raw["rateLimits"]
        if snap.get("limitId") != limit_id:
            raise ProbeError("LIMIT_ID_UNVERIFIED")
    if snap.get("spendControlReached") is True or snap.get("rateLimitReachedType"):
        raise ProbeError("QUOTA_EXHAUSTED")
    windows = []
    for name in ("primary", "secondary"):
        window = snap[name]
        if name == "secondary" and window is None:
            continue
        windows.append(percent(name, window["usedPercent"], iso(window["resetsAt"])))
    if snap.get("individualLimit") is not None:
        window = snap["individualLimit"]
        windows.append(percent("individual", 100 - window["remainingPercent"], iso(window["resetsAt"])))
    return windows


def grok_windows(raw):
    config = raw["config"]
    period = config["currentPeriod"]
    if period["type"] != "USAGE_PERIOD_TYPE_WEEKLY":
        raise ProbeError("UNKNOWN_PERIOD")
    if datetime.fromisoformat(period["start"].replace("Z", "+00:00")) > datetime.now(timezone.utc):
        raise ProbeError("FUTURE_PERIOD")
    return [percent("weekly", config["creditUsagePercent"], period["end"])]


_HTTP = r'''
import json,sys
from urllib.request import Request,build_opener,HTTPRedirectHandler
class NoRedirect(HTTPRedirectHandler):
 def redirect_request(self,*a,**k): raise ValueError('redirect')
try:
 key=json.load(sys.stdin)['key']
 req=Request('https://api.deepseek.com/user/balance',headers={'Authorization':'Bearer '+key})
 with build_opener(NoRedirect()).open(req,timeout=15) as response:
  raw=response.read(1048577)
  if len(raw)>1048576: raise ValueError('oversize')
  body=json.loads(raw)
  if body.get('is_available') is not True: raise ValueError('unavailable')
  balances=[b for b in body['balance_infos'] if b['currency']=='CNY']
  if len(balances)!=1: raise ValueError('currency')
  print(json.dumps({'remaining':balances[0]['total_balance']}))
except Exception:
 print(json.dumps({'error':'BALANCE_UNAVAILABLE'}))
'''


def deepseek_balance(observer):
    if observer.get("paid_access_authorized") is not True:
        raise ProbeError("EXISTING_PAID_ROUTE_NOT_AUTHORIZED")
    config = tomllib.loads(Path(observer["config_path"]).read_text(encoding="utf-8"))
    entry = config["models"][observer["model_alias"]]
    provider = config["providers"][entry["provider"]]
    if (provider["type"] != "openai" or provider["base_url"].rstrip("/") != "https://api.deepseek.com"
            or entry["model"] != observer["model"]):
        raise ProbeError("DEEPSEEK_ROUTE_MISMATCH")
    key = provider.get("api_key")
    if not isinstance(key, str) or not key or key.startswith("${"):
        raise ProbeError("CONFIGURED_KEY_UNAVAILABLE")
    response = subprocess.run([sys.executable, "-c", _HTTP], input=json.dumps({"key": key}).encode(),
                              capture_output=True, timeout=18, check=False,
                              creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0)
    value = json.loads(response.stdout)
    if response.returncode != 0 or "error" in value:
        raise ProbeError("BALANCE_UNAVAILABLE")
    return [{"name": "balance", "unit": "CNY", "remaining": value["remaining"], "reset_at": None}]


def probe(pool_id, pool):
    observer = pool["observer"]
    result = {"pool": pool_id, "status": "UNKNOWN", "windows": [],
              "observer_digest": hashlib.sha256(json.dumps(observer, sort_keys=True).encode()).hexdigest()}
    try:
        adapter = observer["adapter"]
        if adapter == "codex":
            raw = rpc(observer["executable"], ["app-server"], [
                ("initialize", {"clientInfo": {"name": "debate-quota", "version": "1"}}),
                ("account/rateLimits/read", {})])[-1]
            windows = codex_windows(raw, observer["limit_id"])
        elif adapter == "grok":
            rows = rpc(observer["executable"], ["agent", "--no-leader", "stdio"], [
                ("initialize", {"protocolVersion": 1, "clientCapabilities": {},
                                "clientInfo": {"name": "debate-quota", "version": "1"}}),
                ("_x.ai/billing", {})])
            available = rows[0].get("_meta", {}).get("modelState", {}).get("availableModels", [])
            if observer["model"] not in [m.get("modelId") for m in available]:
                raise ProbeError("MODEL_UNAVAILABLE")
            windows = grok_windows(rows[-1])
        elif adapter == "deepseek":
            windows = deepseek_balance(observer)
        else:
            raise ProbeError("UNSUPPORTED_OBSERVER")
        result.update(status="KNOWN", windows=windows)
    except ProbeError as exc:
        result["reason"] = str(exc)
    except (OSError, ValueError, KeyError, TypeError, AttributeError, OverflowError, subprocess.SubprocessError):
        result["reason"] = "PROBE_UNAVAILABLE"
    result["observed_at"] = datetime.now(timezone.utc).isoformat()
    return result
