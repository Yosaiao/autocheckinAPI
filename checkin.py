#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Multi-site New-API checkin / login keepalive with WeCom group bot notify."""

from __future__ import annotations

import argparse
import json
import logging
import os
import re
import sys
import urllib.error
import urllib.request
from datetime import datetime
from http.cookiejar import CookieJar
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

SCRIPT_DIR = Path(__file__).resolve().parent
DEFAULT_CONFIG = SCRIPT_DIR / "config.json"
EXAMPLE_CONFIG = SCRIPT_DIR / "config.example.json"

try:
    import requests  # type: ignore

    HAS_REQUESTS = True
except ImportError:
    HAS_REQUESTS = False

DEFAULT_PATHS = {
    "checkin_path": "/api/user/checkin",
    "login_path": "/api/user/login",
    "self_path": "/api/user/self",
    "status_path": "/api/status",
    "user_header": "New-Api-User",
}
QUOTA_UNIT = 500000
ANYROUTER_CHECKIN_PATH = "/api/user/sign_in"


def is_anyrouter_site(site: Dict[str, Any]) -> bool:
    name = str(site.get("name") or "").strip().lower()
    base = str(site.get("base_url") or "").strip().lower()
    return "anyrouter" in name or "anyrouter.top" in base


def apply_anyrouter_defaults(site: Dict[str, Any]) -> None:
    """anyrouter uses /api/user/sign_in, not classic New-API /api/user/checkin."""
    if not is_anyrouter_site(site):
        return
    path = str(site.get("checkin_path") or "").strip()
    if not path or path.rstrip("/") == "/api/user/checkin":
        site["checkin_path"] = ANYROUTER_CHECKIN_PATH


