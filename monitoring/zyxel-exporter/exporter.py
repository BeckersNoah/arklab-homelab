#!/usr/bin/env python3
"""
Zyxel 5G router exporter — encrypted-login flow
================================================
Scrapes cellular radio metrics (RSRP / RSRQ / SINR / RSSI / band / cell ID)
from a Zyxel NR-series 5G router via the JSON "DAL" API used by the stock
web UI.

Newer firmware wraps the API in an RSA-negotiated AES session:

    GET  /getRSAPublickKey            -> router RSA public key   (typo is Zyxel's)
    POST /UserLogin                   -> {"content", "iv", "key"} envelope where
                                         key = b64(RSA_PKCS1v1.5(b64(aes_key)))
                                         content = b64(AES-256-CBC(json, iv))
    ...all subsequent responses are {"iv","content"} envelopes encrypted with
    the same per-session AES key.

If /getRSAPublickKey is absent (older firmware), the plaintext flow is used.

Protocol references:
  - https://github.com/pkorpine/nr7101 (plain flow)
  - https://github.com/ThomasRinsma/vmg8825scripts (encrypted flow)
  - https://warlo.no/posts/2023-03-31-zyxel-reverse-engineering

Each Prometheus scrape is a stateless login -> fetch -> logout, so no admin
session is held open and the router web UI stays usable.

Environment:
    ROUTER_URL       default http://192.168.1.1
    ROUTER_USER      default admin
    ROUTER_PASSWORD  required (from .env — never commit)
    VERIFY_TLS       default false (router uses a self-signed cert on https)
    EXPORTER_PORT    default 9877
    ROUTER_TIMEOUT   default 8 (seconds, per HTTP call)

Semantics worth knowing: in NSA mode the INTF_* fields are the LTE anchor and
NSA_* the 5G NR leg. A persistent -128 on the NSA leg is the firmware's
"not attached" sentinel, not a weak signal; a single -128 sample is a read glitch.
"""

import base64
import json
import logging
import os
import signal
import sys
import threading
import time

import requests
import urllib3
from Crypto.Cipher import AES, PKCS1_v1_5
from Crypto.PublicKey import RSA
from Crypto.Random import get_random_bytes
from Crypto.Util.Padding import pad, unpad
from prometheus_client import start_http_server
from prometheus_client.core import REGISTRY, GaugeMetricFamily, InfoMetricFamily

# --------------------------------------------------------------------------
# Configuration
# --------------------------------------------------------------------------
ROUTER_URL = os.environ.get("ROUTER_URL", "http://192.168.1.1").rstrip("/")
ROUTER_USER = os.environ.get("ROUTER_USER", "admin")
ROUTER_PASSWORD = os.environ.get("ROUTER_PASSWORD", "")
VERIFY_TLS = os.environ.get("VERIFY_TLS", "false").lower() == "true"
EXPORTER_PORT = int(os.environ.get("EXPORTER_PORT", "9877"))
TIMEOUT = float(os.environ.get("ROUTER_TIMEOUT", "8"))

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("zyxel-exporter")

if not VERIFY_TLS:
    urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

PRIMARY_KEYS = {"INTF_RSRP": "rsrp", "INTF_RSRQ": "rsrq", "INTF_SINR": "sinr", "INTF_RSSI": "rssi"}
NSA_KEYS = {"NSA_RSRP": "rsrp", "NSA_RSRQ": "rsrq", "NSA_SINR": "sinr", "NSA_RSSI": "rssi"}

_REQ = {"timeout": TIMEOUT, "verify": VERIFY_TLS}


def _num(value):
    """Return float(value) or None if it isn't a usable number."""
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


# --------------------------------------------------------------------------
# Zyxel encrypted transport
# --------------------------------------------------------------------------
def _encrypt_request(aes_key, data):
    """Build a {"content","iv","key"} envelope for `data` (dict)."""
    iv = get_random_bytes(16)
    cipher = AES.new(aes_key, AES.MODE_CBC, iv)
    content = cipher.encrypt(pad(json.dumps(data).encode("ascii"), 16))
    return {
        "content": base64.b64encode(content).decode("ascii"),
        "iv": base64.b64encode(iv).decode("ascii"),
        "key": "",
    }


