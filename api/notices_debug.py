# api/notices_debug.py
"""
Vercel-compatible debug endpoint for sweedu.in notices.

GET /api/notices_debug
  Optional query params:
    - method=get|post|both  (default: both)
    - snippet_chars=N       (how many chars of HTML snippet to include; default 4000)

Environment:
    COOKIE : required cookie string (do NOT commit this to git)

Response: JSON with diagnostics for each request attempt.
"""
import os
import json
from typing import List, Dict, Any
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup
from flask import Flask, jsonify, request

API_URL = "https://sweedu.in/app/webparentsapp/action_layer.php"
ANNOUNCE_PAGE = "https://sweedu.in/app/webparentsapp/announcement.php"
BASE_URL = "https://sweedu.in/"
PARAMS = {
    "action": "get_stu_annoucement_notice",
    "tabname": "notice",
    "search_type": "",
    "sdate": "",
    "edate": ""
}

app = Flask(__name__)


def build_headers(cookie: str) -> Dict[str, str]:
    return {
        "Accept": "*/*",
        "Accept-Language": "en-US,en;q=0.9",
        "Connection": "close",
        "Cookie": cookie,
        "Referer": ANNOUNCE_PAGE,
        "Origin": "https://sweedu.in",
        "User-Agent": "Mozilla/5.0 (iPhone; CPU iPhone OS 19_0 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Mobile/15E148",
        "X-Requested-With": "XMLHttpRequest",
    }


def detect_login_like(html: str) -> Dict[str, Any]:
    text_lower = html.lower()
    keywords = [
        "login", "sign in", "please login", "session expired",
        "authentication", "please sign in", "invalid session",
        "please login to continue", "user name", "password", "<form"
    ]
    matches = [kw for kw in keywords if kw in text_lower]
    return {"login_like": bool(matches), "matches": matches}


def parse_notices(html: str) -> Dict[str, Any]:
    soup = BeautifulSoup(html, "lxml")
    cards = soup.find_all("div", class_="post_notice")
    titles = []
    for card in cards:
        info = card.find_all("div", class_="AdmsnTxt")
        title = info[0].get_text(strip=True) if len(info) > 0 else None
        if title:
            titles.append(title)
    return {"count": len(cards), "titles": titles}


def cookie_names_from_jar(jar: requests.cookies.RequestsCookieJar) -> List[str]:
    return sorted({c.name for c in jar})


@app.route("/api/notices_debug", methods=["GET"])
def debug_handler():
    cookie = os.environ.get("COOKIE", "")
    if not cookie:
        return jsonify({"error": "Missing COOKIE environment variable. Set COOKIE in Vercel dashboard."}), 400

    method_param = request.args.get("method", "both").lower()
    snippet_chars = int(request.args.get("snippet_chars", 4000))
    results = []

    session = requests.Session()
    headers = build_headers(cookie)

    # 0) Prime the session by loading the announcement page first (may set server-side cookies)
    try:
        prime = session.get(ANNOUNCE_PAGE, headers=headers, timeout=15)
        prime_ok = True
    except Exception as e:
        prime_ok = False
        results.append({
            "phase": "prime_announcement_page",
            "ok": False,
            "error": str(e)
        })

    # helper to run a request and collect diagnostics
    def run_attempt(method: str) -> Dict[str, Any]:
        entry: Dict[str, Any] = {"phase": f"action_layer_{method}", "method": method}
        try:
            if method == "get":
                resp = session.get(API_URL, params=PARAMS, headers=headers, timeout=20)
            else:
                # try POST with same params (some endpoints expect POST)
                resp = session.post(API_URL, data=PARAMS, headers=headers, timeout=20)

            entry["status_code"] = resp.status_code
            entry["reason"] = resp.reason
            entry["response_length"] = len(resp.text or "")
            # cookie names currently present in the session (do not include cookie values)
            entry["session_cookie_names"] = cookie_names_from_jar(session.cookies)
            # snippet
            entry["html_snippet"] = (resp.text[:snippet_chars] + "...") if resp.text and len(resp.text) > snippet_chars else resp.text
            # detect login-like content
            entry["login_detection"] = detect_login_like(resp.text or "")
            # run parser
            parsed = parse_notices(resp.text or "")
            entry["parsed_notices"] = parsed
            # some helpful hint: if login like and no notices, tell user
            entry["hint"] = None
            if parsed["count"] == 0 and entry["login_detection"]["login_like"]:
                entry["hint"] = "Response looks like a login / session page; cookie might be insufficient for this endpoint."
            elif parsed["count"] == 0 and resp.status_code == 200:
                entry["hint"] = "200 OK but no .post_notice nodes found — server returned HTML without expected DOM structure."
            return entry
        except Exception as e:
            return {"phase": f"action_layer_{method}", "ok": False, "error": str(e)}

    # Run attempts according to method_param
    if method_param in ("get", "both"):
        results.append(run_attempt("get"))
    if method_param in ("post", "both"):
        results.append(run_attempt("post"))

    # Bundle up final diagnostics
    return jsonify({
        "prime_page_fetched": prime_ok,
        "attempts": results,
        "note": "Snippets may contain sensitive data. Do not share publicly."
    }), 200


if __name__ == "__main__":
    app.run(debug=True, port=int(os.environ.get("PORT", 5000)))