def setup_logging(verbose: bool = False) -> None:
    level = logging.DEBUG if verbose else logging.INFO
    logging.basicConfig(
        level=level,
        format="%(asctime)s [%(levelname)s] %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )


def load_raw_config(path: Path) -> Dict[str, Any]:
    if not path.is_file():
        tip = (
            "Config not found: {p}\n"
            "Copy example first:\n"
            "  cp config.example.json config.json\n"
            "Then edit sites and notify.wecom_bot.webhook."
        ).format(p=path)
        raise FileNotFoundError(tip)

    with path.open("r", encoding="utf-8") as f:
        cfg = json.load(f)
    if not isinstance(cfg, dict):
        raise ValueError("config must be a JSON object")
    return cfg


def normalize_sites(cfg: Dict[str, Any]) -> List[Dict[str, Any]]:
    global_timeout = cfg.get("timeout", 30)
    global_proxy = cfg.get("proxy", "")
    sites: List[Dict[str, Any]] = []

    if isinstance(cfg.get("sites"), list) and cfg["sites"]:
        for i, item in enumerate(cfg["sites"]):
            if not isinstance(item, dict):
                raise ValueError("sites[{0}] must be object".format(i))
            site = dict(item)
            site.setdefault("timeout", global_timeout)
            site.setdefault("proxy", global_proxy)
            for k, v in DEFAULT_PATHS.items():
                site.setdefault(k, v)
            site.setdefault("enabled", True)
            site.setdefault("mode", "checkin")
            # anyrouter-checkin style: "session" is an alias of session_cookie
            if not str(site.get("session_cookie") or "").strip() and site.get("session"):
                site["session_cookie"] = site.get("session")
            if not str(site.get("name") or "").strip():
                site["name"] = site.get("base_url") or "site-{0}".format(i + 1)
            apply_anyrouter_defaults(site)
            sites.append(site)
    else:
        site = {
            k: cfg.get(k, DEFAULT_PATHS.get(k, ""))
            for k in (
                "base_url",
                "username",
                "password",
                "user_id",
                "session_cookie",
                "access_token",
                "timeout",
                "proxy",
                *DEFAULT_PATHS.keys(),
            )
        }
        site["timeout"] = cfg.get("timeout", 30)
        site["proxy"] = cfg.get("proxy", "")
        site["name"] = cfg.get("name") or site.get("base_url") or "default"
        site["mode"] = cfg.get("mode") or "checkin"
        site["enabled"] = cfg.get("enabled", True)
        for k, v in DEFAULT_PATHS.items():
            site.setdefault(k, v)
        if not str(site.get("session_cookie") or "").strip() and site.get("session"):
            site["session_cookie"] = site.get("session")
        apply_anyrouter_defaults(site)
        sites.append(site)

    if len(sites) == 1:
        env_map = {
            "base_url": "CHECKIN_BASE_URL",
            "username": "CHECKIN_USERNAME",
            "password": "CHECKIN_PASSWORD",
            "user_id": "CHECKIN_USER_ID",
            "session_cookie": "CHECKIN_SESSION_COOKIE",
            "access_token": "CHECKIN_ACCESS_TOKEN",
            "proxy": "CHECKIN_PROXY",
        }
        for key, env_name in env_map.items():
            val = os.environ.get(env_name)
            if val:
                sites[0][key] = val
    return sites


def normalize_notify(cfg: Dict[str, Any]) -> Dict[str, Any]:
    notify = cfg.get("notify")
    if not isinstance(notify, dict):
        notify = {}

    # Prefer group robot webhook; keep legacy keys for migration.
    bot = None
    for key in ("wecom_bot", "wecom_robot", "wecom", "wework"):
        val = notify.get(key)
        if isinstance(val, dict):
            bot = val
            break
    if bot is None:
        bot = {}

    webhook = str(
        os.environ.get("WECOM_BOT_WEBHOOK")
        or os.environ.get("WECOM_WEBHOOK")
        or bot.get("webhook")
        or bot.get("webhook_url")
        or bot.get("url")
        or ""
    ).strip()

    enabled = bot.get("enabled")
    if enabled is None:
        enabled = bool(webhook)
    else:
        enabled = bool(enabled)

    when = str(bot.get("when") or "always").strip().lower()
    if when not in ("always", "on_failure", "never"):
        when = "always"

    msgtype = str(bot.get("msgtype") or "markdown").strip().lower()
    if msgtype not in ("markdown", "text"):
        msgtype = "markdown"

    return {
        "wecom_bot": {
            "enabled": enabled,
            "webhook": webhook,
            "when": when,
            "msgtype": msgtype,
        }
    }


def join_url(base: str, path: str) -> str:
    base = (base or "").rstrip("/")
    if not path:
        return base
    if not path.startswith("/"):
        path = "/" + path
    return base + path


def parse_session_cookie(raw: str) -> Dict[str, str]:
    raw = (raw or "").strip()
    if not raw:
        return {}
    cookies: Dict[str, str] = {}
    if "=" not in raw and ";" not in raw:
        cookies["session"] = raw
        return cookies
    for part in raw.split(";"):
        part = part.strip()
        if not part or "=" not in part:
            continue
        k, v = part.split("=", 1)
        cookies[k.strip()] = v.strip()
    return cookies


def looks_like_html(data: Any) -> bool:
    if not isinstance(data, str):
        return False
    s = data.lstrip()[:200].lower()
    return s.startswith("<!doctype html") or s.startswith("<html") or "<script" in s[:500]


def is_waf_challenge(data: Any) -> bool:
    if not isinstance(data, str):
        return False
    low = data.lower()
    if "<script>" in low and "arg1" in data:
        return True
    return (
        "acw_sc__v2" in low
        or "var arg1=" in data
        or "arg1='" in data
        or 'arg1="' in data
        or "aliyun_waf" in low
    )


def solve_acw_sc_v2(html: str) -> Optional[str]:
    if not html or "arg1" not in html:
        return None
    m = re.search(r"arg1\s*=\s*['\"]([0-9A-Fa-f]+)['\"]", html)
    if not m:
        return None
    arg1 = m.group(1)
    pos = [
        0xF, 0x23, 0x1D, 0x18, 0x21, 0x10, 0x1, 0x26, 0xA, 0x9,
        0x13, 0x1F, 0x28, 0x1B, 0x16, 0x17, 0x19, 0xD, 0x6, 0xB,
        0x27, 0x12, 0x14, 0x8, 0xE, 0x15, 0x20, 0x1A, 0x2, 0x1E,
        0x7, 0x4, 0x11, 0x5, 0x3, 0x1C, 0x22, 0x25, 0xC, 0x24,
    ]
    mask = "3000176000856006061501533003690027800375"
    chars: List[str] = [""] * len(pos)
    for i, ch in enumerate(arg1):
        for j, p in enumerate(pos):
            if p == i + 1:
                chars[j] = ch
    u = "".join(chars)
    out_parts: List[str] = []
    for i in range(0, min(len(u), len(mask)), 2):
        a = int(u[i : i + 2], 16) ^ int(mask[i : i + 2], 16)
        hx = format(a, "x")
        if len(hx) == 1:
            hx = "0" + hx
        out_parts.append(hx)
    return "".join(out_parts)


def summarize_body(data: Any, limit: int = 160) -> str:
    if data is None:
        return ""
    if is_waf_challenge(data):
        return "WAF challenge page (HTML, not API JSON)"
    if looks_like_html(data):
        text = re.sub(r"\s+", " ", str(data))[:limit]
        return "Non-JSON HTML response: {0}...".format(text)
    if isinstance(data, dict):
        for key in ("message", "msg", "error", "detail"):
            if data.get(key):
                return str(data[key])
        return json.dumps(data, ensure_ascii=False)[:limit]
    s = str(data).replace("\n", " ").strip()
    if len(s) > limit:
        return s[:limit] + "..."
    return s


def to_number(value: Any) -> Optional[float]:
    if value is None or value == "":
        return None
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    try:
        return float(str(value).strip().replace(",", ""))
    except (TypeError, ValueError):
        return None


def format_quota(quota: Any, used_quota: Any = None, unit: int = QUOTA_UNIT) -> str:
    q = to_number(quota)
    if q is None:
        return "未知"
    if unit and unit > 0:
        return "${0:.2f}".format(q / unit)
    return str(int(q) if q == int(q) else q)


def extract_user_payload(data: Any) -> Dict[str, Any]:
    if not isinstance(data, dict):
        return {}
    payload = data.get("data") if isinstance(data.get("data"), dict) else data
    return payload if isinstance(payload, dict) else {}


def extract_quota_fields(data: Any) -> Dict[str, Any]:
    payload = extract_user_payload(data)
    if not payload:
        return {"username": "", "quota": None, "used_quota": None, "quota_text": "未知"}
    username = payload.get("username") or payload.get("display_name") or payload.get("name") or ""
    quota = None
    used = None
    for key in ("quota", "remain_quota", "remaining_quota", "balance", "credit"):
        if key in payload and payload[key] is not None:
            quota = payload[key]
            break
    for key in ("used_quota", "used", "usage"):
        if key in payload and payload[key] is not None:
            used = payload[key]
            break
    return {
        "username": str(username) if username is not None else "",
        "quota": quota,
        "used_quota": used,
        "quota_text": format_quota(quota, used),
    }


class HttpClient:
    def __init__(
        self,
        timeout: float = 30,
        proxy: str = "",
        default_headers: Optional[Dict[str, str]] = None,
    ) -> None:
        self.timeout = timeout
        self.proxy = (proxy or "").strip()
        self.default_headers = {
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/126.0.0.0 Safari/537.36"
            ),
            "Accept": "application/json, text/plain, */*",
            "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
            "Content-Type": "application/json",
        }
        if default_headers:
            self.default_headers.update(default_headers)
        self._session_cookies: Dict[str, str] = {}

        if HAS_REQUESTS:
            self._req = requests.Session()
            self._req.headers.update(self.default_headers)
            if self.proxy:
                self._req.proxies.update({"http": self.proxy, "https": self.proxy})
        else:
            self._req = None
            self._cj = CookieJar()
            handlers = [urllib.request.HTTPCookieProcessor(self._cj)]
            if self.proxy:
                handlers.append(
                    urllib.request.ProxyHandler({"http": self.proxy, "https": self.proxy})
                )
            self._opener = urllib.request.build_opener(*handlers)

    def set_cookies(self, cookies: Dict[str, str]) -> None:
        self._session_cookies.update(cookies)
        if HAS_REQUESTS and self._req is not None:
            self._req.cookies.update(cookies)

    def set_header(self, key: str, value: str) -> None:
        if value is None:
            return
        self.default_headers[key] = value
        if HAS_REQUESTS and self._req is not None:
            self._req.headers[key] = value

    def request(
        self,
        method: str,
        url: str,
        json_body: Optional[Dict[str, Any]] = None,
        headers: Optional[Dict[str, str]] = None,
    ) -> Tuple[int, Dict[str, str], Any]:
        method = method.upper()
        hdrs = dict(self.default_headers)
        if headers:
            hdrs.update(headers)
        body_bytes: Optional[bytes] = None
        if json_body is not None:
            body_bytes = json.dumps(json_body, ensure_ascii=False).encode("utf-8")

        if HAS_REQUESTS and self._req is not None:
            resp = self._req.request(
                method,
                url,
                json=json_body,
                headers=headers or {},
                timeout=self.timeout,
            )
            try:
                self._session_cookies.update(self._req.cookies.get_dict())
            except Exception:
                pass
            try:
                data = resp.json()
            except Exception:
                data = resp.text
            return resp.status_code, dict(resp.headers), data

        req = urllib.request.Request(url, data=body_bytes, headers=hdrs, method=method)
        if self._session_cookies:
            cookie_header = "; ".join("{0}={1}".format(k, v) for k, v in self._session_cookies.items())
            existing = req.get_header("Cookie") or ""
            if existing:
                cookie_header = existing + "; " + cookie_header
            req.add_header("Cookie", cookie_header)

        try:
            with self._opener.open(req, timeout=self.timeout) as resp:
                raw = resp.read()
                status = getattr(resp, "status", 200) or 200
                resp_headers = {k: v for k, v in resp.headers.items()}
                try:
                    scs = resp.headers.get_all("Set-Cookie")  # type: ignore[attr-defined]
                    if scs:
                        for sc in scs:
                            nv = sc.split(";", 1)[0]
                            if "=" in nv:
                                n, v = nv.split("=", 1)
                                self.set_cookies({n.strip(): v.strip()})
                except Exception:
                    pass
        except urllib.error.HTTPError as e:
            raw = e.read() if e.fp else b""
            status = e.code
            resp_headers = {k: v for k, v in (e.headers.items() if e.headers else [])}

        text = raw.decode("utf-8", errors="replace") if raw else ""
        try:
            data = json.loads(text) if text else None
        except json.JSONDecodeError:
            data = text
        return status, resp_headers, data