def _decrypt_response(aes_key, data):
    """Decrypt an {"iv","content"} envelope into a dict.

    Firmware sometimes answers in plaintext (e.g. errors) — then the envelope
    keys are absent and `data` is returned unchanged.
    """
    if not isinstance(data, dict) or "iv" not in data or "content" not in data:
        return data
    iv = base64.b64decode(data["iv"])[:16]
    cipher = AES.new(aes_key, AES.MODE_CBC, iv)
    plaintext = unpad(cipher.decrypt(base64.b64decode(data["content"])), 16)
    return json.loads(plaintext)


def _get_rsa_pubkey(session):
    """Return the router's RSA public key PEM, or None (older firmware)."""
    try:
        r = session.get(f"{ROUTER_URL}/getRSAPublickKey", **_REQ)
        if r.status_code == 200:
            j = r.json()
            if isinstance(j, dict) and j.get("RSAPublicKey"):
                return j["RSAPublicKey"]
    except Exception as exc:  # noqa: BLE001 — probe; any failure means "plain flow"
        log.debug("getRSAPublickKey probe failed: %s", exc)
    return None


def _login_payload():
    return {
        "Input_Account": ROUTER_USER,
        "Input_Passwd": base64.b64encode(ROUTER_PASSWORD.encode("ascii")).decode("ascii"),
        "RememberPassword": 0,
        "SHA512_password": False,
    }


def _logout(session, aes_key, sessionkey):
    """Best-effort logout; firmware variants differ on method and key casing."""
    for method, param in (("get", "sessionkey"), ("post", "sessionKey")):
        try:
            r = getattr(session, method)(f"{ROUTER_URL}/cgi-bin/UserLogout?{param}={sessionkey}", **_REQ)
            j = _decrypt_response(aes_key, r.json()) if aes_key else r.json()
            if isinstance(j, dict) and j.get("result") == "ZCFG_SUCCESS":
                return
        except Exception:  # noqa: BLE001 — best effort
            pass


def fetch_status():
    """Login, fetch cellwan_status, logout. Returns (status_dict, mode)."""
    session = requests.Session()
    session.trust_env = False
    aes_key = None
    sessionkey = None
    try:
        pubkey_pem = _get_rsa_pubkey(session)
        if pubkey_pem:
            mode = "encrypted"
            aes_key = get_random_bytes(32)
            rsa = PKCS1_v1_5.new(RSA.import_key(pubkey_pem))
            envelope = _encrypt_request(aes_key, _login_payload())
            envelope["key"] = base64.b64encode(rsa.encrypt(base64.b64encode(aes_key))).decode("ascii")
            r = session.post(f"{ROUTER_URL}/UserLogin", data=json.dumps(envelope), **_REQ)
            r.raise_for_status()
            j = _decrypt_response(aes_key, r.json())
        else:
            mode = "plain"
            r = session.post(f"{ROUTER_URL}/UserLogin", json=_login_payload(), **_REQ)
            r.raise_for_status()
            j = r.json()

        if not isinstance(j, dict) or j.get("result") != "ZCFG_SUCCESS":
            raise RuntimeError(f"login rejected ({mode}): {str(j)[:200]}")
        sessionkey = j.get("sessionkey")

        r = session.get(f"{ROUTER_URL}/cgi-bin/DAL?oid=cellwan_status", **_REQ)
        r.raise_for_status()
        j = _decrypt_response(aes_key, r.json()) if aes_key else r.json()
        if not isinstance(j, dict) or j.get("result") != "ZCFG_SUCCESS" or not j.get("Object"):
            raise RuntimeError(f"status fetch failed ({mode}): {str(j)[:200]}")
        return j["Object"][0], mode
    finally:
        if sessionkey is not None:
            _logout(session, aes_key, sessionkey)


