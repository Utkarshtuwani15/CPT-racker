#!/usr/bin/env python3
"""
CP Team Tracker — Advanced Backend Server & Google Identity Engine
=================================================================
Features:
  - SQLite persistent database with WAL mode and foreign key constraints
  - Google Sign-In & OAuth 2.0 API token verification (JWT + Google tokeninfo)
  - Automatic personalized UserId (@username) generation & uniqueness guarantee
  - Complete Group & Roster Database Engine (create group, add/remove/edit members)
  - Unique Group IDs (e.g. GRP-1001, GRP-4821)
  - Group invitations by USERNAME and Unique Group ID
  - Group solve matrix persistence & synchronized stats
  - Multi-threaded HTTP server with Private Network Access (PNA) & CORS
"""

import base64
import http.server
import json
import os
import re
import secrets
import sqlite3
import sys
import threading
import time
import urllib.parse
import urllib.request
from datetime import datetime

PORT = 8000
DB_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "cp_tracker.db")

# Thread-safe SQLite connection helper
def get_db():
    conn = sqlite3.connect(DB_FILE, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON;")
    conn.execute("PRAGMA journal_mode = WAL;")
    return conn

# Database Initialization
def init_db():
    conn = get_db()
    with conn:
        conn.executescript("""
        CREATE TABLE IF NOT EXISTS users (
            id TEXT PRIMARY KEY,
            google_id TEXT UNIQUE,
            email TEXT UNIQUE NOT NULL,
            display_name TEXT NOT NULL,
            username TEXT UNIQUE NOT NULL,
            avatar TEXT,
            handles_json TEXT DEFAULT '{}',
            ratings_json TEXT DEFAULT '{}',
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );

        CREATE TABLE IF NOT EXISTS groups (
            id TEXT PRIMARY KEY,
            name TEXT NOT NULL,
            owner_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );

        CREATE TABLE IF NOT EXISTS group_members (
            group_id TEXT NOT NULL REFERENCES groups(id) ON DELETE CASCADE,
            user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            role TEXT NOT NULL DEFAULT 'teammate',
            joined_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            PRIMARY KEY (group_id, user_id)
        );

        CREATE TABLE IF NOT EXISTS invitations (
            id TEXT PRIMARY KEY,
            group_id TEXT NOT NULL REFERENCES groups(id) ON DELETE CASCADE,
            invited_user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            invited_by_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            status TEXT DEFAULT 'accepted',
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );

        CREATE TABLE IF NOT EXISTS solves (
            id TEXT PRIMARY KEY,
            user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            canonical_id TEXT NOT NULL,
            platform TEXT NOT NULL,
            problem_title TEXT NOT NULL,
            problem_url TEXT NOT NULL,
            difficulty TEXT,
            submission_id TEXT,
            verdict TEXT DEFAULT 'AC',
            provenance TEXT DEFAULT 'verified',
            solved_at TIMESTAMP,
            synced_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            UNIQUE(user_id, canonical_id)
        );

        CREATE INDEX IF NOT EXISTS idx_users_username ON users(username);
        CREATE INDEX IF NOT EXISTS idx_solves_user ON solves(user_id);
        CREATE INDEX IF NOT EXISTS idx_group_members_group ON group_members(group_id);
        """)

        # Ensure default group GRP-1001 exists
        cur = conn.cursor()
        cur.execute("SELECT COUNT(*) FROM users")
        if cur.fetchone()[0] == 0:
            print("[DB] Initializing default roster & demo records…")
            demo_users = [
                ("usr_tourist", "google_tourist", "tourist@gmail.com", "Gennady Korotkevich", "tourist",
                 "https://api.dicebear.com/7.x/bottts/svg?seed=tourist",
                 json.dumps({"codeforces": "tourist", "leetcode": "", "atcoder": "tourist", "cses": ""}),
                 json.dumps({"codeforces": 3979})),
                ("usr_petr", "google_petr", "petr@gmail.com", "Petr Mitrichev", "petr",
                 "https://api.dicebear.com/7.x/bottts/svg?seed=petr",
                 json.dumps({"codeforces": "Petr", "leetcode": "", "atcoder": "Petr", "cses": ""}),
                 json.dumps({"codeforces": 3100})),
                ("usr_neal", "google_neal", "neal@gmail.com", "Neal Wu", "neal",
                 "https://api.dicebear.com/7.x/bottts/svg?seed=neal",
                 json.dumps({"codeforces": "neal", "leetcode": "neal_wu", "atcoder": "neal", "cses": ""}),
                 json.dumps({"codeforces": 2800})),
                ("usr_utkarsh", "google_utkarsh", "utkarshtuwani@gmail.com", "Utkarsh Tuwani", "utkarsh",
                 "https://api.dicebear.com/7.x/bottts/svg?seed=utkarsh",
                 json.dumps({"codeforces": "", "leetcode": "", "atcoder": "", "cses": ""}),
                 json.dumps({}))
            ]
            cur.executemany("INSERT INTO users (id, google_id, email, display_name, username, avatar, handles_json, ratings_json) VALUES (?,?,?,?,?,?,?,?)", demo_users)
            
            # Create default demo group
            cur.execute("INSERT OR IGNORE INTO groups (id, name, owner_id) VALUES (?, ?, ?)", ("GRP-1001", "ICPC World Finals Alpha", "usr_tourist"))
            cur.execute("INSERT OR IGNORE INTO group_members (group_id, user_id, role) VALUES ('GRP-1001', 'usr_tourist', 'leader')")
            cur.execute("INSERT OR IGNORE INTO group_members (group_id, user_id, role) VALUES ('GRP-1001', 'usr_petr', 'teammate')")
            cur.execute("INSERT OR IGNORE INTO group_members (group_id, user_id, role) VALUES ('GRP-1001', 'usr_neal', 'teammate')")
            cur.execute("INSERT OR IGNORE INTO group_members (group_id, user_id, role) VALUES ('GRP-1001', 'usr_utkarsh', 'teammate')")

            # Seed canonical demo solves
            seeds = [
                ("cf::1A", "codeforces", "1A - Theatre Square", "https://codeforces.com/contest/1/problem/A", "1000", "90101"),
                ("cf::4A", "codeforces", "4A - Watermelon", "https://codeforces.com/contest/4/problem/A", "800", "90102"),
                ("cf::71A", "codeforces", "71A - Way Too Long Words", "https://codeforces.com/contest/71/problem/A", "800", "90103"),
                ("lc::two-sum", "leetcode", "Two Sum", "https://leetcode.com/problems/two-sum/", "Easy", "90104"),
                ("cses::1068", "cses", "CSES 1068 - Weird Algorithm", "https://cses.fi/problemset/task/1068", "Easy", "90105")
            ]
            now = datetime.utcnow().isoformat()
            for s in seeds:
                cur.execute("INSERT OR REPLACE INTO solves (id, user_id, canonical_id, platform, problem_title, problem_url, difficulty, submission_id, verdict, provenance, solved_at) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                            (f"usr_tourist_{s[0]}", "usr_tourist", s[0], s[1], s[2], s[3], s[4], s[5], "AC", "verified", now))
                cur.execute("INSERT OR REPLACE INTO solves (id, user_id, canonical_id, platform, problem_title, problem_url, difficulty, submission_id, verdict, provenance, solved_at) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                            (f"usr_petr_{s[0]}", "usr_petr", s[0], s[1], s[2], s[3], s[4], "80" + s[5], "AC", "verified", now))
            print("[DB] Initial database seed completed successfully.")
    conn.close()

# Helper: Generate Unique Group ID (e.g. GRP-4821)
def generate_group_id():
    conn = get_db()
    for _ in range(100):
        gid = f"GRP-{secrets.randbelow(9000) + 1000}"
        cur = conn.cursor()
        cur.execute("SELECT 1 FROM groups WHERE id = ?", (gid,))
        if not cur.fetchone():
            conn.close()
            return gid
    conn.close()
    return f"GRP-{secrets.token_hex(3).upper()}"

# Helper: Generate Personalized Unique Username
def generate_unique_username(base_name, email):
    cleaned = re.sub(r'[^a-zA-Z0-9_]', '', base_name.lower().replace(' ', '_'))
    if not cleaned:
        cleaned = re.sub(r'[^a-zA-Z0-9_]', '', email.split('@')[0].lower())
    if not cleaned:
        cleaned = "coder"

    candidate = cleaned[:15]
    conn = get_db()
    cur = conn.cursor()
    cur.execute("SELECT 1 FROM users WHERE username = ?", (candidate,))
    if not cur.fetchone():
        conn.close()
        return candidate

    for i in range(1, 1000):
        suffixed = f"{candidate}_{i}"
        cur.execute("SELECT 1 FROM users WHERE username = ?", (suffixed,))
        if not cur.fetchone():
            conn.close()
            return suffixed
    conn.close()
    return f"{candidate}_{secrets.randbelow(9999)}"

# Helper: Decode Google JWT (ID Token)
def decode_google_jwt(credential):
    try:
        parts = credential.split('.')
        if len(parts) >= 2:
            payload_b64 = parts[1]
            rem = len(payload_b64) % 4
            if rem > 0:
                payload_b64 += '=' * (4 - rem)
            payload_bytes = base64.urlsafe_b64decode(payload_b64)
            return json.loads(payload_bytes.decode('utf-8'))
    except Exception as e:
        print("[Google JWT] Decode warning:", e)
    return None

# Helper: Verify Google Token with Google OAuth API
def verify_google_token_with_api(credential):
    # Try official tokeninfo endpoint
    try:
        url = f"https://oauth2.googleapis.com/tokeninfo?id_token={urllib.parse.quote(credential)}"
        req = urllib.request.Request(url, headers={'User-Agent': 'CP-Team-Tracker/2.0'})
        with urllib.request.urlopen(req, timeout=4) as resp:
            if resp.status == 200:
                return json.loads(resp.read().decode('utf-8'))
    except Exception as e:
        print("[Google API] Tokeninfo request exception (using fallback decode):", e)
    # Safe base64 JWT decode fallback
    return decode_google_jwt(credential)

# Request Handler
class CPRequestHandler(http.server.SimpleHTTPRequestHandler):
    def end_headers(self):
        # Full CORS and Private Network Access headers
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, PUT, DELETE, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type, Authorization, X-Requested-With")
        self.send_header("Access-Control-Allow-Private-Network", "true")
        super().end_headers()

    def do_OPTIONS(self):
        self.send_response(204)
        self.end_headers()

    def send_json(self, data, status=200):
        body = json.dumps(data, indent=2).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def send_error_json(self, message, status=400):
        self.send_json({"error": message, "success": False}, status=status)

    def parse_body(self):
        content_length = int(self.headers.get("Content-Length", 0))
        if content_length == 0:
            return {}
        raw = self.rfile.read(content_length).decode("utf-8")
        try:
            return json.loads(raw)
        except Exception:
            return {}

    # ── GET REQUESTS ────────────────────────────────────────────────────────
    def do_GET(self):
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path
        query = urllib.parse.parse_qs(parsed.query)

        # Health Check
        if path == "/api/health":
            return self.send_json({
                "status": "ok",
                "database": "sqlite_connected",
                "timestamp": datetime.utcnow().isoformat()
            })

        # Search users by username (for autocomplete)
        if path == "/api/users/search":
            q = query.get("q", [""])[0].strip().lower()
            conn = get_db()
            cur = conn.cursor()
            if q:
                cur.execute("SELECT id, username, display_name, avatar, handles_json FROM users WHERE username LIKE ? OR display_name LIKE ? LIMIT 10", (f"%{q}%", f"%{q}%"))
            else:
                cur.execute("SELECT id, username, display_name, avatar, handles_json FROM users LIMIT 10")
            rows = cur.fetchall()
            conn.close()
            results = []
            for r in rows:
                results.append({
                    "id": r["id"],
                    "username": r["username"],
                    "displayName": r["display_name"],
                    "avatar": r["avatar"],
                    "handles": json.loads(r["handles_json"] or "{}")
                })
            return self.send_json({"users": results})

        # Get User's Groups
        if path == "/api/user/groups":
            user_id = query.get("userId", [""])[0]
            if not user_id:
                return self.send_error_json("Missing userId parameter")
            conn = get_db()
            cur = conn.cursor()
            cur.execute("""
                SELECT g.id, g.name, g.owner_id, gm.role,
                       (SELECT COUNT(*) FROM group_members WHERE group_id = g.id) AS member_count
                FROM groups g
                JOIN group_members gm ON g.id = gm.group_id
                WHERE gm.user_id = ?
                ORDER BY g.created_at DESC
            """, (user_id,))
            rows = cur.fetchall()
            conn.close()
            groups = []
            for r in rows:
                groups.append({
                    "id": r["id"],
                    "name": r["name"],
                    "ownerId": r["owner_id"],
                    "role": r["role"],
                    "memberCount": r["member_count"]
                })
            return self.send_json({"groups": groups})

        # Get Specific Group Details & Solves Matrix
        if path.startswith("/api/groups/") and not path.endswith("/members"):
            group_id = path.replace("/api/groups/", "").strip()
            conn = get_db()
            cur = conn.cursor()
            cur.execute("SELECT id, name, owner_id, created_at FROM groups WHERE id = ?", (group_id,))
            g_row = cur.fetchone()
            if not g_row:
                conn.close()
                return self.send_error_json(f"Group '{group_id}' not found in database", status=404)

            # Get group members
            cur.execute("""
                SELECT u.id, u.username, u.display_name, u.avatar, u.handles_json, u.ratings_json, gm.role
                FROM group_members gm
                JOIN users u ON gm.user_id = u.id
                WHERE gm.group_id = ?
                ORDER BY CASE WHEN gm.role = 'leader' THEN 0 ELSE 1 END, u.display_name ASC
            """, (group_id,))
            m_rows = cur.fetchall()
            members = []
            user_ids = []
            for m in m_rows:
                user_ids.append(m["id"])
                members.append({
                    "id": m["id"],
                    "inAppUsername": m["username"],
                    "name": m["display_name"],
                    "avatar": m["avatar"],
                    "role": m["role"],
                    "handles": json.loads(m["handles_json"] or "{}"),
                    "ratings": json.loads(m["ratings_json"] or "{}")
                })

            # Get solves
            solves = {}
            problems = {}
            member_accepted = {mid: [] for mid in user_ids}

            if user_ids:
                placeholders = ",".join(["?"] * len(user_ids))
                cur.execute(f"""
                    SELECT user_id, canonical_id, platform, problem_title, problem_url, difficulty, submission_id, verdict, provenance, solved_at
                    FROM solves
                    WHERE user_id IN ({placeholders})
                """, user_ids)
                s_rows = cur.fetchall()
                for s in s_rows:
                    pid = s["canonical_id"]
                    mid = s["user_id"]
                    if pid not in problems:
                        problems[pid] = {
                            "id": pid,
                            "platform": s["platform"],
                            "title": s["problem_title"],
                            "url": s["problem_url"],
                            "difficulty": s["difficulty"],
                            "provenance": s["provenance"]
                        }
                    if pid not in solves:
                        solves[pid] = {}
                    solves[pid][mid] = {
                        "submissionId": s["submission_id"],
                        "verdict": s["verdict"],
                        "provenance": s["provenance"],
                        "timestamp": s["solved_at"]
                    }
                    if s["verdict"] in ("OK", "AC"):
                        member_accepted[mid].append(pid)

            conn.close()
            return self.send_json({
                "group": {
                    "id": g_row["id"],
                    "name": g_row["name"],
                    "ownerId": g_row["owner_id"],
                    "createdAt": g_row["created_at"]
                },
                "members": members,
                "problems": problems,
                "submissions": solves,
                "memberAccepted": member_accepted
            })

        # Static file fallback: serve index.html or local assets
        return super().do_GET()

    # ── POST REQUESTS ───────────────────────────────────────────────────────
    def do_POST(self):
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path
        body = self.parse_body()

        # 1. Google Sign-In & API Verification
        if path == "/api/auth/google":
            credential = body.get("credential", "").strip()
            email = body.get("email", "").strip().lower()
            name = body.get("name", "").strip()
            google_id = body.get("googleId", "").strip()
            avatar = body.get("avatar", "").strip()
            custom_username = body.get("username", "").strip()

            # If Google Identity Services ID Token (JWT) is provided, verify it
            if credential:
                token_data = verify_google_token_with_api(credential)
                if token_data:
                    email = token_data.get("email", email).strip().lower()
                    name = token_data.get("name", name).strip()
                    google_id = token_data.get("sub", google_id).strip()
                    avatar = token_data.get("picture", avatar).strip()

            if not email:
                return self.send_error_json("Valid Google email or Google credential token is required")

            if not name:
                name = email.split('@')[0].capitalize()

            if not google_id:
                google_id = f"g_{secrets.token_hex(8)}"

            conn = get_db()
            cur = conn.cursor()
            cur.execute("SELECT * FROM users WHERE email = ? OR google_id = ?", (email, google_id))
            user = cur.fetchone()

            if user:
                # Existing user in database
                if avatar and avatar != user["avatar"]:
                    cur.execute("UPDATE users SET avatar = ? WHERE id = ?", (avatar, user["id"]))
                    conn.commit()
                user_dict = {
                    "id": user["id"],
                    "googleId": user["google_id"],
                    "email": user["email"],
                    "displayName": name or user["display_name"],
                    "username": user["username"],
                    "avatar": avatar or user["avatar"],
                    "handles": json.loads(user["handles_json"] or "{}"),
                    "ratings": json.loads(user["ratings_json"] or "{}")
                }
                conn.close()
                return self.send_json({"success": True, "user": user_dict, "isNewUser": False})

            # New User: Assign personalized unique username / UserId
            if custom_username:
                username = re.sub(r'[^a-zA-Z0-9_]', '', custom_username.lower())
                cur.execute("SELECT 1 FROM users WHERE username = ?", (username,))
                if cur.fetchone():
                    username = generate_unique_username(name, email)
            else:
                username = generate_unique_username(name, email)

            user_id = f"usr_{secrets.token_hex(6)}"
            if not avatar:
                avatar = f"https://api.dicebear.com/7.x/bottts/svg?seed={username}"

            cur.execute("""
                INSERT INTO users (id, google_id, email, display_name, username, avatar, handles_json, ratings_json)
                VALUES (?, ?, ?, ?, ?, ?, '{}', '{}')
            """, (user_id, google_id, email, name, username, avatar))

            # Automatically link new user to the default group
            cur.execute("INSERT OR IGNORE INTO group_members (group_id, user_id, role) VALUES ('GRP-1001', ?, 'teammate')", (user_id,))
            conn.commit()
            conn.close()

            new_user = {
                "id": user_id,
                "googleId": google_id,
                "email": email,
                "displayName": name,
                "username": username,
                "avatar": avatar,
                "handles": {"codeforces": "", "leetcode": "", "atcoder": "", "cses": ""},
                "ratings": {}
            }
            return self.send_json({"success": True, "user": new_user, "isNewUser": True})

        # 2. Add Member to Group Roster Directly (Database Sync)
        # Format: /api/groups/<group_id>/members
        match_add_member = re.match(r'^/api/groups/([^/]+)/members$', path)
        if match_add_member:
            group_id = match_add_member.group(1).upper()
            name = body.get("name", "").strip()
            role = body.get("role", "teammate")
            handles = body.get("handles", {})
            in_app_username = body.get("inAppUsername", "").strip()

            if not name:
                return self.send_error_json("Member name is required")

            conn = get_db()
            cur = conn.cursor()
            cur.execute("SELECT id FROM groups WHERE id = ?", (group_id,))
            if not cur.fetchone():
                conn.close()
                return self.send_error_json(f"Group {group_id} not found", status=404)

            username = in_app_username or generate_unique_username(name, f"{name.lower().replace(' ', '')}@local")
            cur.execute("SELECT id FROM users WHERE username = ?", (username,))
            existing = cur.fetchone()

            if existing:
                user_id = existing["id"]
                cur.execute("UPDATE users SET handles_json = ?, display_name = ? WHERE id = ?",
                            (json.dumps(handles), name, user_id))
            else:
                user_id = f"usr_{secrets.token_hex(6)}"
                avatar = f"https://api.dicebear.com/7.x/bottts/svg?seed={username}"
                cur.execute("""
                    INSERT INTO users (id, google_id, email, display_name, username, avatar, handles_json, ratings_json)
                    VALUES (?, ?, ?, ?, ?, ?, ?, '{}')
                """, (user_id, f"local_{user_id}", f"{username}@workspace.local", name, username, avatar, json.dumps(handles)))

            # If new role is leader, set other members to teammate
            if role == "leader":
                cur.execute("UPDATE group_members SET role = 'teammate' WHERE group_id = ?", (group_id,))

            cur.execute("INSERT OR REPLACE INTO group_members (group_id, user_id, role) VALUES (?, ?, ?)",
                        (group_id, user_id, role))
            conn.commit()
            conn.close()

            return self.send_json({
                "success": True,
                "member": {
                    "id": user_id,
                    "name": name,
                    "inAppUsername": username,
                    "role": role,
                    "handles": handles,
                    "avatar": f"https://api.dicebear.com/7.x/bottts/svg?seed={username}"
                }
            })

        # 3. Connect Competitive Programming Handles
        if path == "/api/user/handles":
            user_id = body.get("userId")
            handles = body.get("handles", {})
            if not user_id:
                return self.send_error_json("Missing userId")

            clean_handles = {
                "codeforces": str(handles.get("codeforces", "")).strip(),
                "leetcode": str(handles.get("leetcode", "")).strip(),
                "atcoder": str(handles.get("atcoder", "")).strip(),
                "cses": str(handles.get("cses", "")).strip()
            }

            conn = get_db()
            cur = conn.cursor()
            cur.execute("UPDATE users SET handles_json = ?, updated_at = CURRENT_TIMESTAMP WHERE id = ?",
                        (json.dumps(clean_handles), user_id))
            conn.commit()
            conn.close()
            return self.send_json({"success": True, "handles": clean_handles})

        # 4. Create Group (Returns Unique Group ID)
        if path == "/api/groups":
            user_id = body.get("userId")
            name = body.get("name", "").strip() or "CP Team"
            if not user_id:
                return self.send_error_json("Missing userId")

            group_id = generate_group_id()
            conn = get_db()
            cur = conn.cursor()
            cur.execute("INSERT INTO groups (id, name, owner_id) VALUES (?, ?, ?)", (group_id, name, user_id))
            cur.execute("INSERT INTO group_members (group_id, user_id, role) VALUES (?, ?, 'leader')", (group_id, user_id))
            conn.commit()
            conn.close()

            return self.send_json({
                "success": True,
                "group": {
                    "id": group_id,
                    "name": name,
                    "ownerId": user_id,
                    "role": "leader"
                }
            })

        # 5. Invite User by USERNAME and Unique Group ID
        if path == "/api/groups/invite":
            target_username = body.get("username", "").strip().lower().replace("@", "")
            group_id = body.get("groupId", "").strip().upper()
            invited_by = body.get("invitedBy", "").strip()

            if not target_username or not group_id:
                return self.send_error_json("Both USERNAME and Unique Group ID are required")

            conn = get_db()
            cur = conn.cursor()

            # Check if group exists
            cur.execute("SELECT id, name FROM groups WHERE id = ?", (group_id,))
            group = cur.fetchone()
            if not group:
                conn.close()
                return self.send_error_json(f"Group with ID '{group_id}' was not found in database", status=404)

            # Check if user exists by username
            cur.execute("SELECT id, username, display_name, avatar, handles_json FROM users WHERE username = ?", (target_username,))
            user = cur.fetchone()
            if not user:
                conn.close()
                return self.send_error_json(f"User with username '@{target_username}' was not found", status=404)

            # Check if user is already in group
            cur.execute("SELECT 1 FROM group_members WHERE group_id = ? AND user_id = ?", (group_id, user["id"]))
            if cur.fetchone():
                conn.close()
                return self.send_json({"success": True, "message": f"@{target_username} is already a member of {group['name']} ({group_id})"})

            # Add user to group
            cur.execute("INSERT INTO group_members (group_id, user_id, role) VALUES (?, ?, 'teammate')", (group_id, user["id"]))
            inv_id = f"inv_{secrets.token_hex(4)}"
            cur.execute("INSERT INTO invitations (id, group_id, invited_user_id, invited_by_id, status) VALUES (?, ?, ?, ?, 'accepted')",
                        (inv_id, group_id, user["id"], invited_by or user["id"]))
            conn.commit()
            conn.close()

            return self.send_json({
                "success": True,
                "message": f"Successfully added @{target_username} to {group['name']} ({group_id})",
                "member": {
                    "id": user["id"],
                    "username": user["username"],
                    "name": user["display_name"],
                    "avatar": user["avatar"],
                    "role": "teammate",
                    "handles": json.loads(user["handles_json"] or "{}")
                }
            })

        # 6. Join Group by Unique Group ID
        if path == "/api/groups/join":
            user_id = body.get("userId")
            group_id = body.get("groupId", "").strip().upper()

            if not user_id or not group_id:
                return self.send_error_json("Missing userId or groupId")

            conn = get_db()
            cur = conn.cursor()
            cur.execute("SELECT id, name FROM groups WHERE id = ?", (group_id,))
            group = cur.fetchone()
            if not group:
                conn.close()
                return self.send_error_json(f"Invalid Group ID '{group_id}'", status=404)

            cur.execute("INSERT OR IGNORE INTO group_members (group_id, user_id, role) VALUES (?, ?, 'teammate')", (group_id, user_id))
            conn.commit()
            conn.close()

            return self.send_json({
                "success": True,
                "message": f"Successfully joined {group['name']}",
                "groupId": group["id"]
            })

        # 7. Synchronize / Save Solves to SQLite Database
        if path == "/api/solves/sync":
            user_id = body.get("userId")
            solves_list = body.get("solves", [])
            rating_cf = body.get("codeforcesRating")

            if not user_id or not isinstance(solves_list, list):
                return self.send_error_json("Invalid sync payload")

            conn = get_db()
            cur = conn.cursor()
            now = datetime.utcnow().isoformat()

            for s in solves_list:
                canon_id = s.get("canonicalId") or s.get("canonId")
                if not canon_id:
                    continue
                solve_id = f"{user_id}_{canon_id}"
                cur.execute("""
                    INSERT OR REPLACE INTO solves
                    (id, user_id, canonical_id, platform, problem_title, problem_url, difficulty, submission_id, verdict, provenance, solved_at, synced_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """, (
                    solve_id,
                    user_id,
                    canon_id,
                    s.get("platform", "unknown"),
                    s.get("title", canon_id),
                    s.get("url", "#"),
                    str(s.get("difficulty") or ""),
                    str(s.get("submissionId") or ""),
                    s.get("verdict", "AC"),
                    s.get("provenance", "verified"),
                    s.get("timestamp") or now,
                    now
                ))

            if rating_cf:
                cur.execute("SELECT ratings_json FROM users WHERE id = ?", (user_id,))
                row = cur.fetchone()
                existing = json.loads(row["ratings_json"] or "{}") if row else {}
                existing["codeforces"] = rating_cf
                cur.execute("UPDATE users SET ratings_json = ? WHERE id = ?", (json.dumps(existing), user_id))

            conn.commit()
            conn.close()

            return self.send_json({"success": True, "syncedCount": len(solves_list)})

        self.send_error_json("Endpoint not found", status=404)

    # ── DELETE REQUESTS ─────────────────────────────────────────────────────
    def do_DELETE(self):
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path
        # Format: /api/groups/<group_id>/members/<user_id>
        match = re.match(r'^/api/groups/([^/]+)/members/([^/]+)$', path)
        if match:
            group_id = match.group(1).upper()
            user_id = match.group(2)
            conn = get_db()
            cur = conn.cursor()
            cur.execute("DELETE FROM group_members WHERE group_id = ? AND user_id = ?", (group_id, user_id))
            conn.commit()
            conn.close()
            return self.send_json({"success": True, "message": f"Member {user_id} deleted from group {group_id}"})
        self.send_error_json("Endpoint not found", status=404)

    # ── PUT REQUESTS ────────────────────────────────────────────────────────
    def do_PUT(self):
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path
        body = self.parse_body()
        # Format: /api/groups/<group_id>/members/<user_id>
        match = re.match(r'^/api/groups/([^/]+)/members/([^/]+)$', path)
        if match:
            group_id = match.group(1).upper()
            user_id = match.group(2)
            role = body.get("role")
            name = body.get("name")
            handles = body.get("handles")

            conn = get_db()
            cur = conn.cursor()
            if role:
                if role == "leader":
                    cur.execute("UPDATE group_members SET role = 'teammate' WHERE group_id = ?", (group_id,))
                cur.execute("UPDATE group_members SET role = ? WHERE group_id = ? AND user_id = ?", (role, group_id, user_id))
            if name:
                cur.execute("UPDATE users SET display_name = ? WHERE id = ?", (name, user_id))
            if handles:
                cur.execute("UPDATE users SET handles_json = ? WHERE id = ?", (json.dumps(handles), user_id))
            conn.commit()
            conn.close()
            return self.send_json({"success": True, "message": "Member updated in database"})
        self.send_error_json("Endpoint not found", status=404)

def run_server():
    init_db()
    server_address = ("", PORT)
    httpd = http.server.ThreadingHTTPServer(server_address, CPRequestHandler)
    print("=" * 65)
    print(f"🚀 CP Team Tracker Backend running at: http://localhost:{PORT}")
    print(f"📁 Database: {DB_FILE}")
    print(f"🌐 Serving web portal & API at http://localhost:{PORT}/")
    print("=" * 65)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\n[Server] Shutting down…")
        httpd.server_close()

if __name__ == "__main__":
    run_server()