def extract_user_id(data: Any) -> str:
    if not isinstance(data, dict):
        return ""
    payload = data.get("data") if "data" in data else data
    if isinstance(payload, dict):
        for key in ("id", "user_id", "userId", "UserId"):
            if key in payload and payload[key] is not None:
                return str(payload[key])
    for key in ("id", "user_id", "userId"):
        if key in data and data[key] is not None:
            return str(data[key])
    return ""


def is_success_payload(data: Any) -> Optional[bool]:
    if not isinstance(data, dict):
        return None
    if "success" in data:
        return bool(data["success"])
    if "code" in data:
        code = data["code"]
        if code in (0, 200, "0", "200", "success"):
            return True
        if code in (1, -1, 400, 401, 403, 500):
            return False
    return None


def message_of(data: Any) -> str:
    if isinstance(data, dict):
        for key in ("message", "msg", "error", "detail"):
            if data.get(key):
                return str(data[key])
        # Do not dump whole JSON object as message.
        return ""
    if data is None:
        return ""
    if is_waf_challenge(data) or looks_like_html(data):
        return summarize_body(data)
    s = str(data)
    if len(s) > 200:
        return s[:200] + "..."
    return s


def friendly_result_msg(mode: str, ok: bool, msg: str) -> str:
    raw = (msg or "").strip()
    if already_checked_in(raw):
        return "今日已签到"
    low = raw.lower()
    if ok:
        if mode in ("login_only", "login", "keepalive", "self"):
            return "登录成功"
        if raw in ("checkin ok", "session ok", "keepalive ok"):
            return "签到成功" if mode == "checkin" else "登录成功"
        if "签到" in raw or "checkin" in low or "check-in" in low:
            return raw if ("今日" in raw or "已" in raw or "成功" in raw) else "签到成功"
        return raw or ("签到成功" if mode == "checkin" else "登录成功")
    return raw or ("签到失败" if mode == "checkin" else "登录失败")


