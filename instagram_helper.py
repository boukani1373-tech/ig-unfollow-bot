"""
instagram_helper.py
--------------------
Wraps instagrapi to:
  - log in (with 2FA / challenge support)
  - persist a *session* per Telegram user (NOT the raw password)
  - fetch followers / following
  - compute the same categories shown in the "nofollow.app" screenshot:
        - Doesn't follow you back
        - You don't follow back
        - Mutual followers
        - Your followers / Who you follow
        - Recent unfollowed you  (needs a previous snapshot to compare against)

All state is stored locally under ./data/<telegram_user_id>/
    session.json      -> instagrapi session (safe to keep, revocable from IG settings)
    followers.json     -> last known followers snapshot (for "recent unfollowed you")
"""

import json
import os
from pathlib import Path
from typing import Optional

from instagrapi import Client
from instagrapi.exceptions import (
    TwoFactorRequired,
    ChallengeRequired,
    BadPassword,
    LoginRequired,
)

DATA_DIR = Path(__file__).parent / "data"
DATA_DIR.mkdir(exist_ok=True)


def _user_dir(tg_id: int) -> Path:
    d = DATA_DIR / str(tg_id)
    d.mkdir(parents=True, exist_ok=True)
    return d


def _session_path(tg_id: int) -> Path:
    return _user_dir(tg_id) / "session.json"


def _followers_snapshot_path(tg_id: int) -> Path:
    return _user_dir(tg_id) / "followers.json"


def get_client(tg_id: int) -> Client:
    """Return a Client, loading a saved session if one exists."""
    cl = Client()
    session_file = _session_path(tg_id)
    if session_file.exists():
        cl.load_settings(str(session_file))
    return cl


def save_session(tg_id: int, cl: Client) -> None:
    cl.dump_settings(str(_session_path(tg_id)))


def is_logged_in(tg_id: int) -> bool:
    session_file = _session_path(tg_id)
    if not session_file.exists():
        return False
    try:
        cl = get_client(tg_id)
        cl.get_timeline_feed()  # cheap call to validate session
        return True
    except Exception:
        return False


class TwoFactorNeeded(Exception):
    def __init__(self, two_factor_identifier: str):
        self.two_factor_identifier = two_factor_identifier


class ChallengeNeeded(Exception):
    """Raised when Instagram wants email/SMS verification."""
    pass


def login(tg_id: int, username: str, password: str) -> Client:
    """
    Attempt to log in. Raises TwoFactorNeeded or ChallengeNeeded if
    extra verification is required — caller should then call
    login_2fa() or handle the challenge.
    """
    cl = Client()
    try:
        cl.login(username, password)
    except TwoFactorRequired as e:
        # instagrapi stores what it needs internally on cl, but we also
        # need the identifier to complete the flow
        identifier = cl.last_json.get("two_factor_info", {}).get(
            "two_factor_identifier"
        )
        # stash the half-authenticated client so login_2fa can reuse it
        _pending_clients[tg_id] = (cl, username, password)
        raise TwoFactorNeeded(identifier) from e
    except ChallengeRequired:
        _pending_clients[tg_id] = (cl, username, password)
        raise ChallengeNeeded()
    except BadPassword:
        raise

    save_session(tg_id, cl)
    return cl


# in-memory store of half-logged-in clients while we wait for a 2FA code
_pending_clients: dict[int, tuple] = {}


def login_2fa(tg_id: int, code: str) -> Client:
    if tg_id not in _pending_clients:
        raise RuntimeError("No pending login found, /login again.")
    cl, username, password = _pending_clients.pop(tg_id)
    cl.login(username, password, verification_code=code)
    save_session(tg_id, cl)
    return cl


def logout(tg_id: int) -> None:
    session_file = _session_path(tg_id)
    if session_file.exists():
        session_file.unlink()


def fetch_followers_following(cl: Client):
    """Returns (followers: dict[username->user_id], following: dict[username->user_id])"""
    user_id = cl.user_id
    followers = cl.user_followers(user_id)   # dict[user_id] = UserShort
    following = cl.user_following(user_id)

    followers_by_name = {u.username: uid for uid, u in followers.items()}
    following_by_name = {u.username: uid for uid, u in following.items()}
    return followers_by_name, following_by_name


def compute_categories(tg_id: int, followers: dict, following: dict) -> dict:
    follower_set = set(followers.keys())
    following_set = set(following.keys())

    doesnt_follow_back = following_set - follower_set        # you follow, they don't follow you
    you_dont_follow_back = follower_set - following_set      # they follow you, you don't follow them
    mutual = follower_set & following_set

    # "Recent unfollowed you" -> compare against last saved snapshot
    snap_path = _followers_snapshot_path(tg_id)
    recent_unfollowed = set()
    if snap_path.exists():
        try:
            prev = set(json.loads(snap_path.read_text()))
            recent_unfollowed = prev - follower_set
        except Exception:
            pass

    # update snapshot for next time
    snap_path.write_text(json.dumps(sorted(follower_set)))

    return {
        "followers_count": len(follower_set),
        "following_count": len(following_set),
        "doesnt_follow_back": sorted(doesnt_follow_back),
        "you_dont_follow_back": sorted(you_dont_follow_back),
        "mutual": sorted(mutual),
        "recent_unfollowed": sorted(recent_unfollowed),
    }