class ZyxelCollector:
    """Custom collector — one router round-trip per Prometheus scrape."""

    def __init__(self):
        self._lock = threading.Lock()
        self._dumped_once = False
        self._mode_logged = False

    def collect(self):
        start = time.time()
        up = GaugeMetricFamily("zyxel_up", "1 if the router API scrape succeeded, 0 otherwise")
        duration = GaugeMetricFamily("zyxel_scrape_duration_seconds", "Time spent scraping the router API")

        with self._lock:
            try:
                status, mode = fetch_status()
            except Exception as exc:  # noqa: BLE001 — a failed scrape is a metric, not a crash
                log.warning("scrape failed: %s", exc)
                up.add_metric([], 0.0)
                duration.add_metric([], time.time() - start)
                yield up
                yield duration
                return

        if not self._mode_logged:
            log.info("login flow in use: %s", mode)
            self._mode_logged = True
        if not self._dumped_once:
            log.info("cellwan_status object (first scrape, for field discovery): %s", json.dumps(status, default=str))
            self._dumped_once = True

        up.add_metric([], 1.0)
        yield up

        families = {
            "rsrp": GaugeMetricFamily("zyxel_signal_rsrp_dbm", "Reference Signal Received Power (dBm) — higher (less negative) is better", labels=["radio"]),
            "rsrq": GaugeMetricFamily("zyxel_signal_rsrq_db", "Reference Signal Received Quality (dB) — higher is better", labels=["radio"]),
            "sinr": GaugeMetricFamily("zyxel_signal_sinr_db", "Signal to Interference & Noise Ratio (dB) — higher is better", labels=["radio"]),
            "rssi": GaugeMetricFamily("zyxel_signal_rssi_dbm", "Received Signal Strength Indicator (dBm)", labels=["radio"]),
        }
        for key, name in PRIMARY_KEYS.items():
            value = _num(status.get(key))
            if value is not None:
                families[name].add_metric(["primary"], value)
        for key, name in NSA_KEYS.items():
            value = _num(status.get(key))
            if value is not None:
                families[name].add_metric(["nsa"], value)
        for family in families.values():
            yield family

        cell = GaugeMetricFamily("zyxel_cell_id", "Serving cell ID", labels=["radio"])
        value = _num(status.get("INTF_Cell_ID"))
        if value is not None:
            cell.add_metric(["primary"], value)
        yield cell

        phycell = GaugeMetricFamily("zyxel_phy_cell_id", "Physical cell ID", labels=["radio"])
        for key, radio in (("INTF_PhyCell_ID", "primary"), ("NSA_PhyCellID", "nsa")):
            value = _num(status.get(key))
            if value is not None:
                phycell.add_metric([radio], value)
        yield phycell

        bandwidth = GaugeMetricFamily("zyxel_link_bandwidth_raw", "Firmware-reported bandwidth index/value (raw)", labels=["direction"])
        for key, direction in (("INTF_Uplink_Bandwidth", "uplink"), ("INTF_Downlink_Bandwidth", "downlink")):
            value = _num(status.get(key))
            if value is not None:
                bandwidth.add_metric([direction], value)
        yield bandwidth

        info = InfoMetricFamily("zyxel_cell", "Current access technology, band and network")
        info.add_metric(
            [],
            {
                "access_tech": str(status.get("INTF_Current_Access_Technology", "")),
                "band": str(status.get("INTF_Current_Band", "")),
                "nsa_band": str(status.get("NSA_Band", "")),
                "network": str(status.get("INTF_Network_In_Use", "")),
                "status": str(status.get("INTF_Status", "")),
            },
        )
        yield info

        duration.add_metric([], time.time() - start)
        yield duration


def _terminate(signum, frame):  # noqa: ARG001 — signature fixed by the signal API
    log.info("received signal %s, shutting down", signum)
    sys.exit(0)


def main():
    if not ROUTER_PASSWORD:
        log.error("ROUTER_PASSWORD is not set — refusing to start")
        sys.exit(1)

    signal.signal(signal.SIGTERM, _terminate)
    signal.signal(signal.SIGINT, _terminate)

    REGISTRY.register(ZyxelCollector())
    start_http_server(EXPORTER_PORT)
    log.info("zyxel-exporter listening on :%d, polling %s as %s", EXPORTER_PORT, ROUTER_URL, ROUTER_USER)
    while True:
        time.sleep(3600)


if __name__ == "__main__":
    main()