def already_checked_in(msg: str) -> bool:
    raw = msg or ""
    text = raw.lower()
    keywords = (
        "already",
        "checked in",
        "check-in already",
        "already checked",
        "已经签到",
        "已签到",
        "重复签到",
        "签到过",
        "今日已",
        "今天已经",
    )
    for k in keywords:
        if not k:
            continue
        if k.isascii():
            if k.lower() in text:
                return True
        elif k in raw:
            return True
    return False


def empty_result(name: str, ok: bool, msg: str) -> Dict[str, Any]:
    return {
        "name": name,
        "ok": ok,
        "msg": msg,
        "username": "",
        "quota": None,
        "used_quota": None,
        "quota_text": "未知",
    }


class SiteClient:
    def __init__(self, cfg: Dict[str, Any]) -> None:
        self.cfg = cfg
        self.name = str(cfg.get("name") or cfg.get("base_url") or "site")
        self.mode = str(cfg.get("mode") or "checkin").strip().lower()
        self.base_url = (cfg.get("base_url") or "").rstrip("/")
        if not self.base_url:
            raise ValueError("[{0}] missing base_url".format(self.name))
        timeout = float(cfg.get("timeout") or 30)
        proxy = str(cfg.get("proxy") or "")
        self.user_header = str(cfg.get("user_header") or "New-Api-User")
        self.http = HttpClient(timeout=timeout, proxy=proxy)
        self.user_id = str(cfg.get("user_id") or "").strip()
        unit = cfg.get("quota_unit", QUOTA_UNIT)
        try:
            self.quota_unit = int(unit) if unit not in (None, "") else QUOTA_UNIT
        except (TypeError, ValueError):
            self.quota_unit = QUOTA_UNIT

    def login(self) -> None:
        username = str(self.cfg.get("username") or "").strip()
        password = str(self.cfg.get("password") or "")
        if not username or not password:
            raise ValueError("username and password required for login")
        url = join_url(self.base_url, str(self.cfg.get("login_path") or "/api/user/login"))
        logging.info("[%s] login as %s", self.name, username)
        status, _, data = self.request_json(
            "POST", url, json_body={"username": username, "password": password}
        )
        logging.debug("[%s] login status=%s body=%s", self.name, status, summarize_body(data))
        if is_waf_challenge(data) or looks_like_html(data):
            raise RuntimeError("login failed: {0}".format(summarize_body(data)))
        ok = is_success_payload(data)
        if status >= 400 or ok is False:
            raise RuntimeError("login failed HTTP {0}: {1}".format(status, message_of(data) or data))

        if isinstance(data, dict):
            payload = data.get("data") if isinstance(data.get("data"), dict) else data
            if isinstance(payload, dict):
                for key in ("session", "token", "access_token"):
                    if payload.get(key):
                        val = str(payload[key])
                        if key == "session":
                            self.http.set_cookies({"session": val})
                        else:
                            self.http.set_header("Authorization", "Bearer {0}".format(val))
                        break
        uid = extract_user_id(data)
        if uid:
            self.user_id = uid
        if self.user_id:
            self.http.set_header(self.user_header, self.user_id)
            logging.info("[%s] login ok, user_id=%s", self.name, self.user_id)
        else:
            logging.warning("[%s] login ok but user_id missing", self.name)

    def apply_static_auth(self) -> None:
        raw_cookie = str(
            self.cfg.get("session_cookie")
            or self.cfg.get("session")
            or ""
        )
        cookies = parse_session_cookie(raw_cookie)
        if cookies:
            self.http.set_cookies(cookies)
            logging.info("[%s] loaded cookies: %s", self.name, ", ".join(cookies.keys()))
        token = str(self.cfg.get("access_token") or "").strip()
        if token:
            if not token.lower().startswith("bearer "):
                token = "Bearer {0}".format(token)
            self.http.set_header("Authorization", token)
            logging.info("[%s] access_token set", self.name)
        if self.user_id:
            self.http.set_header(self.user_header, self.user_id)
            logging.info("[%s] %s=%s", self.name, self.user_header, self.user_id)

    def pass_waf(self, html: str, referer: str = "") -> bool:
        token = solve_acw_sc_v2(html)
        if not token:
            logging.warning("[%s] WAF page detected, cannot parse acw_sc__v2", self.name)
            return False
        self.http.set_cookies({"acw_sc__v2": token})
        logging.info("[%s] set WAF cookie acw_sc__v2, retrying", self.name)
        try:
            warm = referer or (self.base_url + "/")
            self.http.request("GET", warm)
        except Exception as e:
            logging.debug("[%s] WAF warm request failed: %s", self.name, e)
        return True

    def request_json(
        self,
        method: str,
        url: str,
        json_body: Optional[Dict[str, Any]] = None,
        retries: int = 3,
    ) -> Tuple[int, Dict[str, str], Any]:
        status, headers, data = self.http.request(method, url, json_body=json_body)
        attempt = 0
        while attempt < retries and is_waf_challenge(data):
            attempt += 1
            logging.warning("[%s] WAF challenge, auto-pass %s/%s", self.name, attempt, retries)
            if not self.pass_waf(str(data), referer=url):
                break
            status, headers, data = self.http.request(method, url, json_body=json_body)
        return status, headers, data

    def ensure_auth(self) -> None:
        cookie = str(
            self.cfg.get("session_cookie")
            or self.cfg.get("session")
            or ""
        ).strip()
        token = str(self.cfg.get("access_token") or "").strip()
        username = str(self.cfg.get("username") or "").strip()
        password = str(self.cfg.get("password") or "")
        if cookie or token:
            self.apply_static_auth()
            if cookie and not self.user_id:
                logging.info("[%s] user_id empty, try /self", self.name)
                self.fetch_self(quiet=True)
            return
        if username and password:
            self.login()
            return
        raise ValueError("need session_cookie+user_id / username+password / access_token")

    def fetch_self(self, quiet: bool = False) -> Tuple[bool, str, Any, Dict[str, Any]]:
        url = join_url(self.base_url, str(self.cfg.get("self_path") or "/api/user/self"))
        status, _, data = self.request_json("GET", url)
        if not quiet:
            logging.debug("[%s] self status=%s body=%s", self.name, status, summarize_body(data))

        if is_waf_challenge(data) or looks_like_html(data):
            info = {"username": "", "quota": None, "used_quota": None, "quota_text": "未知"}
            return False, summarize_body(data), data, info

        info = extract_quota_fields(data)
        info["quota_text"] = format_quota(info.get("quota"), info.get("used_quota"), self.quota_unit)
        msg = message_of(data)

        if status in (401, 403):
            return False, msg or "auth failed HTTP {0}".format(status), data, info
        if status >= 400:
            if not quiet:
                logging.warning("[%s] self failed HTTP %s: %s", self.name, status, msg)
            return False, msg or "self failed HTTP {0}".format(status), data, info
        if not isinstance(data, dict):
            return False, summarize_body(data) or "self response is not JSON", data, info

        uid = extract_user_id(data)
        if uid and not self.user_id:
            self.user_id = uid
            self.http.set_header(self.user_header, self.user_id)
            logging.info("[%s] got user_id from /self: %s", self.name, self.user_id)

        if not quiet:
            logging.info(
                "[%s] user=%s quota=%s",
                self.name,
                info.get("username") or self.user_id or "?",
                info.get("quota_text") or "未知",
            )

        ok_flag = is_success_payload(data)
        if ok_flag is False:
            return False, msg or "invalid user payload", data, info
        if ok_flag is None:
            payload = extract_user_payload(data)
            if not payload and not extract_user_id(data):
                return False, msg or "empty user payload", data, info
        return True, msg or "登录成功", data, info

    def checkin(self) -> Tuple[bool, str, Any]:
        path = str(self.cfg.get("checkin_path") or "/api/user/checkin")
        url = join_url(self.base_url, path)
        # anyrouter-checkin posts empty body to /api/user/sign_in;
        # classic New-API (e.g. nloln) keeps JSON {}.
        empty_body = self.cfg.get("checkin_empty_body")
        if empty_body is None:
            empty_body = path.rstrip("/").endswith("sign_in")
        body: Optional[Dict[str, Any]]
        if empty_body:
            body = None
        else:
            body = {}
        logging.info("[%s] checkin POST %s", self.name, url)
        status, _, data = self.request_json("POST", url, json_body=body)
        logging.debug("[%s] checkin status=%s body=%s", self.name, status, summarize_body(data))
        if is_waf_challenge(data) or looks_like_html(data):
            return False, summarize_body(data), data
        msg = message_of(data)
        ok_flag = is_success_payload(data)
        if status in (401, 403):
            return False, msg or "auth failed HTTP {0}".format(status), data
        if already_checked_in(msg):
            return True, msg or "今日已签到", data
        if status >= 400:
            return False, msg or "checkin failed HTTP {0}".format(status), data
        if ok_flag is False:
            if already_checked_in(msg):
                return True, msg, data
            return False, msg or "checkin failed", data
        if ok_flag is True:
            return True, msg or "签到成功", data
        if 200 <= status < 300:
            return True, msg or "签到成功", data
        return False, msg or "checkin failed HTTP {0}".format(status), data

    def run(self, self_only: bool = False, no_self: bool = False) -> Dict[str, Any]:
        logging.info("======== site: %s | mode=%s | %s ========", self.name, self.mode, self.base_url)
        result = empty_result(self.name, False, "")
        try:
            self.ensure_auth()
        except Exception as e:
            logging.error("[%s] auth failed: %s", self.name, e)
            result["msg"] = "auth failed: {0}".format(e)
            return result

        if self_only or self.mode in ("login_only", "login", "keepalive", "self"):
            try:
                ok, msg, data, info = self.fetch_self(quiet=False)
            except Exception as e:
                logging.error("[%s] keepalive failed: %s", self.name, e)
                result["msg"] = "keepalive failed: {0}".format(e)
                return result
            result.update(
                {
                    "ok": ok,
                    "msg": friendly_result_msg(self.mode, ok, msg),
                    "username": info.get("username") or "",
                    "quota": info.get("quota"),
                    "used_quota": info.get("used_quota"),
                    "quota_text": info.get("quota_text") or "未知",
                }
            )
            if ok:
                logging.info("[%s] OK — %s | 额度: %s", self.name, result["msg"], result["quota_text"])
            else:
                logging.error("[%s] FAIL — %s | 额度: %s", self.name, result["msg"], result["quota_text"])
            return result

        try:
            ok, msg, data = self.checkin()
        except Exception as e:
            logging.error("[%s] checkin exception: %s", self.name, e)
            result["msg"] = "checkin exception: {0}".format(e)
            return result
        result["ok"] = ok
        result["msg"] = friendly_result_msg(self.mode, ok, msg)

        try:
            self_ok, self_msg, _, info = self.fetch_self(quiet=bool(no_self))
            if self_ok or info.get("quota") is not None:
                result["username"] = info.get("username") or result["username"]
                result["quota"] = info.get("quota")
                result["used_quota"] = info.get("used_quota")
                result["quota_text"] = info.get("quota_text") or "未知"
            elif not no_self:
                logging.warning("[%s] 获取额度失败: %s", self.name, self_msg)
        except Exception as e:
            logging.debug("[%s] self after checkin failed: %s", self.name, e)

        if result["ok"]:
            logging.info("[%s] OK — %s | 额度: %s", self.name, result["msg"], result.get("quota_text") or "未知")
        else:
            logging.error("[%s] FAIL — %s | 额度: %s", self.name, result["msg"], result.get("quota_text") or "未知")
        return result


