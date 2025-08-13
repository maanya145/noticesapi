# api/notices.py
"""
Vercel-compatible serverless function to fetch notices from sweedu.in,
parse them and (optionally) persist them to a local SQLite DB.

Notes:
- Store sensitive values (cookie) in environment variables on Vercel (e.g. COOKIE).
- Vercel's filesystem is ephemeral; if you need persistence across invocations,
  use an external DB (Supabase, PlanetScale, AWS RDS, etc.).
"""

import os
import json
import sqlite3
from datetime import datetime
from typing import List
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup
from flask import Flask, jsonify, request

API_URL = "https://sweedu.in/app/webparentsapp/action_layer.php"
BASE_URL = "https://sweedu.in/"
PARAMS = {
    "action": "get_stu_annoucement_notice",
    "tabname": "notice",
    "search_type": "",
    "sdate": "",
    "edate": ""
}

# Use environment variable for cookie to avoid committing secrets
COOKIE = os.environ.get("COOKIE", "")
# If you want to persist DB across invocations (not recommended on Vercel),
# set DB_PATH to an external mount. Otherwise it will use ephemeral /tmp.
DB_PATH = os.environ.get("DB_PATH", "/tmp/notices.db")

app = Flask(__name__)


def build_headers(cookie: str) -> dict:
    """Return headers for the HTTP request."""
    return {
        "Accept": "*/*",
        "Accept-Language": "en-US,en;q=0.9",
        "Connection": "close",
        "Cookie": cookie,
        "Referer": "https://sweedu.in/app/webparentsapp/announcement.php",
        "User-Agent": "Mozilla/5.0 (iPhone; CPU iPhone OS 19_0 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Mobile/15E148",
        "X-Requested-With": "XMLHttpRequest",
    }


def fetch_html(session: requests.Session, headers: dict) -> str:
    resp = session.get(API_URL, params=PARAMS, headers=headers, timeout=20)
    resp.raise_for_status()
    return resp.text


def parse_notices(html: str, base_url: str) -> List[dict]:
    soup = BeautifulSoup(html, "lxml")
    notice_cards = soup.find_all("div", class_="post_notice")
    results = []
    for card in notice_cards:
        info_divs = card.find_all("div", class_="AdmsnTxt")
        title = info_divs[0].get_text(strip=True) if len(info_divs) > 0 else "No Title Found"
        date_text = info_divs[1].get_text(strip=True) if len(info_divs) > 1 else "No Date Found"

        description = "No Description Found"
        modal_link = card.find("a", class_="modal-trigger")
        if modal_link and modal_link.get("href", "").startswith("#"):
            modal_id = modal_link["href"].lstrip("#")
            modal_div = soup.find("div", id=modal_id)
            if modal_div:
                desc_content = modal_div.find("div", class_="modal-content")
                if desc_content:
                    inner_div = desc_content.find("div")
                    description = inner_div.get_text(separator="\n", strip=True) if inner_div else desc_content.get_text(separator="\n", strip=True)

        download_links = []
        for box in card.find_all("div", class_="download_box"):
            link_tag = box.find("a")
            if link_tag and link_tag.get("href"):
                full_link = urljoin(base_url, link_tag["href"])
                if full_link not in download_links:
                    download_links.append(full_link)

        results.append({
            "date": date_text,
            "title": title,
            "description": description,
            "download_links": download_links,
            "fetched_at": datetime.utcnow().isoformat() + "Z"
        })
    return results


def init_db(conn: sqlite3.Connection) -> None:
    conn.execute("""
        CREATE TABLE IF NOT EXISTS notices (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            date TEXT,
            title TEXT,
            description TEXT,
            download_links_json TEXT,
            fetched_at TEXT,
            UNIQUE(date, title)
        )
    """)
    conn.commit()


def save_notices(conn: sqlite3.Connection, notices: List[dict]) -> int:
    inserted = 0
    sql = """
      INSERT OR IGNORE INTO notices (date, title, description, download_links_json, fetched_at)
      VALUES (?, ?, ?, ?, ?)
    """
    for n in notices:
        conn.execute(sql, (
            n["date"],
            n["title"],
            n["description"],
            json.dumps(n["download_links"], ensure_ascii=False),
            n["fetched_at"]
        ))
    conn.commit()
    # conn.total_changes is cumulative since connection opened; return rows in table as a simple indicator
    return conn.total_changes


@app.route("/api/notices", methods=["GET"])
def notices_handler():
    """
    GET /api/notices
    Optional query params:
      - persist=true  -> attempt to save to the SQLite DB (in /tmp by default)
    """
    cookie = os.environ.get("COOKIE", COOKIE)
    if not cookie:
        return jsonify({"error": "Missing COOKIE environment variable. Set COOKIE in Vercel dashboard."}), 400

    session = requests.Session()
    try:
        html = fetch_html(session, build_headers(cookie))
    except requests.exceptions.RequestException as e:
        return jsonify({"error": "Failed to fetch notices", "details": str(e)}), 502

    notices = parse_notices(html, BASE_URL)
    if not notices:
        return jsonify({"notices": [], "message": "No notices found (maybe an expired cookie or login required)."}), 200

    persist = request.args.get("persist", "false").lower() in ("1", "true", "yes")
    inserted = 0
    db_file = DB_PATH
    if persist:
        try:
            os.makedirs(os.path.dirname(db_file), exist_ok=True)
            conn = sqlite3.connect(db_file)
            try:
                init_db(conn)
                inserted = save_notices(conn, notices)
            finally:
                conn.close()
        except Exception as e:
            # Don't fail the entire request if DB write fails — just return a warning
            return jsonify({
                "notices": notices,
                "warning": "Failed to persist to DB",
                "db_error": str(e)
            }), 200

    return jsonify({
        "notices_count": len(notices),
        "inserted_rows_estimate": inserted,
        "notices": notices
    }), 200


# Vercel expects the Flask app to be available as `app`
# (the vercel-python builder will adapt the WSGI app)
if __name__ == "__main__":
    # Local debug
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)), debug=True)