def build_notify_message(results: List[Dict[str, Any]], style: str = "markdown") -> Tuple[str, str]:
    total = len(results)
    failed = sum(1 for r in results if not r.get("ok"))
    ok_count = total - failed
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    if failed == 0:
        title = "签到全部成功 ({0}/{1})".format(ok_count, total)
    elif ok_count == 0:
        title = "签到全部失败 ({0}/{1})".format(failed, total)
    else:
        title = "签到部分失败 ({0}成功/{1}失败)".format(ok_count, failed)

    lines: List[str] = []
    if style == "markdown":
        lines.append("### {0}".format(title))
        lines.append("> 时间：{0}".format(now))
        lines.append("")
        for r in results:
            mark = "OK" if r.get("ok") else "FAIL"
            name = str(r.get("name") or "?")
            msg = str(r.get("msg") or "")
            quota_text = str(r.get("quota_text") or "未知")
            uname = str(r.get("username") or "")
            extra = " ({0})".format(uname) if uname else ""
            lines.append("**[{0}] {1}**{2}".format(mark, name, extra))
            lines.append("结果：{0}".format(msg))
            lines.append("额度：{0}".format(quota_text))
            lines.append("")
    else:
        lines.append(title)
        lines.append("时间：{0}".format(now))
        for r in results:
            mark = "OK" if r.get("ok") else "FAIL"
            name = str(r.get("name") or "?")
            msg = str(r.get("msg") or "")
            quota_text = str(r.get("quota_text") or "未知")
            uname = str(r.get("username") or "")
            extra = " ({0})".format(uname) if uname else ""
            lines.append("[{0}] {1}{2}".format(mark, name, extra))
            lines.append("  结果：{0}".format(msg))
            lines.append("  额度：{0}".format(quota_text))
    return title, "\n".join(lines).strip() + "\n"


def send_wecom_bot(
    webhook: str,
    content: str,
    title: str = "",
    msgtype: str = "markdown",
    timeout: float = 30,
    proxy: str = "",
) -> bool:
    """Send message via WeCom group robot webhook (no app IP whitelist)."""
    webhook = (webhook or "").strip()
    if not webhook:
        logging.warning("wecom bot webhook empty")
        return False
    if "qyapi.weixin.qq.com" not in webhook and "weixin.qq.com" not in webhook:
        logging.warning("webhook looks unusual: %s", webhook[:80])

    if msgtype == "text":
        text = content
        if title and title not in content:
            text = "{0}\n{1}".format(title, content)
        body: Dict[str, Any] = {
            "msgtype": "text",
            "text": {"content": text},
        }
    else:
        # Group robot markdown content limit is relatively small; keep summary short.
        body = {
            "msgtype": "markdown",
            "markdown": {"content": content},
        }

    client = HttpClient(timeout=timeout, proxy=proxy)
    try:
        status, _, data = client.request("POST", webhook, json_body=body)
    except Exception as e:
        logging.error("wecom bot send exception: %s", e)
        return False

    logging.debug("wecom bot status=%s body=%s", status, data)
    if isinstance(data, dict) and data.get("errcode", -1) in (0, "0"):
        logging.info("wecom bot notify ok")
        return True
    # Some gateways return HTTP 200 with plain ok text
    if status == 200 and (data in (None, "", "ok") or (isinstance(data, str) and "ok" in data.lower())):
        logging.info("wecom bot notify ok")
        return True
    logging.error("wecom bot notify failed HTTP %s: %s", status, data)
    return False


def maybe_notify(
    notify_cfg: Dict[str, Any],
    results: List[Dict[str, Any]],
    timeout: float = 30,
    proxy: str = "",
) -> None:
    bot = notify_cfg.get("wecom_bot") or {}
    if not bot.get("enabled"):
        logging.debug("wecom bot notify disabled")
        return

    webhook = str(bot.get("webhook") or "").strip()
    if not webhook:
        logging.warning("wecom bot enabled but webhook empty")
        return

    when = str(bot.get("when") or "always").lower()
    failed = sum(1 for r in results if not r.get("ok"))
    if when == "never":
        return
    if when == "on_failure" and failed == 0:
        logging.info("all ok and when=on_failure, skip notify")
        return

    msgtype = str(bot.get("msgtype") or "markdown").lower()
    title, content = build_notify_message(
        results, style="markdown" if msgtype == "markdown" else "text"
    )
    send_wecom_bot(
        webhook=webhook,
        content=content,
        title=title,
        msgtype=msgtype,
        timeout=timeout,
        proxy=proxy,
    )


def main(argv: Optional[list] = None) -> int:
    parser = argparse.ArgumentParser(description="New-API multi-site checkin / keepalive")
    parser.add_argument("-c", "--config", default=str(DEFAULT_CONFIG), help="config path")
    parser.add_argument("-v", "--verbose", action="store_true", help="debug log")
    parser.add_argument("--self-only", action="store_true", help="only keepalive / self")
    parser.add_argument("--no-self", action="store_true", help="less self logs (still fetch quota)")
    parser.add_argument("--only", action="append", default=[], help="only run site name")
    parser.add_argument("--no-notify", action="store_true", help="skip wecom bot notify")
    args = parser.parse_args(argv)

    setup_logging(args.verbose)
    if not HAS_REQUESTS:
        logging.info("requests not installed, using urllib")

    try:
        raw = load_raw_config(Path(args.config))
        sites = normalize_sites(raw)
        notify_cfg = normalize_notify(raw)
    except Exception as e:
        logging.error("%s", e)
        return 2

    if not sites:
        logging.error("no sites in config")
        return 2

    only = {x.strip() for x in (args.only or []) if x and x.strip()}
    selected: List[Dict[str, Any]] = []
    for site in sites:
        if not site.get("enabled", True):
            logging.info("skip disabled site: %s", site.get("name"))
            continue
        name = str(site.get("name") or "")
        if only and name not in only and site.get("base_url") not in only:
            continue
        selected.append(site)
    if not selected:
        logging.error("no runnable sites")
        return 2

    results: List[Dict[str, Any]] = []
    for site_cfg in selected:
        name = str(site_cfg.get("name") or site_cfg.get("base_url") or "?")
        try:
            client = SiteClient(site_cfg)
            result = client.run(self_only=args.self_only, no_self=args.no_self)
        except Exception as e:
            logging.error("[%s] unhandled: %s", name, e)
            result = empty_result(name, False, str(e))
        results.append(result)

    logging.info("======== summary ========")
    failed = 0
    for r in results:
        status = "OK" if r.get("ok") else "FAIL"
        logging.info("[%s] %s — %s | 额度: %s", r.get("name"), status, r.get("msg"), r.get("quota_text") or "未知")
        if not r.get("ok"):
            failed += 1

    if not args.no_notify:
        maybe_notify(notify_cfg, results, timeout=float(raw.get("timeout") or 30), proxy=str(raw.get("proxy") or ""))

    if failed:
        logging.error("done: %s ok, %s fail", len(results) - failed, failed)
        return 1
    logging.info("done: all %s sites ok", len(results))
    return 0


if __name__ == "__main__":
    sys.exit(main())
