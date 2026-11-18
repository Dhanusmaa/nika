import asyncio
import json
import os
import time
import random
import re
import pycountry
import statistics
import math
import hashlib
import hmac
import unicodedata
from telethon import TelegramClient, events, Button, functions, types
from telethon.sessions import StringSession
from telethon.errors import FloodWaitError, AuthKeyDuplicatedError
from pymongo import MongoClient, DESCENDING
from datetime import datetime, timezone
from zoneinfo import ZoneInfo
from bestpick_engine import pick as bestpick_pick, guess_best_response
from urllib.parse import urlsplit, parse_qsl
from urllib.request import Request, urlopen
from urllib.error import HTTPError, URLError

def env_int(name, default=0):
    value = os.getenv(name, "").strip()
    return int(value) if value else default

API_ID = env_int("API_ID")
API_HASH = os.getenv("API_HASH", "").strip()
BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
TELETHON_SESSION = os.getenv("TELETHON_SESSION", "").strip()
SOURCE_CHAT_ID = env_int("SOURCE_CHAT_ID", -1003929450754)
SOURCE_CHAT_IDS = {
    int(value.strip())
    for value in os.getenv("SOURCE_CHAT_IDS", "-1003929450754,-1003707950453").split(",")
    if value.strip()
}
SOURCE_CHAT_IDS.add(SOURCE_CHAT_ID)
SOURCE_USERNAME = os.getenv("SOURCE_USERNAME", "MazeMoney").strip().lstrip("@")
SOURCE_USERNAMES = {-1003929450754: "MazeMoney", -1003707950453: "mazes"}
POT_SENDER_ID = env_int("POT_SENDER_ID", 8790267038)
GAME_TRUSTED_SENDER_IDS = {
    int(value.strip())
    for value in os.getenv("GAME_TRUSTED_SENDER_IDS", "8359388397,8790267038").split(",")
    if value.strip()
}
RAIN_SENDER_ID = env_int("RAIN_SENDER_ID", 8966963895)
RAIN_ACTIVITY_CHAT_ID = env_int("RAIN_ACTIVITY_CHAT_ID", -1003707950453)  # @mazes
RAIN_ACTIVITY_PROMPT_DELAY = 15 * 60
RAIN_INACTIVITY_SECONDS = 15 * 60
DEFAULT_RAIN_STICKER_PACK = "kang_pack_5250696460_static_by_afckangbot"
RAIN_STICKER_PACK = DEFAULT_RAIN_STICKER_PACK
GAME_REPLY_STICKER_NUMBER = 1  # One-based sticker number selected for reply reactions.
RAIN_STICKER_DELAY_MIN = 5
RAIN_STICKER_DELAY_MAX = 10
_rain_sticker_set_cache = None
rain_last_owner_activity_at = time.monotonic()
rain_inactivity_activity_task = None
OWNER_ID = env_int("OWNER_ID")
MONGODB_URI = os.getenv("MONGODB_URI", "").strip()
WEBAPP_URL = os.getenv("WEBAPP_URL", "").strip()
PROCESS_STARTED_AT = time.monotonic()

mongo_client = None
mongo_collection = None
state_collection = None
answer_history_collection = None
result_history_collection = None
prediction_history_collection = None
daily_earnings_collection = None
LOCAL_TZ = ZoneInfo('Asia/Kolkata')
approved_users = set()
hunt_enabled = False  # Effective state: manual HUNT OR scheduled HUNT.
hunt_manual_enabled = False
hunt_schedule_active = False
hunt_timer_start_minute = None
hunt_timer_end_minute = None
hunt_timer_enabled = False
hunt_schedule_armed = False


def connect_mongodb():
    global mongo_client, mongo_collection, state_collection, answer_history_collection, result_history_collection, prediction_history_collection, daily_earnings_collection

    if not MONGODB_URI:
        print("[DB] MONGODB_URI is not set.")
        return False

    try:
        if mongo_client is None or mongo_collection is None:
            mongo_client = MongoClient(
                MONGODB_URI,
                serverSelectionTimeoutMS=5000,
                connectTimeoutMS=5000,
                socketTimeoutMS=5000,
                retryWrites=True,
            )
            mongo_client.admin.command("ping")
            mongo_collection = mongo_client["mazepot"]["approved_users"]
            state_collection = mongo_client["mazepot"]["state"]
            answer_history_collection = mongo_client["mazepot"]["answer_history"]
            result_history_collection = mongo_client["mazepot"]["result_history"]
            prediction_history_collection = mongo_client["mazepot"]["prediction_history"]
            daily_earnings_collection = mongo_client["mazepot"]["daily_earnings"]
            mongo_collection.create_index("user_id", unique=True)
            state_collection.create_index("key", unique=True)
            answer_history_collection.create_index([("game", 1), ("created_at", DESCENDING)])
            result_history_collection.create_index([("game", 1), ("created_at", DESCENDING)])
            # Message IDs are scoped to their Telegram chat. Replace legacy indexes.
            for collection in (result_history_collection, prediction_history_collection):
                try:
                    collection.drop_index("game_1_source_message_id_1")
                except Exception:
                    pass
            result_history_collection.create_index([("game", 1), ("created_at", DESCENDING)])
            result_history_collection.create_index([("game", 1), ("source_chat_id", 1), ("source_message_id", 1)], unique=True)
            prediction_history_collection.create_index([("game", 1), ("created_at", DESCENDING)])
            prediction_history_collection.create_index([("game", 1), ("source_chat_id", 1), ("source_message_id", 1)], unique=True)
            daily_earnings_collection.create_index("source_message_id", unique=True)
            daily_earnings_collection.create_index([("local_date", 1), ("game", 1)])
        # Reuse the initialized MongoClient without pinging MongoDB on every handler.
        # Real database operations will surface connectivity failures when needed.
        return True
    except Exception as e:
        print(f"[DB] MongoDB connection failed: {e}")
        mongo_client = None
        mongo_collection = None
        state_collection = None
        answer_history_collection = None
        result_history_collection = None
        prediction_history_collection = None
        daily_earnings_collection = None
        return False

def save_access_request(user_id, username="", display_name=""):
    if not connect_mongodb() or state_collection is None:
        return False
    try:
        key = f"access_request_{int(user_id)}"
        state_collection.update_one({"key": key}, {"$set": {"key": key, "value": {
            "user_id": int(user_id), "username": str(username or "")[:64],
            "display_name": str(display_name or "")[:120],
            "requested_at": datetime.now(timezone.utc).isoformat(), "status": "pending",
        }}}, upsert=True)
        return True
    except Exception as e:
        print(f"[ACCESS] Request save failed for {user_id}: {e}")
        return False


def get_access_requests(limit=50):
    if not connect_mongodb() or state_collection is None:
        return []
    try:
        rows = []
        for doc in state_collection.find({"key": {"$regex": "^access_request_"}}):
            value = doc.get("value")
            if isinstance(value, dict) and value.get("status") == "pending":
                rows.append(value)
        return sorted(rows, key=lambda row: row.get("requested_at", ""), reverse=True)[:limit]
    except Exception as e:
        print(f"[ACCESS] Request list failed: {e}")
        return []


def set_access_request_status(user_id, status):
    if not connect_mongodb() or state_collection is None:
        return False
    try:
        result = state_collection.update_one(
            {"key": f"access_request_{int(user_id)}"},
            {"$set": {"value.status": status, "value.reviewed_at": datetime.now(timezone.utc).isoformat()}},
        )
        return result.matched_count > 0
    except Exception as e:
        print(f"[ACCESS] Request update failed for {user_id}: {e}")
        return False


def load_approved_users():
    if not connect_mongodb():
        print("[DB] Approved users were NOT loaded from MongoDB.")
        return

    try:
        approved_users.update(
            int(doc["user_id"])
            for doc in mongo_collection.find({}, {"_id": 0, "user_id": 1})
            if "user_id" in doc
        )
        print(f"[DB] Loaded {len(approved_users)} approved users")
    except Exception as e:
        print(f"[DB] Load failed: {e}")

def save_approved_user(user_id, username=None):
    if not connect_mongodb():
        return False

    try:
        mongo_collection.update_one(
            {"user_id": int(user_id)},
            {"$set": {"user_id": int(user_id), **({"username": username.lstrip("@").casefold()} if username else {})}},
            upsert=True,
        )

        saved = mongo_collection.find_one(
            {"user_id": int(user_id)},
            {"_id": 1, "user_id": 1},
        )

        if saved:
            print(f"[DB] Approved user saved: {user_id}")
            return True

        print(f"[DB] Save verification failed: {user_id}")
        return False
    except Exception as e:
        print(f"[DB] Save failed for {user_id}: {e}")
        return False

def delete_approved_user(user_id):
    if not connect_mongodb():
        return False

    try:
        result = mongo_collection.delete_one({"user_id": int(user_id)})
        print(f"[DB] Removed user {user_id} | deleted={result.deleted_count}")
        return result.deleted_count > 0
    except Exception as e:
        print(f"[DB] Delete failed for {user_id}: {e}")
        return False

def load_last_answer(game):
    if not connect_mongodb() or state_collection is None:
        return None

    key = f"last_{game.lower()}_answer"
    try:
        doc = state_collection.find_one({"key": key}, {"_id": 0, "value": 1})
        value = doc.get("value") if doc else None
        return int(value) if value is not None else None
    except Exception as e:
        print(f"[DB] Last {game} load failed: {e}")
        return None

def save_last_answer(game, answer):
    if not connect_mongodb() or state_collection is None or answer_history_collection is None:
        print(f"[DB] Last {game} answer could not be persisted: {answer}")
        return False

    key = f"last_{game.lower()}_answer"
    try:
        state_collection.update_one(
            {"key": key},
            {"$set": {"key": key, "value": int(answer)}},
            upsert=True,
        )
        answer_history_collection.insert_one({
            "game": game.upper(),
            "answer": int(answer),
            "created_at": datetime.now(timezone.utc),
        })
        print(f"[DB] Last {game} answer and history saved: {answer}")
        return True
    except Exception as e:
        print(f"[DB] Last {game} save failed: {e}")
        return False

def get_recent_answers(game, limit=25):
    if not connect_mongodb() or answer_history_collection is None:
        return None
    try:
        docs = answer_history_collection.find(
            {"game": game.upper()},
            {"_id": 0, "answer": 1, "created_at": 1},
        ).sort("created_at", DESCENDING).limit(limit)
        return list(docs)
    except Exception as e:
        print(f"[DB] {game} history read failed: {e}")
        return None

async def save_game_result(message):
    """Save completed results once and score only the exact preceding game round."""
    text = (getattr(message, "raw_text", None) or "").strip()
    # Match result headers robustly even if Telegram adds spacing or variation selectors.
    normalized_text = text.replace("\ufe0f", "").replace("\u200d", "").strip()
    if re.match(r"^🎯\s*It was\s+\d{1,3}\b", normalized_text):
        game = "GUESS"
    elif re.match(r"^🪙\s*It was\s+\d{1,3}\b", normalized_text):
        game = "POT"
    else:
        return False

    if not connect_mongodb() or result_history_collection is None:
        print(f"[RESULT] MongoDB unavailable; {game} result not saved (message {message.id})")
        return False

    try:
        source_chat_id = int(message.chat_id)
        existing = result_history_collection.find_one(
            scoped_query({"game": game, "source_message_id": int(message.id)}, source_chat_id), {"_id": 1}
        )
        if existing:
            return True

        winning_number = extract_result_number(text)
        if winning_number is None:
            return False

        # Fast path: live/startup tracking keeps the most recent round ID in memory.
        # Only fall back to Telegram history if the cache has no valid round.
        matching_round_id = latest_game_round_ids.get((source_chat_id, game))
        if matching_round_id is not None and int(matching_round_id) >= int(message.id):
            matching_round_id = None
        if matching_round_id is None:
            async for prior in user_client.iter_messages(
                source_chat_id, limit=5000, min_id=0, max_id=int(message.id)
            ):
                if int(prior.id) >= int(message.id):
                    continue
                prior_text = (getattr(prior, "raw_text", None) or "").strip()
                normalized_prior = prior_text.replace("\ufe0f", "").replace("\u200d", "").strip()
                if re.match(r"^[🎯🪙]\s*It was\s+\d{1,3}\b", normalized_prior):
                    continue
                if detect_event_type(prior_text) == game:
                    matching_round_id = int(prior.id)
                    break


        range_match = re.search(r"\b(\d{1,3})\s*[-–]\s*(\d{1,3})\b", text)
        result_range = None
        if range_match:
            low, high = sorted((int(range_match.group(1)), int(range_match.group(2))))
            if 1 <= low <= high <= 100:
                result_range = [low, high]
        user_won = bool(re.search(r"(?<![\w])@?lokzai(?![\w])", text, re.IGNORECASE))
        result_history_collection.insert_one({
            "game": game, "source_chat_id": source_chat_id, "source_message_id": int(message.id), "result_text": text,
            "actual": int(winning_number), "result_range": result_range,
            "user_won": user_won, "round_source_message_id": matching_round_id,
            "created_at": datetime.now(timezone.utc),
        })

        if matching_round_id is None:
            print(f"[RESULT] Saved {game} result={winning_number}; no matching round start found, left unscored.")
            return True

        resolve_prediction(game, winning_number, int(message.id), matching_round_id, source_chat_id=source_chat_id)
        print(f"[RESULT] Saved completed {game} result | source_message={message.id} | round={matching_round_id}")
        return True
    except Exception as e:
        print(f"[RESULT] Failed to save {game} result: {e}")
        record_incident(game, e, "error")
        return False

async def backfill_recent_results(limit=500):
    """Recover completed rounds independently for every configured source chat."""
    if not connect_mongodb() or result_history_collection is None:
        print("[BACKFILL] Skipped: MongoDB unavailable.")
        return
    if not user_client.is_connected():
        print("[BACKFILL] Skipped: Telegram user session is disconnected.")
        return

    try:
        recovered = 0
        rebuilt_predictions = 0
        scanned = 0
        for source_chat_id in sorted(SOURCE_CHAT_IDS):
            messages = []
            async for item in user_client.iter_messages(source_chat_id, limit=limit):
                messages.append(item)
            messages.reverse()
            latest_round = {"POT": None, "GUESS": None}
            latest_sent = {"POT": None, "GUESS": None}
            active_game = None

            for item in messages:
                scanned += 1
                raw = (getattr(item, "raw_text", None) or "").strip()
                normalized = raw.replace("\ufe0f", "").replace("\u200d", "").strip()
                event_type = detect_event_type(raw)
                record_daily_earning(item, event_type)

                if event_type in ("POT", "GUESS"):
                    active_game = event_type
                    latest_round[event_type] = int(item.id)
                    latest_sent[event_type] = None
                    latest_game_round_ids[(int(source_chat_id), event_type)] = int(item.id)
                    continue

                # Recover the owner's submitted number from their own message in
                # the same source chat. This repairs older rounds that lacked a
                # source_chat_id field in MongoDB.
                sent_match = re.fullmatch(r"(\d{1,3})", raw) if getattr(item, "out", False) else None
                if sent_match and 1 <= int(sent_match.group(1)) <= 100:
                    if active_game and latest_round[active_game] is not None and latest_sent[active_game] is None:
                        latest_sent[active_game] = int(sent_match.group(1))
                    continue

                if re.match(r"^🎯\s*It was\s+\d{1,3}\b", normalized):
                    game = "GUESS"
                elif re.match(r"^🪙\s*It was\s+\d{1,3}\b", normalized):
                    game = "POT"
                else:
                    continue

                number = extract_result_number(raw)
                if number is None:
                    continue
                existing = result_history_collection.find_one(
                    scoped_query({"game": game, "source_message_id": int(item.id)}, source_chat_id),
                    {"_id": 1},
                )
                round_id = latest_round.get(game)
                if not existing:
                    range_match = re.search(r"\b(\d{1,3})\s*[-–]\s*(\d{1,3})\b", raw)
                    result_range = None
                    if range_match:
                        low, high = sorted((int(range_match.group(1)), int(range_match.group(2))))
                        if 1 <= low <= high <= 100:
                            result_range = [low, high]
                    source_date = getattr(item, "date", None) or datetime.now(timezone.utc)
                    if source_date.tzinfo is None:
                        source_date = source_date.replace(tzinfo=timezone.utc)
                    result_history_collection.insert_one({
                        "game": game,
                        "source_chat_id": int(source_chat_id),
                        "source_message_id": int(item.id),
                        "result_text": raw,
                        "actual": int(number),
                        "result_range": result_range,
                        "user_won": bool(re.search(r"(?<![\w])@?lokzai(?![\w])", raw, re.IGNORECASE)),
                        "round_source_message_id": round_id,
                        "created_at": source_date.astimezone(timezone.utc),
                        "recovered_by_backfill": True,
                    })
                    recovered += 1

                # If an old source's prediction was never stored separately,
                # reconstruct it from the owner's actual outgoing number.
                if round_id is not None and latest_sent.get(game) is not None:
                    pred_filter = scoped_query(
                        {"game": game, "source_message_id": int(round_id)}, source_chat_id
                    )
                    if not prediction_history_collection.find_one(pred_filter, {"_id": 1}):
                        sent_number = int(latest_sent[game])
                        shadow = {}
                        prediction_history_collection.insert_one({
                            "game": game,
                            "source_chat_id": int(source_chat_id),
                            "source_message_id": int(round_id),
                            "prediction": sent_number,
                            "backups": [],
                            "method": "backfilled-owner-message",
                            "shadow_predictions": shadow,
                            "created_at": getattr(item, "date", None) or datetime.now(timezone.utc),
                            "resolved": False,
                        })
                        rebuilt_predictions += 1
                if round_id is not None:
                    resolve_prediction(game, number, int(item.id), round_id, source_chat_id=source_chat_id)
                latest_round[game] = None
                latest_sent[game] = None
                if active_game == game:
                    active_game = None

        print(
            f"[BACKFILL] Separate source scans complete | recovered_results={recovered} "
            f"| rebuilt_predictions={rebuilt_predictions} | scanned={scanned}"
        )
    except Exception as e:
        print(f"[BACKFILL] Recent result recovery failed: {e}")

def get_recent_results(game, limit=4):
    if not connect_mongodb() or result_history_collection is None:
        return None
    try:
        return list(result_history_collection.find(
            {"game": game.upper()},
            {"_id": 0, "result_text": 1, "created_at": 1, "source_message_id": 1},
        ).sort("created_at", DESCENDING).limit(limit))
    except Exception as e:
        print(f"[RESULT] {game} result history read failed: {e}")
        return None


def parse_earned_points(text):
    value = (text or "").replace(",", "")
    for pattern in (
        r"\+\s*(\d{1,7})\s*(?:points?|pts?)?\s*(?:each|per\s+winner)\b",
        r"(?:earned|won|received|grabbed|got)\D{0,35}(\d{1,7})\s*(?:points?|pts?)\b",
        r"\b(\d{1,7})\s*(?:points?|pts?)\s*(?:earned|won|received|grabbed)\b",
        r"\+\s*(\d{1,7})\s*(?:points?|pts?)\b",
    ):
        match = re.search(pattern, value, re.IGNORECASE)
        if match and 0 < int(match.group(1)) <= 1000000:
            return int(match.group(1))
    return None


def record_daily_earning(message, game=None):
    text_value = (getattr(message, "raw_text", None) or "").strip()
    if not text_value:
        return False

    normalized = text_value.casefold()
    is_owner_winner = bool(re.search(r"(?<![\w])@?lokzai(?![\w])", text_value, re.IGNORECASE))
    is_personal = bool(re.search(r"\b(you|your|me|my)\b.{0,35}\b(won|earned|received|grabbed|got)\b|\b(won|earned|received|grabbed)\b.{0,35}\b(points?|pts?)\b", normalized))
    if not (is_owner_winner or is_personal):
        return False

    points = parse_earned_points(text_value)
    if points is None:
        return False

    if game not in ("POT", "GUESS", "RAIN", "FLAG"):
        header = text_value.replace("\ufe0f", "").replace("\u200d", "").strip()
        if re.match(r"^🎯\s*It was\s+\d{1,3}\b", header):
            game = "GUESS"
        elif "flag" in normalized:
            game = "FLAG"
        elif re.match(r"^🪙\s*It was\s+\d{1,3}\b", header) or "split the pot" in normalized:
            game = "POT"
        elif "rain" in normalized or "grab" in normalized:
            game = "RAIN"
        elif "guess" in normalized:
            game = "GUESS"
        else:
            return False

    # Avoid pinging MongoDB for every ordinary source message.
    if not connect_mongodb() or daily_earnings_collection is None:
        return False

    created = getattr(message, "date", None) or datetime.now(timezone.utc)
    if created.tzinfo is None:
        created = created.replace(tzinfo=timezone.utc)
    try:
        daily_earnings_collection.update_one(
            {"source_message_id": int(message.id)},
            {"$setOnInsert": {"source_message_id": int(message.id), "game": game, "points": points,
             "local_date": created.astimezone(LOCAL_TZ).date().isoformat(),
             "created_at": created.astimezone(timezone.utc), "raw_text": text_value[:1500]}},
            upsert=True,
        )
        print(f"[EARNINGS] Captured {game} +{points} points | message={message.id}")
        return True
    except Exception as e:
        print(f"[EARNINGS] Save failed for reward message {message.id}: {e}")
        return False


def parse_user_game_rewards(text_value):
    """Parse only explicit per-user reward lines from known game result formats."""
    text = (text_value or "").replace("\ufe0f", "").replace("\u200d", "")
    rows = []
    # POT result: all listed handles receive the explicit "+X each" amount.
    if re.match(r"^🪙\s*It was\s+\d{1,3}\b", text):
        each = re.search(r"\b\d+\s+made it,\s*\+(\d+)\s+each\b", text, re.I)
        if each:
            points = int(each.group(1))
            handles = re.findall(r"@([A-Za-z0-9_]{3,})", text)
            for handle in handles:
                # Ignore named bonus recipients in trailing notes, if any.
                rows.append(("POT", handle.casefold(), points))
    # GUESS result: podium reward is the first +points after the guess number.
    elif re.match(r"^🎯\s*It was\s+\d{1,3}\b", text):
        for line in text.splitlines():
            rank = re.search(r"🥇|🥈|🥉", line)
            if not rank:
                continue
            handle = re.search(r"@([A-Za-z0-9_]{3,})", line)
            points = re.search(r"\+\s*(\d+)\b", line[rank.end():])
            if handle and points:
                rows.append(("GUESS", handle.group(1).casefold(), int(points.group(1))))
    # RAIN result: each explicit "username - N points" row.
    elif re.match(r"^🌧", text):
        for line in text.splitlines():
            match = re.search(r"^\s*[•·-]?\s*@?([A-Za-z0-9_]{3,})\s*[-·:]\s*([\d,]+)\s+points?\b", line, re.I)
            if match:
                rows.append(("RAIN", match.group(1).casefold(), int(match.group(2).replace(",", ""))))
    # FLAG format provided so far contains no points, so it is not counted.
    return rows


def get_daily_earnings(local_date=None):
    if not connect_mongodb() or daily_earnings_collection is None:
        return None
    local_date = local_date or datetime.now(LOCAL_TZ).date().isoformat()
    try:
        docs = list(daily_earnings_collection.find({"local_date": local_date}, {"_id": 0, "game": 1, "points": 1, "raw_text": 1}))
        totals = {"RAIN": 0, "POT": 0, "GUESS": 0, "FLAG": 0}
        for doc in docs:
            game = str(doc.get("game", "")).upper()
            # Reclassify previously mislabelled Flag rewards without double counting.
            raw_text = str(doc.get("raw_text", "")).casefold()
            if game == "GUESS" and "flag" in raw_text:
                game = "FLAG"
            if game in totals:
                totals[game] += int(doc.get("points", 0))
        return {"date": local_date, "totals": totals, "total": sum(totals.values()), "events": len(docs)}
    except Exception as e:
        print(f"[EARNINGS] Read failed: {e}")
        return None



def get_approved_usernames():
    """Return only usernames saved on the currently approved-user records."""
    if not connect_mongodb() or mongo_collection is None:
        return []
    try:
        return sorted({
            str(doc.get("username", "")).lstrip("@").casefold()
            for doc in mongo_collection.find(
                {"user_id": {"$in": [int(uid) for uid in approved_users]}},
                {"_id": 0, "username": 1},
            )
            if doc.get("username")
        })
    except Exception as e:
        print(f"[ACCESS] Approved username list failed: {e}")
        return []




# Verified source-channel results supplied by the owner, oldest -> newest.
# These are the baseline until live results after 2026-10-15 04:26 UTC arrive.
SEED_RESULTS = {
    # De-duplicated timeline, oldest -> newest. The first 50 of each list are the owner's
    # timestamped logs (12 Oct 17:09 UTC -> 14 Oct ~06:00 UTC); the rest is the part of the
    # older seed that came AFTER them (the two overlapped by ~20 rounds and were double-counted).
    "GUESS": [
        33, 62, 31, 17, 29, 58, 23, 92, 38, 93,
        54, 19, 52, 16, 28, 9, 11, 23, 31, 59,
        40, 96, 35, 1, 37, 3, 71, 95, 56, 36,
        22, 29, 5, 31, 83, 76, 87, 14, 5, 15,
        21, 63, 64, 5, 28, 79, 94, 77, 45, 22,
        90, 25, 48, 46, 34, 22, 20, 55, 71, 24,
        65, 56, 22, 76, 18, 23, 73, 95, 97, 46,
        62, 96, 56, 51, 27, 34, 66, 26, 83,
    ],
    "POT": [
        94, 93, 2, 23, 34, 94, 21, 50, 10, 73,
        72, 64, 75, 51, 69, 54, 63, 93, 59, 9,
        15, 45, 19, 6, 33, 95, 70, 63, 86, 11,
        70, 13, 77, 95, 21, 88, 59, 24, 4, 56,
        21, 91, 30, 57, 55, 65, 33, 81, 56, 69,
        70, 48, 54, 8, 11, 30, 27, 70, 53, 56,
        4, 18, 10, 83, 91, 29, 6, 28, 99, 55,
        72, 22, 42, 13, 90, 6, 100, 53, 19, 6,
    ],
}
SEED_CUTOFF = datetime(2026, 10, 15, 4, 26, tzinfo=timezone.utc)

def source_name(source_chat_id):
    return SOURCE_USERNAMES.get(int(source_chat_id), f"Chat {int(source_chat_id)}")


def source_scope(source_chat_id):
    source_chat_id = int(source_chat_id)
    if source_chat_id == int(SOURCE_CHAT_ID):
        return {"$or": [{"source_chat_id": source_chat_id}, {"source_chat_id": {"$exists": False}}, {"source_chat_id": None}]}
    return {"source_chat_id": source_chat_id}


def scoped_query(base, source_chat_id):
    return {"$and": [base, source_scope(source_chat_id)]}


def extract_result_number(text):
    match = re.search(r"\bIt was\s+(\d{1,3})\b", text or "", re.IGNORECASE)
    if not match:
        return None
    number = int(match.group(1))
    return number if 1 <= number <= 100 else None

def get_all_model_history(game, source_chat_id=SOURCE_CHAT_ID):
    """Return revealed outcomes oldest-to-newest for this game only.

    Predictions and pending rounds are never included. Seed results are the
    verified historical snapshot; live results after SEED_CUTOFF are appended
    in chronological order.
    """
    game = game.upper()
    values = list(SEED_RESULTS.get(game, []))
    if connect_mongodb() and result_history_collection is not None:
        try:
            docs = result_history_collection.find(
                scoped_query({"game": game, "created_at": {"$gt": SEED_CUTOFF}}, source_chat_id),
                {"_id": 0, "result_text": 1, "created_at": 1, "source_message_id": 1},
            ).sort([("created_at", 1), ("source_message_id", 1)])
            for doc in docs:
                number = extract_result_number(doc.get("result_text", ""))
                if number is not None:
                    values.append(number)
        except Exception as e:
            print(f"[MODEL] Full history read failed for {game}: {e}")
    return [int(value) for value in values if 1 <= int(value) <= 100]


def get_model_history(game, limit=15, source_chat_id=SOURCE_CHAT_ID):
    """Return the latest revealed outcomes in chronological order for one source."""
    return get_all_model_history(game, source_chat_id=source_chat_id)[-limit:]


def calculate_gate(game, outcomes=None):
    """Walk-forward test: does the expanding median beat fixed 50 historically?"""
    if outcomes is None:
        outcomes = get_all_model_history(game)
    deltas = []
    # Each historical median uses only outcomes revealed before that round.
    for index in range(1, len(outcomes)):
        prior = outcomes[:index]
        median_prior = statistics.median(prior)
        actual = outcomes[index]
        deltas.append(abs(50 - actual) - abs(median_prior - actual))

    n = len(deltas)
    if n < 2:
        return False, n, 0.0, 0.0

    mean_delta = statistics.mean(deltas)
    sd_delta = statistics.stdev(deltas)
    if sd_delta == 0:
        t_stat = float("inf") if mean_delta > 0 else (float("-inf") if mean_delta < 0 else 0.0)
    else:
        t_stat = mean_delta / (sd_delta / math.sqrt(n))

    # Require positive evidence that the historical median beats 50.
    is_open = n >= 30 and t_stat >= 2.0
    return is_open, n, mean_delta, t_stat


def get_recent_pot_ranges(limit=300, source_chat_id=SOURCE_CHAT_ID):
    """Read only completed, valid POT win ranges from live result history."""
    if not connect_mongodb() or result_history_collection is None:
        return []
    try:
        docs = list(result_history_collection.find(
            scoped_query({"game": "POT", "result_range.0": {"$gte": 1}, "result_range.1": {"$lte": 100}}, source_chat_id),
            {"_id": 0, "result_range": 1, "created_at": 1},
        ).sort("created_at", DESCENDING).limit(max(1, min(int(limit), 1000))))
        ranges = []
        for doc in reversed(docs):
            value = doc.get("result_range")
            if isinstance(value, (list, tuple)) and len(value) == 2:
                low, high = int(value[0]), int(value[1])
                if 1 <= low <= high <= 100:
                    ranges.append((low, high))
        return ranges
    except Exception as e:
        print(f"[POT] Live range history unavailable: {type(e).__name__}: {e}")
        return []


def predict_number(game, history=None, source_chat_id=SOURCE_CHAT_ID):
    """Use Best Pick, calibrating POT picks against completed live win ranges when enough data exists."""
    game = game.upper()
    if history is None:
        history = get_all_model_history(game, source_chat_id=source_chat_id)
    history = [int(value) for value in history if 1 <= int(value) <= 100]
    prediction, details = bestpick_pick(game, history=history)
    ranked = [int(n) for n in details.get("top", []) if str(n).isdigit() and 1 <= int(n) <= 100]
    calibration = ""
    if game == "POT":
        ranges = get_recent_pot_ranges(300, source_chat_id=source_chat_id)
        # Avoid trusting a tiny sample. Recent ranges are weighted more heavily,
        # but a small prior prevents one unusually wide round dominating.
        if len(ranges) >= 30:
            recent = ranges[-200:]
            total_weight = 0.0
            scores = [0.0] * 101
            for idx, (low, high) in enumerate(recent):
                weight = 0.985 ** (len(recent) - idx - 1)
                total_weight += weight
                for candidate in range(low, high + 1):
                    scores[candidate] += weight
            rates = {n: scores[n] / total_weight for n in range(1, 101)}
            # Blend observed live coverage with the base model ranking to reduce
            # overfitting to a small or unusually clustered archive.
            model_order = ranked + [n for n in range(1, 101) if n not in ranked]
            model_rank = {n: i for i, n in enumerate(model_order)}
            prediction = max(range(1, 101), key=lambda n: (rates[n], -model_rank.get(n, 999)))
            ranked = sorted(range(1, 101), key=lambda n: (rates[n], -model_rank.get(n, 999)), reverse=True)[:10]
            calibration = f" | live_ranges={len(ranges)} | recent_range_coverage={rates[prediction]:.1%}"
        else:
            calibration = f" | live_range_calibration=waiting ({len(ranges)}/30 valid rounds)"
    if game == "POT" and connect_mongodb() and state_collection is not None:
        # Persist recent POT picks per source chat so restarts cannot reset the no-repeat guard.
        recent_key = f"recent_pot_predictions_{int(source_chat_id)}"
        try:
            recent_doc = state_collection.find_one({"key": recent_key}, {"_id": 0, "values": 1}) or {}
            recent_picks = [int(n) for n in recent_doc.get("values", []) if 1 <= int(n) <= 100][-10:]
            if prediction in recent_picks:
                for _ in range(30):
                    candidate, candidate_details = bestpick_pick(game, history=history)
                    if candidate not in recent_picks:
                        prediction, details = candidate, candidate_details
                        break
                else:
                    available = [n for n in range(1, 101) if n not in recent_picks]
                    prediction = random.choice(available)
            recent_picks = (recent_picks + [int(prediction)])[-10:]
            state_collection.update_one({"key": recent_key}, {"$set": {"key": recent_key, "values": recent_picks}}, upsert=True)
        except Exception as e:
            print(f"[POT] Persistent no-repeat guard unavailable: {type(e).__name__}: {e}")

    backups = []
    for candidate in ranked:
        if candidate != prediction and candidate not in backups:
            backups.append(candidate)
        if len(backups) == 2:
            break
    if len(backups) < 2:
        for candidate in (50, 45, 55, 40, 60):
            if candidate != prediction and candidate not in backups:
                backups.append(candidate)
            if len(backups) == 2:
                break

    method = (
        f"bestpick-{details.get('mode', 'smart')} "
        f"n={len(history)} | {details.get('info', '')} | "
        f"{details.get('bias', '')}{calibration} | top={details.get('top', [])}"
    )
    print(
        f"[BESTPICK] {game} | revealed_n={len(history)} | prediction={prediction} "
        f"| backups={backups} | details={details}{calibration}"
    )
    return prediction, backups, method

def get_saved_prediction(game, source_message_id, source_chat_id=SOURCE_CHAT_ID):
    """Return the canonical prediction for a round so edits/retries never change it."""
    if not connect_mongodb() or prediction_history_collection is None:
        return None
    try:
        return prediction_history_collection.find_one(
            scoped_query({"game": game.upper(), "source_message_id": int(source_message_id)}, source_chat_id),
            {"_id": 0, "prediction": 1, "backups": 1, "method": 1},
        )
    except Exception as e:
        print(f"[MODEL] Existing {game} prediction lookup failed: {e}")
        return None

def get_strategy_predictions(game, primary_prediction, history=None):
    """Record independent candidates so fresh formula performance can be compared fairly."""
    history = get_model_history(game, 15) if history is None else history[-15:]
    short_history = history[-5:]
    median5 = int(statistics.median(short_history)) if short_history else random.randint(1, 100)
    median15 = int(statistics.median(history)) if history else random.randint(1, 100)
    mean15 = round(sum(history) / len(history)) if history else random.randint(1, 100)
    return {
        "mean15": int(primary_prediction),
        "median15": int(median15),
        "median5": int(median5),
        "weighted_median5_15": round((0.65 * median5) + (0.35 * median15)) if history else random.randint(1, 100),
        "uniform_random_v1": random.randint(1, 100),
        "random_baseline": random.randint(1, 100),
    }

def save_prediction(game, source_message_id, prediction, backups, method, history=None, source_chat_id=SOURCE_CHAT_ID):
    if not connect_mongodb() or prediction_history_collection is None:
        print(f"[MODEL] Cannot persist {game} prediction; MongoDB unavailable.")
        return False
    try:
        shadow_predictions = get_strategy_predictions(game, prediction, history=history)
        prediction_history_collection.update_one(
            scoped_query({"game": game.upper(), "source_message_id": int(source_message_id)}, source_chat_id),
            {"$setOnInsert": {"game": game.upper(), "source_chat_id": int(source_chat_id), "source_message_id": int(source_message_id),
             "prediction": int(prediction), "backups": [int(n) for n in backups], "method": method,
             "shadow_predictions": shadow_predictions,
             "created_at": datetime.now(timezone.utc), "resolved": False}},
            upsert=True,
        )
        return True
    except Exception as e:
        print(f"[MODEL] Prediction save failed: {e}")
        return False

def resolve_prediction(game, actual_number, result_message_id=None, prediction_source_message_id=None, source_chat_id=SOURCE_CHAT_ID):
    """Score the nearest earlier prediction for this game's result.

    Pairing by game alone can accidentally score a stale prediction after a
    missed result. Source message IDs preserve the round order in the same chat.
    """
    if not connect_mongodb() or prediction_history_collection is None:
        return
    try:
        query = scoped_query({"game": game.upper(), "resolved": False}, source_chat_id)
        if prediction_source_message_id is None:
            print(f"[MODEL] No exact round ID supplied for {game}; refusing to score a possibly mismatched result.")
            return
        query["source_message_id"] = int(prediction_source_message_id)
        pending = prediction_history_collection.find_one(query)
        if not pending:
            print(f"[MODEL] No pending {game} prediction to score.")
            return
        prediction = int(pending["prediction"])
        backups = [int(n) for n in pending.get("backups", [])]
        error = abs(prediction - int(actual_number))
        result_doc = None
        if result_message_id is not None and result_history_collection is not None:
            result_doc = result_history_collection.find_one(
                scoped_query({"game": game.upper(), "source_message_id": int(result_message_id)}, source_chat_id),
                {"_id": 0, "result_range": 1, "user_won": 1}
            )
        result_range = result_doc.get("result_range") if result_doc else None
        prediction_won = bool(result_range and len(result_range) == 2 and int(result_range[0]) <= prediction <= int(result_range[1]))
        backup_error = min([abs(n - int(actual_number)) for n in backups] or [999])
        shadow_predictions = pending.get("shadow_predictions", {})
        shadow_results = {}
        for strategy, value in shadow_predictions.items():
            strategy_error = abs(int(value) - int(actual_number))
            shadow_results[strategy] = {
                "prediction": int(value),
                "error": strategy_error,
                "close_5": strategy_error <= 5,
            }
        prediction_history_collection.update_one({"_id": pending["_id"]}, {"$set": {
            "resolved": True, "actual": int(actual_number), "error": error, "close_5": error <= 5,
            "result_range": result_range, "prediction_won": prediction_won,
            "user_won": bool(result_doc.get("user_won")) if result_doc else False,
            "backup_best_error": backup_error, "backup_close_5": backup_error <= 5,
            "strategy_results": shadow_results,
            "resolved_at": datetime.now(timezone.utc)}})
        print(f"[MODEL] Scored {game} | predicted={prediction} | actual={actual_number} | error={error} | within5={error <= 5}")
    except Exception as e:
        print(f"[MODEL] Prediction scoring failed for {game}: {e}")


def load_last_pot_answer():
    return load_last_answer("POT")

def save_last_pot_answer(answer):
    return save_last_answer("POT", answer)

def load_last_guess_answer():
    return load_last_answer("GUESS")

def save_last_guess_answer(answer):
    return save_last_answer("GUESS", answer)

def choose_next_number(game):
    # Choose across the full 2-99 range, not a narrow band.
    # Never repeat any of the last 10 suggestions for this game.
    # Prefer a higher number sometimes, but keep the choices varied.
    key = f"recent_{game.lower()}_answers"
    history_limit = 10
    min_number, max_number = 2, 99

    try:
        if not connect_mongodb() or state_collection is None:
            last = load_last_answer(game)
            choices = [n for n in range(min_number, max_number + 1) if n != last]
            if last is not None and random.random() < 0.6:
                higher = [n for n in choices if n > last]
                if higher:
                    choices = higher
            return random.choice(choices)

        doc = state_collection.find_one(
            {"key": key},
            {"_id": 0, "values": 1},
        )
        history = [
            int(n)
            for n in (doc.get("values", []) if doc else [])
            if min_number <= int(n) <= max_number
        ][-history_limit:]

        last = history[-1] if history else load_last_answer(game)
        choices = [
            n for n in range(min_number, max_number + 1)
            if n not in history
        ]

        # Prefer a higher value in 60% of turns when possible, but don't
        # get trapped in one range or force a strict upward sequence.
        if last is not None and random.random() < 0.6:
            higher = [n for n in choices if n > last]
            if higher:
                choices = higher

        if not choices:
            # The 2-99 pool has 98 values, so this is only a safety reset.
            history = []
            choices = list(range(min_number, max_number + 1))

        number = random.choice(choices)
        history.append(number)
        history = history[-history_limit:]

        state_collection.update_one(
            {"key": key},
            {"$set": {"key": key, "values": history}},
            upsert=True,
        )

        print(f"[{game}] Recent={history} | last={last} | suggested={number}")
        return number

    except Exception as e:
        print(f"[{game}] Number selection failed: {e}")
        last = load_last_answer(game)
        choices = [n for n in range(min_number, max_number + 1) if n != last]
        return random.choice(choices)

def choose_pot_number():
    return choose_next_number("POT")

def choose_guess_number():
    return choose_next_number("GUESS")

user_client = TelegramClient(StringSession(TELETHON_SESSION), API_ID, API_HASH)
bot_client = TelegramClient("maze_pot_bot", API_ID, API_HASH)

def clean_text(text):
    return (text or "").strip()

def normalize_text(text):
    text = unicodedata.normalize("NFKC", text or "")
    text = text.replace("\ufe0f", "")
    return " ".join(text.split()).casefold()

def get_message_link(message):
    chat_id = int(getattr(message, "chat_id", SOURCE_CHAT_ID) or SOURCE_CHAT_ID)
    username = SOURCE_USERNAMES.get(chat_id, SOURCE_USERNAME)
    return f"https://t.me/{username}/{message.id}"

def detect_event_type(text):
    normalized = normalize_text(text)

    if "split the pot" in normalized:
        return "POT"
    if "guess the flag" in normalized:
        return "FLAG"
    if "new guess game" in normalized or "closest guess" in normalized:
        return "GUESS"
    if "rain:" in normalized or "new rain" in normalized or "rain!" in normalized:
        return "RAIN"

    return None

FLAG_RE = re.compile(r'[\U0001F1E6-\U0001F1FF]{2}')

# Use short, familiar, searchable country names in lowercase.
# Avoid formal/official long names in the automatic FLAG answer.
# Short, lowercase names that are easy to match in auto-search.
# Countries not listed here use pycountry's common name (lowercase) below.
FLAG_ALIASES = {
    "AE": "uae",
    "BO": "bolivia",
    "BN": "brunei",
    "CD": "dr congo",
    "CG": "congo",
    "CI": "ivory coast",
    "CZ": "czechia",
    "GB": "uk",
    "IR": "iran",
    "KP": "north korea",
    "KR": "south korea",
    "LA": "laos",
    "MK": "macedonia",
    "MD": "moldova",
    "MM": "myanmar",
    "RU": "russia",
    "SY": "syria",
    "SZ": "eswatini",
    "TL": "east timor",
    "TR": "turkey",
    "TZ": "tanzania",
    "US": "usa",
    "VA": "vatican city",
    "VE": "venezuela",
    "VN": "vietnam",
}


def flag_to_name(flag):
    code = "".join(chr(ord(char) - 0x1F1E6 + 65) for char in flag)
    if code in FLAG_ALIASES:
        return FLAG_ALIASES[code]
    country = pycountry.countries.get(alpha_2=code)
    if not country:
        return None
    # Prefer the common name when available, then normalize for easy matching.
    name = getattr(country, "common_name", None) or country.name
    return name.casefold()


def detect_flag_country(text):
    """Return the country name for the first regional-indicator flag emoji."""
    for match in FLAG_RE.finditer(text or ""):
        country = flag_to_name(match.group())
        if country:
            return country
    return None

def format_rain_alert(raw_text, source_link, bot_reply="Processing rain claim...", status="Processing"):
    """Render RAIN notifications in the ☀️ NIKA • WARRIOR OF LIBERATION event-log style."""
    raw = clean_text(raw_text or "")
    prize = re.search(r"([\d,]+)\s*points?\b", raw, re.IGNORECASE)
    winners = re.search(r"(\d+)\s*(?:random\s*)?winners?\b", raw, re.IGNORECASE)
    timer = re.search(r"(\d+)\s*(?:s|sec|secs|seconds?)\b", raw, re.IGNORECASE)
    host = next((line.strip() for line in raw.splitlines() if re.search(r"\bhosted by\b", line, re.IGNORECASE)), None)

    reply = clean_text(bot_reply or "No Telegram response received.")
    successful = str(status).casefold() in {"grab clicked", "grab successful", "success"}
    if successful:
        result_line = "⚡ NIKA has activated the Grab protocol! ☀️"
        status_line = "✅ GRAB SUCCESSFUL"
    elif str(status).casefold() in {"waiting for chat activity", "activity required"}:
        result_line = "⏳ NIKA awaits the crew's next move before retrying Grab."
        status_line = "🟡 WAITING FOR CHAT ACTIVITY"
    else:
        result_line = f"⚙️ NIKA is working the Grab protocol.\n{reply}"
        status_line = f"🟡 STATUS: {status}"

    lines = [
        "🌧️ TREASURE RAIN SPOTTED",
        "",
        f"💰 Prize Pool　{prize.group(1) + ' POINTS' if prize else 'Not Specified'}",
        f"🎲 Winners　{winners.group(1) if winners else 'Not Specified'}",
        f"⏱️ Time Limit　{timer.group(1) + ' SECONDS' if timer else 'Not Specified'}",
    ]
    if host:
        lines.append(f"🏠 {host}",
        )
    lines += [
        "",
        "━━━━━━━━━━━━━━━━━━",
        "🤖 ☀️ DRUMS OF LIBERATION LOG",
        "",
        result_line,
        "",
        "📡 DEN-DEN MUSHI RESPONSE:",
        reply,
        "",
        status_line,
        "",
        f"🔗 GRAND LINE SIGHTING: {source_link}",
        "",
        "━━━━━━━━━━━━━━━━━━",
        "🕒 ☀️ NIKA • WARRIOR OF LIBERATION",
        "Automatic Treasure-Rain Watch",
    ]
    return "\n".join(lines)


def format_notification(message, event_type):
    if event_type == "RAIN":
        return format_rain_alert(
            message.raw_text, get_message_link(message),
            bot_reply="Checking the Grab button...",
            status="Detected · processing",
        )
    title = "GUESS ROUND" if event_type == "GUESS" else "POT ROUND"
    return (
        f"{'🎯' if event_type == 'GUESS' else '🪙'} {title} DETECTED\n"
        f"\n{clean_text(message.raw_text)}\n"
        f"\n🔗 SOURCE: {get_message_link(message)}\n"
        f"\n☀️ NIKA • WARRIOR OF LIBERATION"
    )

async def get_target_user_id(event):
    if event.is_reply:
        reply = await event.get_reply_message()
        if reply and reply.sender_id:
            return int(reply.sender_id)
    parts = clean_text(event.raw_text).split()
    if len(parts) >= 2 and parts[1].lstrip("-").isdigit():
        return int(parts[1])
    return int(event.sender_id) if event.sender_id else None

def make_nika_start_image():
    """Build a Joy Boy welcome image as a safe fallback when no custom asset is installed."""
    from PIL import Image, ImageDraw, ImageFont
    image = Image.new("RGB", (1000, 420), (24, 15, 21))
    draw = ImageDraw.Draw(image)
    try:
        title_font = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf", 46)
        main_font = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf", 27)
        small_font = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSansMono.ttf", 18)
    except OSError:
        title_font = main_font = small_font = ImageFont.load_default()
    draw.rectangle((14, 14, 986, 406), fill=(255, 231, 190), outline=(112, 57, 48), width=8)
    draw.rectangle((32, 32, 968, 388), outline=(214, 157, 79), width=2)
    draw.text((55, 45), "THE GRAND LINE", font=small_font, fill=(67, 56, 34))
    draw.text((55, 91), "WARRIOR OF LIBERATION", font=title_font, fill=(35, 32, 23))
    draw.line((55, 158, 945, 158), fill=(82, 67, 40), width=3)
    draw.text((58, 190), "IDENTITY: SUN GOD NIKA", font=main_font, fill=(39, 35, 25))
    draw.text((58, 238), "STATUS: DRUMS BEATING", font=small_font, fill=(145, 91, 49))
    draw.text((58, 279), "POT / GUESS / RAIN • GRAND LINE WATCH", font=small_font, fill=(39, 35, 25))
    draw.text((58, 340), "WELCOME ABOARD. CHOOSE YOUR COURSE.", font=small_font, fill=(75, 60, 35))
    return image

@bot_client.on(events.NewMessage(pattern=r"^/app(?:@\w+)?$"))
async def app_handler(event):
    # Allow /app in groups so members can open the Nika dashboard.
    if not WEBAPP_URL:
        await event.reply("⚠️ App link is not configured. Set WEBAPP_URL in the deployment environment.")
        return
    # Use Telegram's direct Mini App launch link, not a plain website URL.
    bot_identity = await bot_client.get_me()
    bot_username = getattr(bot_identity, "username", None)
    if not bot_username:
        await event.reply("⚠️ Set a public username for this bot in BotFather before opening the Mini App.")
        return
    mini_app_link = f"https://t.me/{bot_username}?startapp=dashboard"
    await event.reply(
        "🕰️ ☀️ NIKA • MINI DEN-DEN MUSHI\nTap below to open the Mini App inside Telegram:",
        buttons=[[Button.url("▣ OPEN NIKA MINI APP", mini_app_link)]],
        link_preview=False,
    )

def help_section_text(section="core"):
    sections = {
        "core": (
            "☀️ CREW COMMANDS\n"
            "/start — Open the command center\n"
            "/help — Open this command menu\n"
            "/status — System, database and HUNT status\n"
            "/ping — Telegram/DB diagnostics\n"
            "/app — Open the Grand Line dashboard\n"
            "/speed — Processing timings\n"
            "/livegames — Current game and round status\n"
            "/stickerpack [link/name] — View or change the RAIN/reply sticker pack\n"
        ),
        "pot": (
            "🪙 POT • TREASURE RECORDS\n"
            "/potstats [limit] — POT prize history (newest first)\n"
            "POT learning uses completed winning ranges; actual POT values are not always available.\n"
        ),
        "guess": (
            "🎯 GUESS • OBSERVATION HAKI\n"
            "/guessstats [limit] — GUESS prize history (newest first)\n"
            "GUESS prize ranking needs the other players' guesses; missing crowd data is not invented.\n"
        ),
        "access": (
            "🏴‍☠️ CREW ACCESS • CAPTAIN ONLY\n"
            "/approve USER_ID — Grant alert-only access\n"
            "/unapprove USER_ID — Remove alert access\n"
            "/approved — List approved users\n"
            "/accessinfo USER_ID — Inspect access\n"
            "/requestaccess — Request access in a private chat\n"
        ),
        "automation": (
            "🌊 GRAND LINE • AUTO SAIL\n"
            "/hunt — Enable manual auto-submit\n"
            "/huntpanel — Open HUNT and timer controls\n"
            "/stop — Disable manual auto-submit\n"
            "/hunttimer 3am to 5am — Daily HUNT ON window (IST)\n"
            "POT/GUESS auto-submit follows HUNT state. RAIN uses its own activity/retry workflow.\n"
        ),
    }
    return (
        "📘 FIELD MANUAL · NIKA • JOY BOY\n"
        "━━━━━━━━━━━━━━━━━━━━\n"
        + sections.get(section, sections["core"])
        + "\n🔒 Owner-only controls. Approved users receive source alerts only."
    )


def help_menu_buttons():
    return [
        [Button.inline("🏠 CORE", b"menu:section:core"),
         Button.inline("🪙 POT", b"menu:section:pot"),
         Button.inline("🎯 GUESS", b"menu:section:guess")],
        [Button.inline("🔐 ACCESS", b"menu:section:access"),
         Button.inline("⚙️ AUTOMATION", b"menu:section:automation")],
        [Button.inline("☀️ GRAND LINE STATUS", b"menu:status")],
    ]


@bot_client.on(events.NewMessage(pattern=r"^/start(?:@\w+)?$"))
async def start_handler(event):
    if event.sender_id != OWNER_ID:
        return
    import io
    start_image = os.path.join(os.path.dirname(__file__), "assets", "nika_start.jpg")
    caption = (
        "🕰️ ☀️ NIKA • JOY BOY COMMAND DECK\n"
        "☀️ DRUMS BEATING  •  🪙 POT  •  🎯 GUESS  •  🌧️ RAIN  •  🏴‍☠️ FLAG\n\n"
        "Choose your course below, Captain. Every completed round is preserved in the logbook."
    )
    buttons = help_menu_buttons()
    if os.path.isfile(start_image):
        await event.respond(file=start_image, message=caption, buttons=buttons)
    else:
        output = io.BytesIO()
        make_nika_start_image().save(output, format="JPEG", quality=90, optimize=True)
        output.seek(0)
        output.name = "nika_joyboy_start.jpg"
        await event.respond(file=output, message=caption, buttons=buttons)


@bot_client.on(events.NewMessage(pattern=r"^/help(?:@\w+)?$"))
async def help_handler(event):
    if event.sender_id != OWNER_ID:
        return
    await event.reply(help_section_text("core"), buttons=help_menu_buttons())


@bot_client.on(events.CallbackQuery(pattern=rb"^menu:(status|section:(core|pot|guess|access|automation))$"))
async def menu_callback(event):
    if event.sender_id != OWNER_ID:
        await event.answer("🔒 Owner-only control.", alert=True)
        return
    action = event.pattern_match.group(1).decode()
    if action.startswith("section:"):
        section = action.split(":", 1)[1]
        await event.edit(help_section_text(section), buttons=help_menu_buttons())
        await event.answer()
        return
    db_ok = connect_mongodb()
    last_pot = load_last_pot_answer() if db_ok else None
    last_guess = load_last_guess_answer() if db_ok else None
    await event.edit(
        "☀️ // JOY BOY'S LOGBOOK ◈\n"
        f"🖥️ System          {'ONLINE' if bot_client.is_connected() else 'DEGRADED'}\n"
        f"🗃️ Database        {'CONNECTED' if db_ok else 'UNAVAILABLE'}\n"
        f"👥 Approved users  {len(approved_users)}\n"
        f"🪙 Last POT        {last_pot if last_pot is not None else '—'}\n"
        f"🎯 Last GUESS      {last_guess if last_guess is not None else '—'}\n"
        f"⚡ HUNT             {'ON' if hunt_enabled else 'OFF'}\n"
        f"⏰ HUNT window      {format_hunt_clock(hunt_timer_start_minute) + '–' + format_hunt_clock(hunt_timer_end_minute) + ' IST' if hunt_timer_enabled else 'NOT SET'}\n"
        "📡 Monitoring active",
        buttons=help_menu_buttons(),
    )
    await event.answer()


@bot_client.on(events.NewMessage(pattern=r"^/ping(?:@\w+)?$"))
async def ping_handler(event):
    if event.sender_id != OWNER_ID:
        return
    import io
    from PIL import Image, ImageDraw, ImageFont

    started = time.perf_counter()
    # Probe the bot's Telegram API session and archive database for a live image overlay.
    telegram_ms = None
    telegram_ok = False
    try:
        probe_start = time.perf_counter()
        await bot_client.get_me()
        telegram_ms = max(1, round((time.perf_counter() - probe_start) * 1000))
        telegram_ok = True
    except Exception:
        pass
    db_ok = False
    try:
        db_ok = bool(connect_mongodb())
    except Exception:
        db_ok = False
    user_ok = False
    try:
        user_ok = bool(user_client.is_connected())
    except Exception:
        pass
    elapsed_ms = max(1, round((time.perf_counter() - started) * 1000))
    memory_mb = None
    try:
        with open("/proc/self/status", "r", encoding="utf-8") as proc_status:
            for line in proc_status:
                if line.startswith("VmRSS:"):
                    memory_mb = round(int(line.split()[1]) / 1024, 1)
                    break
    except (OSError, ValueError, IndexError):
        pass

    # Build a native Joy Boy status poster so no legacy TVA artwork remains.
    canvas = Image.new("RGB", (1445, 1089), (16, 13, 18))
    draw = ImageDraw.Draw(canvas)
    draw.rectangle((24, 24, 1421, 1065), outline=(214, 157, 79), width=5)
    draw.rectangle((54, 54, 1391, 1035), outline=(112, 57, 48), width=2)
    try:
        title_font = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf", 58)
        label_font = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf", 25)
    except OSError:
        title_font = label_font = ImageFont.load_default()
    draw.text((92, 100), "NIKA • JOY BOY", font=title_font, fill=(255, 231, 177))
    draw.text((96, 190), "WARRIOR OF LIBERATION  /  GRAND LINE SYSTEMS", font=label_font, fill=(220, 172, 115))
    draw.line((94, 250, 1345, 250), fill=(178, 86, 57), width=4)
    draw.text((96, 330), "DEN-DEN MUSHI // LIVE VITALS", font=label_font, fill=(255, 231, 177))
    draw.text((96, 415), "TELEGRAM LATENCY", font=label_font, fill=(201, 181, 158))
    draw.text((96, 475), "BOT CONNECTION", font=label_font, fill=(201, 181, 158))
    draw.text((96, 535), "PONEGLYPH ARCHIVE", font=label_font, fill=(201, 181, 158))
    draw.text((96, 595), "USER SESSION", font=label_font, fill=(201, 181, 158))
    draw.text((96, 920), "THE DRUMS OF LIBERATION ARE STILL BEATING", font=label_font, fill=(220, 172, 115))
    try:
        font = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSansMono.ttf", max(13, canvas.width // 52))
    except OSError:
        font = ImageFont.load_default()

    # Draw live status values over the Joy Boy poster.
    # Coordinates align with the live-value panel on the Joy Boy status poster.
    scale_x = canvas.width / 1445
    scale_y = canvas.height / 1089
    value_x = int(1014 * scale_x)
    value_top = [int(y * scale_y) for y in (663, 724, 784, 845)]
    value_right = int(1275 * scale_x)
    value_bottoms = [int(y * scale_y) for y in (695, 755, 815, 875)]
    live_values = [
        f"{telegram_ms if telegram_ms is not None else 'N/A'} ms",
        "ONLINE" if telegram_ok else "DEGRADED",
        "CONNECTED" if db_ok else "OFFLINE",
        "AUTHORIZED" if user_ok else "OFFLINE",
    ]
    for y1, y2, value in zip(value_top, value_bottoms, live_values):
        # Clear only the printed sample value; preserve the panel, dividers and status lights.
        draw.rectangle((value_x - int(8 * scale_x), y1 - int(3 * scale_y),
                        value_right, y2), fill=(14, 12, 10))
        draw.text((value_x, y1), value, font=font,
                  fill=(226, 208, 176) if value not in ("DEGRADED", "OFFLINE", "N/A ms") else (235, 102, 81))
    output = io.BytesIO()
    canvas.save(output, format="JPEG", quality=90, optimize=True)
    output.seek(0)
    output.name = "nika_joyboy_ping.jpg"
    health = "HEALTHY" if telegram_ok and db_ok and user_ok else "DEGRADED"
    caption = (
        "☀️ NIKA // LIVE VITALS\n"
        f"Overall: {health}\n"
        f"Telegram API: {telegram_ms if telegram_ms is not None else 'N/A'} ms\n"
        f"Database: {'CONNECTED' if db_ok else 'OFFLINE'}\n"
        f"User session: {'CONNECTED' if user_ok else 'OFFLINE'}\n"
        f"Command runtime: {elapsed_ms} ms"
        + (f"\nProcess RAM: {memory_mb} MB" if memory_mb is not None else "")
    )
    await event.respond(file=output, message=caption)



async def notify_approved_users(message, event_type):
    """Send only POT/GUESS/RAIN source alerts to approved users; FLAG is owner-only."""
    if event_type not in ("POT", "GUESS", "RAIN"):
        print(f"[APPROVED] Skipped unsupported notification type={event_type}")
        return
    label = {"POT": "🪙 POT", "GUESS": "🎯 GUESS", "RAIN": "🌧️ RAIN"}[event_type]
    text_value = (
        "☀️ NIKA • CREW TRANSMISSION\n"
        f"{label} update detected.\n\n"
        f"{clean_text(getattr(message, 'raw_text', '') or '')}\n\n"
        f"Source: {get_message_link(message)}"
    )
    sent = 0
    failed = 0
    for user_id in tuple(approved_users):
        if int(user_id) == int(OWNER_ID):
            continue
        try:
            await bot_client.send_message(int(user_id), text_value, link_preview=False)
            sent += 1
        except Exception as e:
            failed += 1
            print(f"[APPROVED] {event_type} notification failed | user={user_id} | error={type(e).__name__}: {e}")
    print(f"[APPROVED] {event_type} notification complete | sent={sent} | failed={failed}")


@bot_client.on(events.NewMessage(pattern=r"^/approve(?:@\w+)?(?:\s+(-?\d+))?$"))
async def approve_user_handler(event):
    if event.sender_id != OWNER_ID:
        return
    target_id = int(event.pattern_match.group(1)) if event.pattern_match.group(1) else None
    if target_id is None and event.is_reply:
        replied = await event.get_reply_message()
        target_id = int(replied.sender_id) if replied and replied.sender_id else None
    if not target_id:
        await event.reply("Usage: /approve USER_ID (or reply to that user's message with /approve)")
        return
    if target_id == OWNER_ID:
        await event.reply("Owner already has full access.")
        return
    try:
        target_entity = await event.client.get_entity(target_id)
        target_username = getattr(target_entity, "username", None)
    except Exception:
        target_username = None
    if not save_approved_user(target_id, target_username):
        await event.reply("⚠️ Could not save approval. Check MongoDB connection.")
        return
    approved_users.add(target_id)
    await event.reply(f"✅ Approved notification-only access for {target_id}. They receive alerts only; no commands, predictions, or buttons.")


@bot_client.on(events.NewMessage(pattern=r"^/unapprove(?:@\w+)?(?:\s+(-?\d+))?$"))
async def unapprove_user_handler(event):
    if event.sender_id != OWNER_ID:
        return
    target_id = int(event.pattern_match.group(1)) if event.pattern_match.group(1) else None
    if target_id is None and event.is_reply:
        replied = await event.get_reply_message()
        target_id = int(replied.sender_id) if replied and replied.sender_id else None
    if not target_id:
        await event.reply("Usage: /unapprove USER_ID (or reply to that user's message with /unapprove)")
        return
    if not delete_approved_user(target_id):
        await event.reply("⚠️ User was not removed; check MongoDB connection or the user ID.")
        return
    approved_users.discard(target_id)
    await event.reply(f"✅ Removed notification access for {target_id}.")


@bot_client.on(events.NewMessage(pattern=r"^/requestaccess(?:@\w+)?$"))
async def request_access_handler(event):
    if not event.is_private:
        await event.reply("Please DM me /requestaccess so I can review your private access request.")
        return
    sender = await event.get_sender()
    user_id = int(event.sender_id)
    if user_id == OWNER_ID or user_id in approved_users:
        await event.reply("You already have access.")
        return
    if not save_access_request(user_id, getattr(sender, "username", ""), getattr(sender, "first_name", "")):
        await event.reply("Request could not be saved. Please try again later.")
        return
    await event.reply("🏴‍☠️ CREW 🏴‍☠️ CREW REQUEST\nYour request was sent to the owner. Access is not granted until approved.")
    try:
        label = f"@{sender.username}" if getattr(sender, "username", None) else (getattr(sender, "first_name", None) or "User")
        await bot_client.send_message(OWNER_ID,
            f"🏴‍☠️ CREW REQUEST\nUser: {label}\nID: {user_id}\nApprove permanent access or reject.",
            buttons=[[Button.inline("APPROVE", data=f"access_request:approve:{user_id}".encode()),
                      Button.inline("REJECT", data=f"access_request:reject:{user_id}".encode())]])
    except Exception as e:
        print(f"[ACCESS] Owner notification failed: {type(e).__name__}: {e}")


@bot_client.on(events.CallbackQuery(pattern=rb"^access_request:(approve|reject):(-?\d+)$"))
async def access_request_callback(event):
    if event.sender_id != OWNER_ID:
        await event.answer("Owner-only control.", alert=True)
        return
    action = event.pattern_match.group(1).decode()
    user_id = int(event.pattern_match.group(2))
    if action == "approve":
        if not save_approved_user(user_id):
            await event.answer("MongoDB unavailable; approval not saved.", alert=True)
            return
        approved_users.add(user_id)
        set_access_request_status(user_id, "approved")
        await event.edit(f"🏴‍☠️ CREW ACCESS GRANTED\nUser ID: {user_id}\nAccess: permanent\nPermissions: source alerts only.")
        try:
            await bot_client.send_message(user_id, "Your NIKA access request was approved. You will receive source alerts only.")
        except Exception as e:
            print(f"[ACCESS] Approval DM failed for {user_id}: {type(e).__name__}")
        await event.answer("Permanent access granted.")
    else:
        set_access_request_status(user_id, "rejected")
        await event.edit(f"🏴‍☠️ CREW REQUEST REJECTED\nUser ID: {user_id}")
        try:
            await bot_client.send_message(user_id, "Your NIKA access request was not approved.")
        except Exception as e:
            print(f"[ACCESS] Rejection DM failed for {user_id}: {type(e).__name__}")
        await event.answer("Request rejected.")


@bot_client.on(events.NewMessage(pattern=r"^/approved(?:@\w+)?$"))
async def approved_users_handler(event):
    if event.sender_id != OWNER_ID:
        return
    ids = sorted(approved_users)
    if not ids:
        await event.reply("🏴‍☠️ ALLIED CREW\nNo users are currently approved.")
        return
    await event.reply("🏴‍☠️ ALLIED CREW · " + str(len(ids)) + " USERS\n" +
        "\n".join(f"{index}. {user_id}" for index, user_id in enumerate(ids, 1)) +
        "\n\nUse /unapprove USER_ID to revoke access.")


@bot_client.on(events.NewMessage(pattern=r"^/accessinfo(?:@\w+)?(?:\s+(-?\d+))?$"))
async def access_info_handler(event):
    if event.sender_id != OWNER_ID:
        return
    target_id = int(event.pattern_match.group(1)) if event.pattern_match.group(1) else None
    if target_id is None and event.is_reply:
        replied = await event.get_reply_message()
        target_id = int(replied.sender_id) if replied and replied.sender_id else None
    if target_id is None:
        await event.reply("Usage: /accessinfo USER_ID")
        return
    approved = target_id in approved_users
    pending = any(int(row.get("user_id", 0)) == target_id for row in get_access_requests())
    await event.reply("🏴‍☠️ CREW DOSSIER\n"
        f"User ID: {target_id}\n"
        f"Approval: {'PERMANENT' if approved else 'NOT APPROVED'}\n"
        f"Pending request: {'YES' if pending else 'NO'}\n"
        "Permissions: source alerts only; no owner controls.")


@bot_client.on(events.NewMessage(pattern=r"^/livegames(?:@\w+)?$"))
async def live_games_handler(event):
    if event.sender_id != OWNER_ID:
        return
    lines = ["☀️ NIKA • GRAND LINE STATUS", "━━━━━━━━━━━━━━━━━━━━"]
    for game in ("POT", "GUESS", "RAIN", "FLAG"):
        state = "MONITORING" if MONITOR_STATES.get(game, False) else "PAUSED"
        round_id = latest_game_round_ids.get((int(SOURCE_CHAT_ID), game)) if game in ("POT", "GUESS") else None
        if game == "GUESS" and active_guess_round is not None:
            detail = f"round #{active_guess_round} · {len(guess_crowd.get(active_guess_round, {}))} guesses observed"
        elif game in ("POT", "GUESS") and round_id:
            detail = f"latest round #{round_id}"
        else:
            detail = "waiting for next source event"
        lines.append(f"{state} · {game}\n   {detail}")
    lines.append(f"\nUptime: {int(time.monotonic() - PROCESS_STARTED_AT)}s")
    await event.reply("\n".join(lines))


@bot_client.on(events.NewMessage(pattern=r"^/speed(?:@\w+)?$"))
async def speed_handler(event):
    if event.sender_id != OWNER_ID:
        return
    lines = ["⚡ ☀️ NIKA • HAKI SPEED", "━━━━━━━━━━━━━━━━━━━━"]
    for name in ("FLAG", "RAIN", "POT", "GUESS"):
        m = SPEED_METRICS[name]
        if m["count"]:
            lines.append(f"{name:<5} last {m['last_ms']} ms · avg {m['avg_ms']} ms · max {m['max_ms']} ms · n={m['count']}")
            lines.append(f"      last event: {m['last_at']}")
        else:
            lines.append(f"{name:<5} waiting for first event")
    lines += ["", "Times measure local detection-to-alert completion.", "Metrics reset when the bot restarts."]
    await event.reply("\n".join(lines))


@bot_client.on(events.NewMessage(pattern=r"(?i)^/resetolddata(?:@\w+)?(?:\s+(mazes|mazemoney))?$"))
async def reset_old_data_handler(event):
    if event.sender_id != OWNER_ID:
        return

    target = (event.pattern_match.group(1) or "all").casefold()
    if target == "mazes":
        source_chat_id = -1003707950453
        scope_label = "mazes"
    elif target == "mazemoney":
        source_chat_id = SOURCE_CHAT_ID
        scope_label = "MazeMoney"
    else:
        source_chat_id = None
        scope_label = "both groups"

    await event.reply(
        "⚠️ CONFIRM OLD DATA RESET\n"
        f"Scope: {scope_label}\n"
        "This permanently deletes saved POT/GUESS result and prediction history.\n"
        "It does NOT delete settings, rent, points, approved users, or answer history.\n"
        "Confirm only if you want to continue.",
        buttons=[[
            Button.inline("🗑 CONFIRM RESET", f"resetolddata:confirm:{source_chat_id if source_chat_id is not None else 'all'}".encode()),
            Button.inline("CANCEL", b"resetolddata:cancel"),
        ]],
    )


@bot_client.on(events.CallbackQuery(pattern=rb"^resetolddata:(confirm:(all|-?\d+)|cancel)$"))
async def reset_old_data_callback(event):
    if event.sender_id != OWNER_ID:
        await event.answer("🔒 Owner-only control.", alert=True)
        return

    action = event.pattern_match.group(1).decode("ascii")
    if action == "cancel":
        await event.edit("Reset cancelled. No data was deleted.")
        await event.answer("Cancelled.")
        return

    if not connect_mongodb() or result_history_collection is None or prediction_history_collection is None:
        await event.answer("MongoDB unavailable. Nothing was deleted.", alert=True)
        return

    scope = action.split(":", 1)[1]
    game_filter = {"game": {"$in": ["POT", "GUESS"]}}
    if scope == "all":
        result_filter = dict(game_filter)
        prediction_filter = dict(game_filter)
        scope_label = "both groups"
    else:
        source_chat_id = int(scope)
        if source_chat_id == int(SOURCE_CHAT_ID):
            source_filter = {"$or": [
                {"source_chat_id": source_chat_id},
                {"source_chat_id": {"$exists": False}},
                {"source_chat_id": None},
            ]}
        else:
            source_filter = {"source_chat_id": source_chat_id}
        result_filter = {"$and": [game_filter, source_filter]}
        prediction_filter = {"$and": [game_filter, source_filter]}
        scope_label = source_name(source_chat_id)

    try:
        result_deleted = result_history_collection.delete_many(result_filter).deleted_count
        prediction_deleted = prediction_history_collection.delete_many(prediction_filter).deleted_count
        cache = globals().get("PREDICTOR_VALIDATION_CACHE")
        if isinstance(cache, dict):
            cache.clear()
        await event.edit(
            "✅ OLD DATA RESET COMPLETE\n"
            f"Scope: {scope_label}\n"
            f"Result records deleted: {result_deleted}\n"
            f"Prediction records deleted: {prediction_deleted}\n"
            "POT/GUESS saved history is now cleared. Other bot data was preserved."
        )
        await event.answer("Reset complete.")
        print(
            f"[RESET] Owner reset old POT/GUESS data | scope={scope_label} "
            f"| results={result_deleted} | predictions={prediction_deleted}"
        )
    except Exception as e:
        print(f"[RESET] Old data reset failed: {type(e).__name__}: {e}")
        await event.answer("Reset failed. Check deployment logs.", alert=True)


@bot_client.on(events.NewMessage(pattern=r"^/status(?:@\w+)?$"))
async def status_handler(event):
    if event.sender_id != OWNER_ID:
        return
    db_ok = connect_mongodb()
    last_pot = load_last_pot_answer() if db_ok else None
    last_guess = load_last_guess_answer() if db_ok else None
    report = (
        '☀️ NIKA • GRAND LINE STATUS '
        '\n'
        f'🖥️ System          {"ONLINE" if bot_client.is_connected() else "DEGRADED"}\n'
        f'🗃️ Database        {"CONNECTED" if db_ok else "UNAVAILABLE"}\n'
        f'👥 Approved users  {len(approved_users)}\n'
        f'🪙 Last POT        {last_pot if last_pot is not None else "—"}\n'
        f'🎯 Last GUESS      {last_guess if last_guess is not None else "—"}\n'
        f'⚡ HUNT             {"ON" if hunt_enabled else "OFF"}\n'
        f'⏰ HUNT window      {format_hunt_clock(hunt_timer_start_minute) + "–" + format_hunt_clock(hunt_timer_end_minute) + " IST" if hunt_timer_enabled else "NOT SET"}\n'
        f'📡 Monitoring active'
    )
    await event.reply(report, buttons=[
        [Button.inline("▤ FIELD MANUAL", b"menu:help")],
    ])

processed_game_authorizations = set()

# Owner-controlled auto-submit mode. Off by default and reset on restart.
hunt_enabled = False

def refresh_effective_hunt_state():
    """Manual HUNT and the daily timer are independent; either can enable auto-submit."""
    global hunt_enabled
    hunt_enabled = bool(hunt_manual_enabled or (hunt_timer_enabled and hunt_schedule_armed and hunt_schedule_active))
    return hunt_enabled


def hunt_next_transition_text(now=None):
    now = now or datetime.now(LOCAL_TZ)
    if not hunt_timer_enabled or not hunt_schedule_armed:
        return "No scheduled transition"
    active = hunt_timer_is_in_active_window(now)
    target_minute = hunt_timer_end_minute if active else hunt_timer_start_minute
    target = now.replace(hour=target_minute // 60, minute=target_minute % 60, second=0, microsecond=0)
    if target <= now:
        target = datetime.fromtimestamp(target.timestamp() + 86400, LOCAL_TZ)
    seconds = max(0, int((target - now).total_seconds()))
    hours, remainder = divmod(seconds, 3600)
    minutes = remainder // 60
    action = "STOP" if active else "START"
    return f"{action} at {format_hunt_clock(target_minute)} IST (in {hours}h {minutes}m)"


def hunt_panel_text():
    now = datetime.now(LOCAL_TZ)
    current = now.strftime("%a, %d %b • %I:%M:%S %p")
    timer_line = (
        f"{format_hunt_clock(hunt_timer_start_minute)} → {format_hunt_clock(hunt_timer_end_minute)} IST"
        if hunt_timer_enabled else "OFF"
    )
    timer_state = "ARMED" if hunt_timer_enabled and hunt_schedule_armed else "DISABLED"
    window_state = "ACTIVE NOW" if hunt_schedule_active else "WAITING"
    auto_state = "🟢 ACTIVE" if hunt_enabled else "🔴 INACTIVE"
    return (
        "🕰️ ☀️ NIKA • HUNT CONTROL\n"
        "━━━━━━━━━━━━━━━━━━━━\n"
        f"🇮🇳 INDIA TIME: {current} IST\n\n"
        "⚡ MANUAL MODE\n"
        f"   {'🟢 ON' if hunt_manual_enabled else '🔴 OFF'}\n"
        "⏰ DAILY TIMER\n"
        f"   Window: {timer_line}\n"
        f"   State: {timer_state} • {window_state}\n"
        f"   Next: {hunt_next_transition_text(now)}\n\n"
        f"🚀 EFFECTIVE AUTO-SUBMIT: {auto_state}\n"
        "━━━━━━━━━━━━━━━━━━━━\n"
        "Manual HUNT and the timer are independent. Either can enable auto-submit.\n"
        "Custom hours: /hunttimer 3am to 5am\n"
        "Disable schedule: /hunttimer off"
    )


def hunt_panel_buttons():
    manual_label = "🔴 TURN MANUAL HUNT OFF" if hunt_manual_enabled else "🟢 TURN MANUAL HUNT ON"
    timer_label = "🛑 DISABLE DAILY TIMER" if hunt_timer_enabled else "⏰ ENABLE 3AM–5AM TIMER"
    return [
        [Button.inline(manual_label, data=b"huntpanel:manual")],
        [Button.inline(timer_label, data=b"huntpanel:toggle_timer")],
        [
            Button.inline("🌅 3–5 AM", data=b"huntpanel:preset_3_5"),
            Button.inline("☀️ 6–8 AM", data=b"huntpanel:preset_6_8"),
            Button.inline("🌙 9–11 PM", data=b"huntpanel:preset_21_23"),
        ],
        [Button.inline("🔄 REFRESH STATUS", data=b"huntpanel:refresh")],
    ]


@bot_client.on(events.NewMessage(pattern=r"^/huntpanel(?:@\w+)?$"))
async def hunt_panel_handler(event):
    if event.sender_id != OWNER_ID:
        return
    refresh_effective_hunt_state()
    await event.reply(hunt_panel_text(), buttons=hunt_panel_buttons())


@bot_client.on(events.CallbackQuery(pattern=rb"^huntpanel:(manual|toggle_timer|preset_3_5|preset_6_8|preset_21_23|refresh)$"))
async def hunt_panel_callback(event):
    global hunt_manual_enabled, hunt_timer_enabled, hunt_schedule_armed
    global hunt_timer_start_minute, hunt_timer_end_minute, hunt_schedule_active
    if event.sender_id != OWNER_ID:
        await event.answer("Owner-only control.", alert=True)
        return
    action = event.pattern_match.group(1).decode()
    if action == "manual":
        hunt_manual_enabled = not hunt_manual_enabled
    elif action == "toggle_timer":
        if hunt_timer_enabled:
            hunt_timer_enabled = False
            hunt_schedule_armed = False
            hunt_schedule_active = False
            hunt_timer_start_minute = None
            hunt_timer_end_minute = None
        else:
            hunt_timer_start_minute, hunt_timer_end_minute = 180, 300
            hunt_timer_enabled = True
            hunt_schedule_armed = True
            hunt_schedule_active = hunt_timer_is_in_active_window()
    elif action.startswith("preset_"):
        presets = {
            "preset_3_5": (180, 300),
            "preset_6_8": (360, 480),
            "preset_21_23": (1260, 1380),
        }
        hunt_timer_start_minute, hunt_timer_end_minute = presets[action]
        hunt_timer_enabled = True
        hunt_schedule_armed = True
        hunt_schedule_active = hunt_timer_is_in_active_window()
    refresh_effective_hunt_state()
    saved = save_hunt_timer_config()
    await event.edit(hunt_panel_text(), buttons=hunt_panel_buttons())
    await event.answer("Updated and saved." if saved else "Updated in memory; database save failed.", alert=not saved)


def parse_hunt_clock(value):
    match = re.fullmatch(r"\s*(\d{1,2})(?::(\d{2}))?\s*(am|pm)\s*", value, re.IGNORECASE)
    if not match:
        raise ValueError("Use a time such as 3am or 5:30pm.")
    hour = int(match.group(1))
    minute = int(match.group(2) or 0)
    meridiem = match.group(3).casefold()
    if not 1 <= hour <= 12 or not 0 <= minute <= 59:
        raise ValueError("Invalid time. Use 1–12 for the hour and 00–59 for minutes.")
    hour = hour % 12 + (12 if meridiem == "pm" else 0)
    return hour * 60 + minute


def hunt_timer_is_in_active_window(now=None):
    """Return whether Asia/Kolkata local time is inside the daily HUNT ON window."""
    if not hunt_timer_enabled or hunt_timer_start_minute is None or hunt_timer_end_minute is None:
        return False
    now = now or datetime.now(LOCAL_TZ)
    current = now.hour * 60 + now.minute
    if hunt_timer_start_minute < hunt_timer_end_minute:
        return hunt_timer_start_minute <= current < hunt_timer_end_minute
    return current >= hunt_timer_start_minute or current < hunt_timer_end_minute


def format_hunt_clock(minutes):
    hour24, minute = divmod(int(minutes), 60)
    suffix = "am" if hour24 < 12 else "pm"
    hour12 = hour24 % 12 or 12
    return f"{hour12}:{minute:02d}{suffix}" if minute else f"{hour12}{suffix}"


def save_hunt_timer_config():
    if not connect_mongodb() or state_collection is None:
        return False
    try:
        state_collection.update_one(
            {"key": "hunt_timer"},
            {"$set": {"key": "hunt_timer", "value": {
                "enabled": bool(hunt_timer_enabled),
                "armed": bool(hunt_schedule_armed),
                "start_minute": hunt_timer_start_minute,
                "end_minute": hunt_timer_end_minute,
                "timezone": "Asia/Kolkata",
            }}},
            upsert=True,
        )
        return True
    except Exception as exc:
        print(f"[HUNT TIMER] Config save failed: {exc}")
        return False


def load_hunt_timer_config():
    global hunt_timer_start_minute, hunt_timer_end_minute, hunt_timer_enabled, hunt_schedule_armed
    if not connect_mongodb() or state_collection is None:
        return
    try:
        doc = state_collection.find_one({"key": "hunt_timer"}, {"_id": 0, "value": 1})
        value = doc.get("value", {}) if doc else {}
        if value.get("enabled") and value.get("start_minute") is not None and value.get("end_minute") is not None:
            start_minute = int(value["start_minute"])
            end_minute = int(value["end_minute"])
            if start_minute != end_minute:
                hunt_timer_start_minute = start_minute
                hunt_timer_end_minute = end_minute
                hunt_timer_enabled = True
                hunt_schedule_armed = bool(value.get("armed", False))
        print(f"[HUNT TIMER] Loaded | enabled={hunt_timer_enabled} | armed={hunt_schedule_armed} | timezone=Asia/Kolkata")
    except Exception as exc:
        print(f"[HUNT TIMER] Config load failed: {exc}")


async def save_sticker_pack_name(short_name):
    if not connect_mongodb() or state_collection is None:
        return False
    try:
        state_collection.update_one(
            {"key": "rain_sticker_pack"},
            {"$set": {"key": "rain_sticker_pack", "value": short_name}},
            upsert=True,
        )
        return True
    except Exception as exc:
        print(f"[STICKER PACK] Save failed: {type(exc).__name__}: {exc}")
        return False


def load_sticker_pack_name():
    global RAIN_STICKER_PACK, _rain_sticker_set_cache, GAME_REPLY_STICKER_NUMBER
    if not connect_mongodb() or state_collection is None:
        return
    try:
        doc = state_collection.find_one({"key": "rain_sticker_pack"}, {"_id": 0, "value": 1})
        value = doc.get("value") if doc else None
        if isinstance(value, str) and value:
            RAIN_STICKER_PACK = value
        index_doc = state_collection.find_one({"key": "rain_reply_sticker_number"}, {"_id": 0, "value": 1})
        try:
            GAME_REPLY_STICKER_NUMBER = max(1, int(index_doc.get("value", 1))) if index_doc else 1
        except (TypeError, ValueError):
            GAME_REPLY_STICKER_NUMBER = 1
        _rain_sticker_set_cache = None
        print(f"[STICKER PACK] Loaded | pack={RAIN_STICKER_PACK}")
    except Exception as exc:
        print(f"[STICKER PACK] Load failed: {type(exc).__name__}: {exc}")


@bot_client.on(events.NewMessage(pattern=r"^/stickerpack(?:@\w+)?(?:\s+(.+))?$"))
async def sticker_pack_command(event):
    global RAIN_STICKER_PACK, _rain_sticker_set_cache, GAME_REPLY_STICKER_NUMBER
    if event.sender_id != OWNER_ID:
        return
    argument = (event.pattern_match.group(1) or "").strip()
    if not argument:
        await event.reply(
            "🎭 CURRENT STICKER PACK\n"
            f"https://t.me/addstickers/{RAIN_STICKER_PACK}\n\n"
            f"Reply reaction sticker: #{GAME_REPLY_STICKER_NUMBER}\n\n"
            "Change pack and choose a sticker:\n/stickerpack https://t.me/addstickers/PACK_NAME #1\n"
            "You can choose any sticker number available in that pack.\n"
            "Or restore default:\n/stickerpack default #34"
        )
        return
    number_match = re.search(r"(?:\s+|^)#(\d+)\s*$", argument)
    requested_number = int(number_match.group(1)) if number_match else GAME_REPLY_STICKER_NUMBER
    pack_argument = argument[:number_match.start()].strip() if number_match else argument
    if requested_number < 1:
        await event.reply("⚠️ Sticker number must be 1 or higher. Example: /stickerpack PACK_NAME #1")
        return
    if pack_argument.casefold() in {"default", "reset"}:
        new_name = DEFAULT_RAIN_STICKER_PACK
    else:
        match = re.search(r"(?:https?://)?t\.me/(?:addstickers/)?([A-Za-z0-9_]+)", pack_argument, re.IGNORECASE)
        new_name = match.group(1) if match else pack_argument.lstrip("@")
        if not re.fullmatch(r"[A-Za-z0-9_]{5,64}", new_name):
            await event.reply("⚠️ Invalid pack. Send /stickerpack PACK_NAME or its t.me/addstickers link.")
            return
    try:
        sticker_set = await user_client(
            functions.messages.GetStickerSetRequest(
                stickerset=types.InputStickerSetShortName(short_name=new_name),
                hash=0,
            )
        )
        documents = list(getattr(sticker_set, "documents", []) or [])
        if requested_number > len(documents):
            await event.reply(
                f"⚠️ This pack has {len(documents)} stickers. Choose a number from 1 to {len(documents)}."
            )
            return
        old_name = RAIN_STICKER_PACK
        old_number = GAME_REPLY_STICKER_NUMBER
        RAIN_STICKER_PACK = new_name
        GAME_REPLY_STICKER_NUMBER = requested_number
        _rain_sticker_set_cache = documents
        if not save_sticker_pack_name(new_name):
            RAIN_STICKER_PACK = old_name
            GAME_REPLY_STICKER_NUMBER = old_number
            _rain_sticker_set_cache = None
            await event.reply("⚠️ Pack validated, but MongoDB save failed. The current pack was not changed.")
            return
        try:
            state_collection.update_one(
                {"key": "rain_reply_sticker_number"},
                {"$set": {"key": "rain_reply_sticker_number", "value": GAME_REPLY_STICKER_NUMBER}},
                upsert=True,
            )
        except Exception as exc:
            print(f"[STICKER PACK] Reply sticker number save failed: {type(exc).__name__}: {exc}")
        await event.reply(
            "✅ STICKER PACK UPDATED\n"
            f"Pack: https://t.me/addstickers/{RAIN_STICKER_PACK}\n"
            f"Reply reaction sticker: #{GAME_REPLY_STICKER_NUMBER}.\n"
            "RAIN random stickers use this pack too."
        )
        print(f"[STICKER PACK] Updated by owner | old={old_name} | new={new_name}")
    except Exception as exc:
        await event.reply(f"⚠️ Could not load that sticker pack: {type(exc).__name__}: {str(exc)[:180]}")


@bot_client.on(events.NewMessage(pattern=r"^/hunttimer(?:@\w+)?(?:\s+(.+))?$"))
async def hunt_timer_handler(event):
    global hunt_timer_start_minute, hunt_timer_end_minute, hunt_timer_enabled, hunt_schedule_armed, hunt_enabled, hunt_schedule_active
    if event.sender_id != OWNER_ID:
        return
    argument = (event.pattern_match.group(1) or "").strip()
    if not argument:
        if hunt_timer_enabled:
            state = "ARMED" if hunt_schedule_armed else "DISARMED"
            await event.reply(
                "⏰ ☀️ NIKA • HUNT SCHEDULE\n"
                f"🟢 HUNT START: {format_hunt_clock(hunt_timer_start_minute)} IST\n"
                f"🛑 HUNT STOP: {format_hunt_clock(hunt_timer_end_minute)} IST\n"
                f"⚙️ Schedule: {state}\n"
                f"📍 Current HUNT: {'ON' if hunt_enabled else 'OFF'}\n"
                "🌏 Time zone: Asia/Kolkata (IST)\n"
                "Use /hunttimer off to disable the schedule."
            )
        else:
            await event.reply("⏰ HUNT schedule is OFF.\nSet it with: /hunttimer 3am to 5am")
        return
    if argument.casefold() in {"off", "disable", "none"}:
        hunt_timer_enabled = False
        hunt_schedule_armed = False
        hunt_schedule_active = False
        refresh_effective_hunt_state()
        hunt_timer_start_minute = None
        hunt_timer_end_minute = None
        saved = save_hunt_timer_config()
        await event.reply(
            "🛑 HUNT SCHEDULE DISABLED\n"
            "The schedule will no longer start or stop HUNT automatically. Current HUNT state is unchanged.\n"
            + ("💾 Setting saved." if saved else "⚠️ Database unavailable; this change lasts until restart.")
        )
        return
    match = re.fullmatch(r"(.+?)\s+to\s+(.+)", argument, re.IGNORECASE)
    if not match:
        await event.reply("Usage: /hunttimer 3am to 5am\nThis means HUNT ON at 3am and OFF at 5am, every day (IST).")
        return
    try:
        start_minute = parse_hunt_clock(match.group(1))
        end_minute = parse_hunt_clock(match.group(2))
        if start_minute == end_minute:
            raise ValueError("Start and stop times must be different.")
    except ValueError as exc:
        await event.reply(f"⚠️ {exc}")
        return
    hunt_timer_start_minute = start_minute
    hunt_timer_end_minute = end_minute
    hunt_timer_enabled = True
    hunt_schedule_armed = True
    saved = save_hunt_timer_config()
    current_ist = datetime.now(LOCAL_TZ).strftime("%I:%M:%S %p")
    inside_window = hunt_timer_is_in_active_window()
    if inside_window:
        hunt_schedule_active = True
        refresh_effective_hunt_state()
        state_text = "Scheduled HUNT window is active."
    else:
        hunt_schedule_active = False
        refresh_effective_hunt_state()
        state_text = f"Scheduled HUNT will turn ON at {format_hunt_clock(start_minute)} IST."
    await event.reply(
        "✅ ☀️ NIKA • DAILY HUNT WINDOW SET\n"
        f"🟢 START: {format_hunt_clock(start_minute)} IST\n"
        f"🛑 STOP: {format_hunt_clock(end_minute)} IST\n"
        f"🕒 CURRENT IST: {current_ist}\n"
        f"⚡ {state_text}\n"
        "📅 Repeats daily. Schedule is armed and saved.\n"
        + ("💾 Database: saved." if saved else "⚠️ Database unavailable; schedule will not survive a restart.")
    )
    print(f"[HUNT TIMER] Daily active window {format_hunt_clock(start_minute)}–{format_hunt_clock(end_minute)} IST | armed=True")


async def hunt_timer_worker():
    global hunt_schedule_active
    while True:
        await asyncio.sleep(2)
        if not hunt_timer_enabled or not hunt_schedule_armed:
            continue
        should_run = hunt_timer_is_in_active_window()
        if should_run == hunt_schedule_active:
            continue
        hunt_schedule_active = should_run
        refresh_effective_hunt_state()
        if should_run:
            message = (
                "🟢 ☀️ NIKA • DAILY HUNT STARTED\n"
                "Timer window is active. POT/GUESS auto-submit is ON."
            )
            print("[HUNT TIMER] Scheduled window started.")
        else:
            message = (
                "🛑 ☀️ NIKA • DAILY HUNT STOPPED\n"
                "Timer window ended. Manual HUNT remains independent.\n"
                f"Effective auto-submit: {'ON' if hunt_enabled else 'OFF'}."
            )
            print("[HUNT TIMER] Scheduled window ended.")
        try:
            await bot_client.send_message(OWNER_ID, message)
        except Exception as exc:
            print(f"[HUNT TIMER] Schedule notice failed: {exc}")


@bot_client.on(events.NewMessage(pattern=r"^/hunt(?:@\w+)?$"))
async def hunt_handler(event):
    global hunt_manual_enabled
    if event.sender_id != OWNER_ID:
        return
    hunt_manual_enabled = True
    refresh_effective_hunt_state()
    await event.reply(
        "🟢 MANUAL HUNT ENABLED\n"
        "POT/GUESS auto-submit is ON independently of the daily timer.\n"
        "Use /stop to turn off manual HUNT."
    )
    print("[HUNT] Manual auto-submit enabled by owner")


@bot_client.on(events.NewMessage(pattern=r"^/stop(?:@\w+)?$"))
async def stop_hunt_handler(event):
    global hunt_manual_enabled
    if event.sender_id != OWNER_ID:
        return
    hunt_manual_enabled = False
    refresh_effective_hunt_state()
    await event.reply(
        "🛑 MANUAL HUNT DISABLED\n"
        "The daily timer remains independent and can still enable HUNT during its window.\n"
        f"Effective auto-submit is {'ON' if hunt_enabled else 'OFF'}."
    )
    print("[HUNT] Manual mode disabled; scheduled mode unchanged")


async def auto_send_game_value(source_chat_id, message_id, game, answer, save_answer):
    """Submit one prediction automatically, with the same duplicate protection as manual mode."""
    key = (game.upper(), int(source_chat_id), int(message_id))
    if key in processed_game_authorizations:
        return False, "already processed"
    source_message = await user_client.get_messages(int(source_chat_id), ids=int(message_id))
    if not source_message:
        return False, "source round unavailable"
    processed_game_authorizations.add(key)
    try:
        sent_message = await user_client.send_message(int(source_chat_id), str(int(answer)), link_preview=False)
        remember_game_message(source_chat_id, sent_message, game)
    except Exception:
        processed_game_authorizations.discard(key)
        raise
    if game.upper() == "GUESS" and connect_mongodb() and prediction_history_collection is not None:
        try:
            prediction_history_collection.update_one(
                scoped_query({"game": "GUESS", "source_message_id": int(message_id)}, source_chat_id),
                {"$set": {"prediction": int(answer), "auto_sent": True}},
            )
        except Exception as e:
            print(f"[GUESS] Could not update auto-sent prediction log: {e}")
    saved = await asyncio.to_thread(save_answer, int(answer))
    print(f"[HUNT] {game} auto-sent={answer} | source_message={message_id} | history_saved={saved}")
    return True, ("history saved" if saved else "history save failed")

async def send_game_answer(event, game, label, save_answer):
    """Send a POT/GUESS value only after the owner confirms the inline button."""
    if event.sender_id != OWNER_ID:
        await event.answer("Owner-only control.", alert=True)
        return

    try:
        source_chat_id = int(event.pattern_match.group(1))
        message_id = int(event.pattern_match.group(2))
        answer = int(event.pattern_match.group(3))
        if not 1 <= answer <= 100:
            await event.answer("Invalid value; expected 1–100.", alert=True)
            return

        authorization_key = (game.upper(), source_chat_id, message_id)
        if authorization_key in processed_game_authorizations:
            await event.answer("This value was already authorized.", alert=True)
            return

        # Confirm the source round still exists before sending anything.
        source_message = await user_client.get_messages(source_chat_id, ids=message_id)
        if not source_message:
            await event.answer("Source round is unavailable; nothing sent.", alert=True)
            return

        # Claim this authorization before sending to avoid double-click duplicates.
        processed_game_authorizations.add(authorization_key)
        try:
            sent_message = await user_client.send_message(
                source_chat_id, str(answer), link_preview=False
            )
            remember_game_message(source_chat_id, sent_message, game)
        except Exception:
            processed_game_authorizations.discard(authorization_key)
            raise

        if game.upper() == "GUESS" and connect_mongodb() and prediction_history_collection is not None:
            try:  # log what was actually sent, not the number that was shown first
                prediction_history_collection.update_one(
                    scoped_query({"game": "GUESS", "source_message_id": message_id}, source_chat_id), {"$set": {"prediction": answer}})
            except Exception as e:
                print(f"[GUESS] Could not update logged prediction: {e}")
        save_answer_ok = save_answer(answer)
        await event.edit(
            f"✅ {label} VALUE SENT\n"
            f"🎯 Value: {answer}\n"
            f"🔗 Source post: #{message_id}\n"
            + ("🗃️ Answer history saved." if save_answer_ok else "⚠️ Sent, but database history could not be saved.")
        )
        await event.answer(f"{label} value sent: {answer}")
        print(f"[{label}] Owner authorized value={answer} | source_message={message_id} | saved={save_answer_ok}")
    except Exception as e:
        print(f"[{label}] Authorization failed: {type(e).__name__}: {e}")
        await event.answer(f"Send failed: {type(e).__name__}. Check Koyeb logs.", alert=True)


@bot_client.on(events.CallbackQuery(pattern=rb"^pot_send:(-?\d+):(\d+):(\d+)$"))
async def pot_send_callback(event):
    await send_game_answer(event, "POT", "POT", save_last_pot_answer)

@bot_client.on(events.CallbackQuery(pattern=rb"^guess_send:(-?\d+):(\d+):(\d+)$"))
async def guess_send_callback(event):
    await send_game_answer(event, "GUESS", "GUESS", save_last_guess_answer)

@bot_client.on(events.CallbackQuery(pattern=rb"^flag_send:(-?\d+):(\d+)$"))
async def flag_send_callback(event):
    if event.sender_id != OWNER_ID:
        await event.answer("Owner-only control.", alert=True)
        return
    try:
        source_chat_id = int(event.pattern_match.group(1))
        message_id = int(event.pattern_match.group(2))
        source_message = await user_client.get_messages(source_chat_id, ids=message_id)
        if not source_message:
            await event.answer("Source flag post is unavailable.", alert=True)
            return

        answer = detect_flag_country(getattr(source_message, "raw_text", "") or "")
        if not answer:
            await event.answer("No supported flag found in the source post.", alert=True)
            return

        # Final output guard: every FLAG answer sent to the game chat must be lowercase.
        answer = answer.casefold()
        await user_client.send_message(source_chat_id, answer, link_preview=False)
        await event.edit(
            "✅ FLAG ANSWER SENT\n"
            f"🏳️ Country: {answer}\n"
            f"🔗 Source post: #{message_id}\n"
            "Sent after owner confirmation."
        )
        await event.answer(f"Sent: {answer}")
        print(f"[FLAG] Owner confirmed answer={answer} | source_message={message_id}")
    except Exception as e:
        print(f"[FLAG] Failed to send confirmed answer: {type(e).__name__}: {e}")
        await event.answer(f"Send failed: {type(e).__name__}", alert=True)


processed_rain_alert_ids = set()
processed_rain_completed_ids = set()
pending_rain_retries = {}  # (source_chat_id, message_id) -> owner notification message ID

# Track messages the account sends to source groups during RAIN/POT/GUESS.
# A reply within three minutes gets the owner's selected sticker from the configured pack.
GAME_REPLY_STICKER_WINDOW = 3 * 60
recent_game_messages = {}  # (chat_id, message_id) -> (event_type, monotonic_sent_at)


def remember_game_message(chat_id, sent_message, event_type):
    message_id = getattr(sent_message, "id", None)
    if message_id is None:
        return
    now = time.monotonic()
    recent_game_messages[(int(chat_id), int(message_id))] = (str(event_type).upper(), now)
    # Keep this small and discard entries outside the reply window.
    for key, (_, sent_at) in list(recent_game_messages.items()):
        if now - sent_at > GAME_REPLY_STICKER_WINDOW:
            recent_game_messages.pop(key, None)


async def reply_with_game_sticker_if_needed(event):
    """Reply with sticker #34 when a group member replies to our recent game post."""
    message = event.message
    try:
        if getattr(message, "out", False):
            return
        reply_to_id = (
            getattr(message, "reply_to_msg_id", None)
            or getattr(getattr(message, "reply_to", None), "reply_to_msg_id", None)
        )
        if not reply_to_id:
            return
        chat_id = int(event.chat_id)
        key = (chat_id, int(reply_to_id))
        tracked = recent_game_messages.get(key)
        if not tracked:
            print(
                f"[REPLY STICKER] Unmatched reply | chat={chat_id} "
                f"| reply_to={reply_to_id} | tracked={len(recent_game_messages)}"
            )
            return
        event_type, sent_at = tracked
        age = time.monotonic() - sent_at
        if age > GAME_REPLY_STICKER_WINDOW:
            recent_game_messages.pop(key, None)
            print(f"[{event_type}] Reply window expired | message={reply_to_id} | age={age:.1f}s")
            return
        sticker = await get_rain_sticker_document(GAME_REPLY_STICKER_NUMBER - 1)
        sent = await user_client.send_file(chat_id, sticker, reply_to=int(message.id))
        print(
            f"[{event_type}] Reply sticker sent | chat={chat_id} "
            f"| original={reply_to_id} | replying_user_message={message.id} "
            f"| age={age:.1f}s | sticker_message={getattr(sent, 'id', None)}"
        )
    except Exception as e:
        print(
            f"[REPLY STICKER] Failed | chat={event.chat_id} "
            f"| message={getattr(message, 'id', None)} | {type(e).__name__}: {e}"
        )


rain_retry_worker_task = None
RAIN_RETRY_INTERVAL = 5.0
RAIN_RETRY_MAX_ATTEMPTS = 240  # Keep pending claims alive for up to 20 minutes so the 15-minute reminder can fire.


def rain_activity_gate_text(result):
    """Extract a callback answer's text without using OCR or faking chat activity."""
    parts = []
    candidates = [result]
    for attr in ("message", "text", "raw_text"):
        try:
            candidates.append(getattr(result, attr, None))
        except Exception:
            pass
    for candidate in candidates:
        if isinstance(candidate, str):
            parts.append(candidate)
            continue
        if candidate is None:
            continue
        for attr in ("message", "raw_text", "text"):
            value = getattr(candidate, attr, None)
            if isinstance(value, str):
                parts.append(value)
    return " ".join(parts).casefold()


def rain_has_activity_gate(result):
    text_value = rain_activity_gate_text(result)
    return (
        "say something in the chat first" in text_value
        or "people who have been here in the last 15 minutes" in text_value
    )


async def click_rain_grab(source_chat_id, message_id):
    """Click the source Grab button; return (status, response text)."""
    if message_id in processed_rain_completed_ids:
        return "already", "This Rain claim was already attempted."
    rain_message = await user_client.get_messages(source_chat_id, ids=message_id)
    if not rain_message:
        return "missing", "Rain post expired or unavailable."
    grab_position = None
    for row_index, row in enumerate(rain_message.buttons or []):
        for column_index, button in enumerate(row):
            if "grab" in (getattr(button, "text", "") or "").strip().casefold():
                grab_position = (row_index, column_index)
                break
        if grab_position:
            break
    if grab_position is None:
        return "no_button", "Grab button not found; Rain may have expired."
    result = await rain_message.click(*grab_position)
    response_text = rain_activity_gate_text(result).strip()
    if rain_has_activity_gate(result):
        return "activity_required", response_text
    processed_rain_completed_ids.add(message_id)
    return "clicked", response_text


async def rain_retry_worker():
    """Retry pending RAIN claims while Telegram's genuine-activity gate is pending."""
    while pending_rain_retries:
        for key, state in list(pending_rain_retries.items()):
            source_chat_id, message_id = key
            if state.get("retrying"):
                continue
            state["attempts"] += 1
            try:
                status, response_text = await click_rain_grab(source_chat_id, message_id)
                if status == "activity_required":
                    if state["attempts"] == 1 or state["attempts"] % 15 == 0:
                        print(f"[RAIN] Still waiting for genuine chat activity | source_message={message_id} | attempt={state['attempts']}")
                elif status in ("clicked", "already"):
                    pending_rain_retries.pop(key, None)
                    owner_message_id = state.get("owner_message_id")
                    if owner_message_id:
                        source_message = await user_client.get_messages(source_chat_id, ids=message_id)
                        if source_message:
                            updated_text = format_rain_alert(
                                source_message.raw_text or "", get_message_link(source_message),
                                bot_reply="The activity check cleared. NIKA clicked Grab."
                                + (f" Telegram replied: {response_text[:180]}" if response_text else ""),
                                status="Grab clicked",
                            )
                        else:
                            updated_text = f"🌧️ RAIN JOINED\nNIKA clicked Grab for source post #{message_id}.\n\n☀️ NIKA • WARRIOR OF LIBERATION"
                        await bot_client.edit_message(OWNER_ID, owner_message_id, updated_text)
                    print(f"[RAIN] Automatic retry succeeded | source_chat={source_chat_id} | source_message={message_id}")
                else:
                    pending_rain_retries.pop(key, None)
                    print(f"[RAIN] Automatic retry stopped | status={status} | source_message={message_id}")
            except Exception as e:
                print(f"[RAIN] Auto-retry error | source_message={message_id} | {type(e).__name__}: {e}")
                if state["attempts"] >= RAIN_RETRY_MAX_ATTEMPTS:
                    pending_rain_retries.pop(key, None)
            if state["attempts"] >= RAIN_RETRY_MAX_ATTEMPTS and key in pending_rain_retries:
                pending_rain_retries.pop(key, None)
                print(f"[RAIN] Auto-retry timed out after {RAIN_RETRY_MAX_ATTEMPTS} attempts | source_message={message_id}")
        if pending_rain_retries:
            await asyncio.sleep(RAIN_RETRY_INTERVAL)


async def retry_pending_rains_after_owner_activity(message):
    """Immediately retry pending RAIN claims after the account sends a genuine group message."""
    if not getattr(message, "out", False):
        return
    source_chat_id = int(getattr(message, "chat_id", 0) or 0)
    for key, state in list(pending_rain_retries.items()):
        if key[0] != source_chat_id or state.get("retrying"):
            continue
        state["retrying"] = True
        try:
            status, response_text = await click_rain_grab(source_chat_id, key[1])
            if status in ("clicked", "already"):
                pending_rain_retries.pop(key, None)
                owner_message_id = state.get("owner_message_id")
                if owner_message_id:
                    source_message = await user_client.get_messages(source_chat_id, ids=key[1])
                    if source_message:
                        updated_text = format_rain_alert(
                            source_message.raw_text or "", get_message_link(source_message),
                            bot_reply="Your recent group activity was detected. NIKA clicked Grab."
                            + (f" Telegram replied: {response_text[:180]}" if response_text else ""),
                            status="Grab clicked",
                        )
                    else:
                        updated_text = f"🌧️ RAIN JOINED\\nNIKA clicked Grab for source post #{key[1]}.\\n\\n☀️ NIKA • WARRIOR OF LIBERATION"
                    await bot_client.edit_message(OWNER_ID, owner_message_id, updated_text, buttons=None)
                print(f"[RAIN] Grab succeeded immediately after genuine owner activity | source_message={key[1]}")
            elif status != "activity_required":
                pending_rain_retries.pop(key, None)
                print(f"[RAIN] Activity-triggered retry stopped | status={status} | source_message={key[1]}")
        except Exception as e:
            print(f"[RAIN] Activity-triggered retry failed | source_message={key[1]} | {type(e).__name__}: {e}")
        finally:
            state["retrying"] = False


async def prompt_owner_for_rain_activity_after_delay(key, owner_message_id):
    """Prompt only for a specific RAIN that was blocked by Telegram activity requirements."""
    await asyncio.sleep(RAIN_ACTIVITY_PROMPT_DELAY)
    if key not in pending_rain_retries or not owner_message_id:
        return
    try:
        await bot_client.edit_message(
            OWNER_ID,
            owner_message_id,
            "🌧️ RAIN STILL WAITING · CHAT ACTIVITY REQUIRED\n\n"
            "Telegram has not accepted the account's recent activity for this RAIN. "
            "Please open the source group and send a normal message yourself. "
            "NIKA will retry Grab automatically after your message. "
            "A short message such as Hi/Hello can be deleted after 1 minute.",
            buttons=[[
                Button.url("📂 OPEN GROUP", f"https://t.me/{SOURCE_USERNAMES.get(key[0], SOURCE_USERNAME)}"),
                Button.inline("🔄 RETRY GRAB", data=f"rain_retry:{key[0]}:{key[1]}".encode()),
            ]],
        )
        print(f"[RAIN] Sent delayed activity prompt | source_message={key[1]}")
    except Exception as e:
        print(f"[RAIN] Delayed activity prompt failed | source_message={key[1]} | {type(e).__name__}: {e}")


def queue_rain_retry(source_chat_id, message_id, owner_message_id=None):
    global rain_retry_worker_task
    key = (int(source_chat_id), int(message_id))
    pending_rain_retries[key] = {"attempts": 0, "owner_message_id": owner_message_id}
    if owner_message_id:
        asyncio.create_task(prompt_owner_for_rain_activity_after_delay(key, owner_message_id))
    if rain_retry_worker_task is None or rain_retry_worker_task.done():
        rain_retry_worker_task = asyncio.create_task(rain_retry_worker())


async def get_rain_sticker_document(index=0):
    """Fetch and cache one sticker document from the configured Telegram sticker pack."""
    global _rain_sticker_set_cache
    if _rain_sticker_set_cache is None:
        sticker_set = await user_client(
            functions.messages.GetStickerSetRequest(
                stickerset=types.InputStickerSetShortName(short_name=RAIN_STICKER_PACK),
                hash=0,
            )
        )
        _rain_sticker_set_cache = list(getattr(sticker_set, "documents", []) or [])
    if not 0 <= index < len(_rain_sticker_set_cache):
        raise IndexError(
            f"Sticker #{index + 1} is unavailable in {RAIN_STICKER_PACK}; "
            f"pack contains {len(_rain_sticker_set_cache)} stickers."
        )
    return _rain_sticker_set_cache[index]


async def send_rain_activity_sticker(source_chat_id, rain_message_id, sticker_index=None):
    """Send a pack sticker without deleting it; index is zero-based when explicitly supplied."""
    if sticker_index is None:
        sticker_set = await user_client(
            functions.messages.GetStickerSetRequest(
                stickerset=types.InputStickerSetShortName(short_name=RAIN_STICKER_PACK),
                hash=0,
            )
        )
        documents = list(getattr(sticker_set, "documents", []) or [])
        if not documents:
            raise RuntimeError("The configured sticker pack is empty.")
        sticker = random.choice(documents)
    else:
        sticker = await get_rain_sticker_document(sticker_index)
    delay = random.uniform(RAIN_STICKER_DELAY_MIN, RAIN_STICKER_DELAY_MAX)
    await asyncio.sleep(delay)
    sent = await user_client.send_file(int(source_chat_id), sticker)
    remember_game_message(source_chat_id, sent, "RAIN")
    print(
        f"[RAIN] Sent sticker activity | rain_message={rain_message_id} "
        f"| sticker_index={sticker_index if sticker_index is not None else 'random'} "
        f"| delay={delay:.2f}s | sent_message={getattr(sent, 'id', None)} | kept=True"
    )
    return sent


async def rain_inactivity_activity_after_event(source_chat_id, rain_message_id):
    """Only a detected RAIN event can trigger a sticker after 15 minutes of owner inactivity."""
    global rain_inactivity_activity_task
    try:
        while True:
            idle_for = time.monotonic() - rain_last_owner_activity_at
            remaining = RAIN_INACTIVITY_SECONDS - idle_for
            if remaining <= 0:
                break
            await asyncio.sleep(min(remaining, 5))

        await send_rain_activity_sticker(source_chat_id, rain_message_id)
    except asyncio.CancelledError:
        raise
    except Exception as e:
        print(f"[RAIN] Inactivity sticker worker failed | rain_message={rain_message_id} | {type(e).__name__}: {e}")
    finally:
        rain_inactivity_activity_task = None


def queue_rain_inactivity_activity(source_chat_id, rain_message_id):
    """Start at most one inactivity activity timer, and only after a RAIN event."""
    global rain_inactivity_activity_task
    if rain_inactivity_activity_task is None or rain_inactivity_activity_task.done():
        rain_inactivity_activity_task = asyncio.create_task(
            rain_inactivity_activity_after_event(source_chat_id, rain_message_id)
        )


async def attempt_rain_grab(event, source_chat_id, message_id):
    """Owner retry after manually satisfying the source group's activity requirement."""
    status, response_text = await click_rain_grab(source_chat_id, message_id)
    if status == "activity_required":
        await event.edit(
            f"🌧️ CHAT ACTIVITY REQUIRED · #{message_id}\n"
            "Telegram requires genuine recent activity from your account. Send a normal message in the source group, "
            "then tap RETRY JOIN below. NIKA also keeps retrying automatically.",
            buttons=Button.inline("🔄 RETRY JOIN", data=f"rain_retry:{source_chat_id}:{message_id}".encode()),
        )
        await event.answer("Send a message in the group first, then tap JOIN RAIN.", alert=True)
        return
    if status == "clicked":
        await event.edit(f"🌧️ RAIN · #{message_id}\nGrab was clicked automatically." + (f"\n\nResponse: {response_text[:300]}" if response_text else ""))
        await event.answer("Rain Grab clicked.")
    else:
        await event.edit(f"🌧️ RAIN · #{message_id}\n{response_text or status}")
        await event.answer(response_text or status, alert=True)
    print(f"[RAIN] Retry result={status} | source_chat={source_chat_id} | source_message={message_id}")


@bot_client.on(events.CallbackQuery(pattern=rb"^rain_confirm:(-?\d+):(\d+)$"))
async def rain_confirm_callback(event):
    if event.sender_id != OWNER_ID:
        await event.answer("Owner-only control.", alert=True)
        return
    source_chat_id = int(event.pattern_match.group(1))
    message_id = int(event.pattern_match.group(2))
    try:
        await attempt_rain_grab(event, source_chat_id, message_id)
    except Exception as e:
        print(f"[RAIN] Grab check failed | source_message={message_id} | error={type(e).__name__}: {e}")
        await event.answer("Rain check failed; check Koyeb logs.", alert=True)


@bot_client.on(events.CallbackQuery(pattern=rb"^rain_retry:(-?\d+):(\d+)$"))
async def rain_retry_callback(event):
    if event.sender_id != OWNER_ID:
        await event.answer("Owner-only control.", alert=True)
        return
    source_chat_id = int(event.pattern_match.group(1))
    message_id = int(event.pattern_match.group(2))
    try:
        await attempt_rain_grab(event, source_chat_id, message_id)
    except Exception as e:
        print(f"[RAIN] Retry failed | source_message={message_id} | error={type(e).__name__}: {e}")
        await event.answer("Rain retry failed; check Koyeb logs.", alert=True)


# ---- Live GUESS crowd tracking -------------------------------------------------
# The winning number is random, but other players' numbers are public in the chat.
# "First number counts", so we keep each sender's FIRST bare number per round and
# re-pick a number that sits in a gap between them (see guess_best_response).
GUESS_REFRESH_DELAY = float(os.getenv("GUESS_REFRESH_DELAY", "8") or 8)
GUESS_ROUND_WINDOW = 60  # seconds after the round post during which numbers are collected
guess_crowd = {}         # round_message_id -> {sender_id: number}
guess_round_started = {} # round_message_id -> monotonic start time
active_guess_round = None
latest_game_round_ids = {}
BARE_NUMBER_RE = re.compile(r"^\s*(\d{1,3})\s*$")

def record_guess_number(message):
    """Store another player's first bare 1-100 number for the active GUESS round."""
    if active_guess_round is None or getattr(message, "out", False):
        return
    if int(getattr(message, "chat_id", 0) or 0) != int(active_guess_round[0]):
        return
    started = guess_round_started.get(active_guess_round)
    if started is None or time.monotonic() - started > GUESS_ROUND_WINDOW:
        return
    match = BARE_NUMBER_RE.match(getattr(message, "raw_text", None) or "")
    sender = getattr(message, "sender_id", None)
    if not match or sender is None:
        return
    number = int(match.group(1))
    if 1 <= number <= 100:
        guess_crowd[active_guess_round].setdefault(int(sender), number)  # first number counts

async def refresh_guess_alert(round_id, owner_message, header_text, fallback_prediction, source_chat_id=SOURCE_CHAT_ID):
    """After a short wait, replace the pick with one based on the numbers posted so far."""
    try:
        await asyncio.sleep(GUESS_REFRESH_DELAY)
        source_chat_id = int(source_chat_id)
        authorization_key = ("GUESS", source_chat_id, int(round_id))
        if authorization_key in processed_game_authorizations:
            return  # owner already sent a value
        others = list(guess_crowd.get((source_chat_id, int(round_id)), {}).values())
        if len(others) < 2:
            return
        players = int(os.getenv("BP_GUESS_PLAYERS", "0") or 0) or 12
        ranked, info = guess_best_response(others, players=players)
        best = ranked[0][0]
        alts = []
        for number, _ev, _win in ranked[1:]:
            if all(abs(number - x) >= 3 for x in [best] + alts):
                alts.append(number)
            if len(alts) == 2:
                break
        if connect_mongodb() and prediction_history_collection is not None:
            prediction_history_collection.update_one(
                scoped_query({"game": "GUESS", "source_message_id": int(round_id)}, source_chat_id),
                {"$set": {"prediction": int(best), "backups": alts,
                          "method": f"crowd-best-response others={len(others)} ev={info['best_ev']} win={info['best_win']}"}},
            )
        # Keep BOTH choices on screen: the original pick never disappears, so a late
        # tap still sends a sensible number instead of racing a changing value.
        source_chat_id = int(source_chat_id)
        buttons = [Button.inline(f"SEND {best} (LIVE)", data=f"guess_send:{source_chat_id}:{round_id}:{best}".encode())]
        if int(fallback_prediction) != best:
            buttons.insert(0, Button.inline(f"SEND {fallback_prediction} (FIRST)", data=f"guess_send:{source_chat_id}:{round_id}:{int(fallback_prediction)}".encode()))
        await owner_message.edit(
            header_text + "\n\n"
            f"🔄 LIVE UPDATE ({len(others)} players seen): {sorted(others)}\n"
            f"FIRST PICK: {fallback_prediction}\n"
            f"LIVE PICK: {best}  (win chance ≈ {info['best_win']*100:.0f}%, exp. prize ≈ {info['best_ev']})\n"
            f"ALTERNATES: {alts[0]} / {alts[1]}\nOnly one can be sent — first tap wins.",
            buttons=[buttons],
            link_preview=False,
        )
        print(f"[GUESS] Live update | round={round_id} | others={sorted(others)} | pick={best} | info={info}")
    except Exception as e:
        print(f"[GUESS] Live update failed: {type(e).__name__}: {e}")

SPEED_METRICS = {
    name: {"count": 0, "last_ms": None, "avg_ms": 0.0, "max_ms": 0.0, "last_at": None}
    for name in ("FLAG", "RAIN", "POT", "GUESS")
}

# Dashboard observability: bounded in-memory incident and latency history.
SPEED_HISTORY = []
INCIDENTS = []
MONITOR_STATES = {name: True for name in ("FLAG", "RAIN", "POT", "GUESS")}

def record_incident(source, message, severity="warning"):
    INCIDENTS.append({
        "at": datetime.now(timezone.utc).isoformat(),
        "source": str(source)[:40],
        "message": str(message)[:300],
        "severity": severity if severity in ("info", "warning", "error") else "warning",
    })
    del INCIDENTS[:-200]


def record_speed_metric(event_type, started_at):
    metric = SPEED_METRICS.get(event_type)
    if metric is None:
        return
    elapsed_ms = (time.perf_counter() - started_at) * 1000
    metric["count"] += 1
    metric["last_ms"] = round(elapsed_ms, 1)
    metric["avg_ms"] = round(metric["avg_ms"] + (elapsed_ms - metric["avg_ms"]) / metric["count"], 1)
    metric["max_ms"] = round(max(metric["max_ms"], elapsed_ms), 1)
    metric["last_at"] = datetime.now(LOCAL_TZ).strftime("%H:%M:%S")
    SPEED_HISTORY.append({"event": event_type, "ms": metric["last_ms"], "at": datetime.now(timezone.utc).isoformat()})
    del SPEED_HISTORY[:-500]

fast_alert_keys = set()

async def await_fast_notice(task, event_type):
    if task is None:
        return None
    try:
        return await task
    except Exception as e:
        print(f"[FAST ALERT] {event_type} notice failed: {type(e).__name__}: {e}")
        return None

async def process_source_alert(message):
    """Process recognized source events only from their configured trusted sender IDs."""
    global active_guess_round
    fast_notice = None  # Always create a fresh owner alert; never rely on an undefined cached message.
    speed_started_at = time.perf_counter()
    sender_id = getattr(message, "sender_id", None)
    raw_text = (getattr(message, "raw_text", None) or "").strip()
    if not raw_text:
        return
    event_type = detect_event_type(raw_text)
    # POT/GUESS are accepted only from the two trusted game bots; FLAG only from @mazemoneybot.
    if event_type in ("POT", "GUESS") and int(sender_id or 0) not in GAME_TRUSTED_SENDER_IDS:
        print(f"[{event_type}] Ignored untrusted sender | sender={sender_id} | chat={message.chat_id} | message={message.id}")
        return
    if event_type is None:
        if re.match(r"^\U0001F3AF\s*It was\s+\d{1,3}\b", raw_text.replace("\ufe0f", "")):
            if active_guess_round and active_guess_round[0] == int(message.chat_id):
                active_guess_round = None
        else:
            record_guess_number(message)
    if event_type == "GUESS":
        round_key = (int(message.chat_id), int(message.id))
        if round_key not in guess_crowd:
            active_guess_round = round_key
            guess_crowd[round_key] = {}
            guess_round_started[round_key] = time.monotonic()
            for old_key in list(guess_crowd)[:-10]:
                guess_crowd.pop(old_key, None); guess_round_started.pop(old_key, None)
    if event_type in ("POT", "GUESS"):
        latest_game_round_ids[(int(message.chat_id), event_type)] = int(message.id)
    if event_type not in ("POT", "GUESS", "RAIN", "FLAG"):
        # Result messages are recorded separately and never treated as new rounds.
        try:
            await save_game_result(message)
        except Exception as e:
            print(f"[RESULT] Handler error for source message {message.id}: {e}")
        return

    # Send the owner a fast receipt immediately. Prediction/database work must
    # never delay the first notification for a new POT/GUESS round.
    if event_type in ("POT", "GUESS"):
        alert_key = (int(message.chat_id), int(message.id), event_type)
        if alert_key in fast_alert_keys:
            return
        fast_alert_keys.add(alert_key)
        fast_notice = await bot_client.send_message(
            OWNER_ID,
            f"⚡ NIKA // {event_type} DETECTED\n"
            f"Preparing prediction…\n"
            f"Source: {get_message_link(message)}",
            link_preview=False,
        )

        # Keep persistent duplicate protection, but do not block the event loop.
        saved_existing = await asyncio.to_thread(
            get_saved_prediction, event_type, message.id, int(message.chat_id)
        )
        if saved_existing:
            try:
                await fast_notice.delete()
            except Exception:
                pass
            return

        # Approved-user DMs run independently so a slow recipient cannot delay
        # the owner's prediction or the source monitor.
        async def dispatch_approved_notice():
            try:
                await notify_approved_users(message, event_type)
            except Exception as e:
                print(f"[APPROVED] {event_type} notification dispatch error | source={message.chat_id} | message={message.id} | {type(e).__name__}: {e}")
        asyncio.create_task(dispatch_approved_notice())

    if event_type in ("POT", "GUESS"):
        alert_started_at = time.perf_counter()
        try:
            # Missed-result recovery runs at startup. Live results are handled
            # as they arrive, so avoid a 500-message scan before every alert.
            source_chat_id = int(message.chat_id)
            history = await asyncio.to_thread(get_all_model_history, event_type, source_chat_id)
            prediction, backups, method = await asyncio.to_thread(predict_number, event_type, history=history, source_chat_id=source_chat_id)
            await asyncio.to_thread(save_prediction, event_type, message.id, prediction, backups, method, history=history, source_chat_id=source_chat_id)
            saved = await asyncio.to_thread(get_saved_prediction, event_type, message.id, source_chat_id)
            if saved:
                prediction = int(saved["prediction"])
                backups = [int(n) for n in saved.get("backups", backups)]
                method = saved.get("method", method)
            while len(backups) < 2:
                candidate = random.randint(1, 100)
                if candidate != prediction and candidate not in backups:
                    backups.append(candidate)
            header_text = format_notification(message, event_type)
            if hunt_enabled:
                # Add a small randomized delay before HUNT submits to the source game.
                hunt_delay = random.uniform(2.0, 6.0)
                print(f"[HUNT] Waiting {hunt_delay:.2f}s before auto-submit | game={event_type} | source_message={message.id}")
                await asyncio.sleep(hunt_delay)
                sent, save_status = await auto_send_game_value(
                    int(message.chat_id), int(message.id), event_type, prediction,
                    save_last_pot_answer if event_type == "POT" else save_last_guess_answer,
                )
                final_text = (
                    header_text + "\n\n"
                    + (f"🟢 AUTO-SUBMITTED: {prediction}\n" if sent else f"⚠️ AUTO-SUBMIT FAILED: {save_status}\n")
                    + f"ALTERNATES: {backups[0]} / {backups[1]}\nMETHOD: {method}\n"
                    + "HUNT is active. Use /stop to return to manual confirmation."
                )
                if fast_notice:
                    await fast_notice.edit(final_text, link_preview=False)
                else:
                    await bot_client.send_message(OWNER_ID, final_text, link_preview=False)
                print(f"[{event_type}] Hunt result | sent={sent} | source_message={message.id}")
            else:
                callback = f"{event_type.lower()}_send:{int(message.chat_id)}:{message.id}:{prediction}".encode()
                final_text = (
                    header_text + "\n\n"
                    f"PRIMARY: {prediction}\nALTERNATES: {backups[0]} / {backups[1]}\n"
                    f"METHOD: {method}\nConfirm before sending."
                )
                if fast_notice:
                    await fast_notice.edit(final_text, buttons=Button.inline("AUTHORIZE VALUE", data=callback), link_preview=False)
                    sent_alert = fast_notice
                else:
                    sent_alert = await bot_client.send_message(
                        OWNER_ID, final_text,
                        buttons=Button.inline("AUTHORIZE VALUE", data=callback),
                        link_preview=False,
                    )
                # Live crowd override disabled: keep the initial Best Pick prediction.
            print(f"[{event_type}] Owner notification updated | source_message={message.id}")
            print(f"[{event_type}] Alert ready | source_message={message.id} | prediction={prediction} | method={method} | elapsed={time.perf_counter() - alert_started_at:.3f}s")
            record_speed_metric(event_type, speed_started_at)
        except Exception as e:
            print(f"[{event_type}] Failed to prepare prediction for source message {message.id}: {e}")
            record_incident(event_type, e, "error")
            if fast_notice:
                try:
                    await fast_notice.edit(
                        f"⚠️ NIKA // {event_type} PREDICTION FAILED\n"
                        f"{type(e).__name__}: {str(e)[:250]}\n"
                        f"Source: {get_message_link(message)}",
                        link_preview=False,
                    )
                except Exception:
                    pass
        return

    if event_type == "FLAG":
        country = detect_flag_country(raw_text)
        if country:
            flag_match = re.search(r"[\U0001F1E6-\U0001F1FF]{2}", raw_text)
            flag_emoji = flag_match.group(0) if flag_match else "🏳️"
            prize_match = re.search(r"(?:claims?|prize|reward)\s*(?:is|of|:)?\s*([\d,]+)", raw_text, re.IGNORECASE)
            timer_match = re.search(r"(\d+)\s*s(?:ec(?:ond)?s?)?\b", raw_text, re.IGNORECASE)
            prize_text = f"{prize_match.group(1)} points" if prize_match else "See source post"
            timer_text = f"{timer_match.group(1)} seconds" if timer_match else "See source post"
            final_text = (
                "🏳️ FLAG DETECTED\n\n"
                f"{flag_emoji} {country.title()}\n\n"
                f"🎯 Reward: {prize_text}\n"
                f"⏳ Time limit: {timer_text}\n\n"
                f"🔗 Source: {get_message_link(message)}\n\n"
                "⚡ ☀️ NIKA • WARRIOR OF LIBERATION\n\n"
                f"🏳️ IDENTIFIED: {country}\n"
                "Confirm to send?"
            )
            buttons = Button.inline("✅ CONFIRM: " + country, data=f"flag_send:{int(message.chat_id)}:{message.id}".encode())
        else:
            final_text = (
                "🏳️ FLAG NEEDS REVIEW\n\n"
                f"🔗 Source: {get_message_link(message)}\n\n"
                "⚡ ☀️ NIKA • WARRIOR OF LIBERATION\n"
                "No country could be identified automatically."
            )
            buttons = None

        # FLAG detection builds the alert above; explicitly deliver it to the owner.
        try:
            flag_notice = await bot_client.send_message(
                OWNER_ID, final_text, buttons=buttons, link_preview=False
            )
            print(
                f"[FLAG] Owner notified | source_chat={message.chat_id} "
                f"| source_message={message.id} | detected={bool(country)} "
                f"| notice={getattr(flag_notice, 'id', None)}"
            )
            record_speed_metric("FLAG", speed_started_at)
        except Exception as e:
            print(
                f"[FLAG] Owner notification failed | source_chat={message.chat_id} "
                f"| source_message={message.id} | {type(e).__name__}: {e}"
            )
            record_incident("FLAG", e, "error")
        return

    if event_type == "RAIN":
        if int(message.chat_id) == int(RAIN_ACTIVITY_CHAT_ID):
            queue_rain_inactivity_activity(int(message.chat_id), int(message.id))
        if message.id in processed_rain_alert_ids:
            return
        processed_rain_alert_ids.add(message.id)

        # Tell the owner immediately; the Telegram Grab-button operation can
        # take several network round trips and must not delay the alert.
        fast_notice = await bot_client.send_message(
            OWNER_ID,
            f"🌧️ NIKA // NEW RAIN DETECTED\n"
            f"Checking Grab status…\n"
            f"Source: {get_message_link(message)}",
            link_preview=False,
        )

        async def dispatch_rain_notice():
            try:
                await notify_approved_users(message, event_type)
            except Exception as e:
                print(f"[APPROVED] RAIN notification dispatch error | source={message.chat_id} | message={message.id} | {type(e).__name__}: {e}")
        asyncio.create_task(dispatch_rain_notice())

        source_chat_id = int(message.chat_id)
        status, response_text = await click_rain_grab(source_chat_id, message.id)
        if status == "activity_required":
            bot_reply = "Telegram requires genuine recent activity from your account in this group. Open the group and send a normal message; NIKA will keep retrying. You can also tap RETRY GRAB after sending it."
            status_text = "Waiting for chat activity"
            owner_buttons = [[
                Button.url("📂 OPEN GROUP", f"https://t.me/{SOURCE_USERNAMES.get(source_chat_id, SOURCE_USERNAME)}"),
                Button.inline("🔄 RETRY GRAB", data=f"rain_retry:{source_chat_id}:{message.id}".encode()),
            ]]
        elif status == "clicked":
            bot_reply = "NIKA clicked the Grab button." + (f" Telegram replied: {response_text[:180]}" if response_text else "")
            status_text = "Grab clicked"
            owner_buttons = None
        else:
            bot_reply = response_text or str(status)
            status_text = "Needs attention"
            owner_buttons = Button.inline("RETRY JOIN RAIN", data=f"rain_retry:{source_chat_id}:{message.id}".encode()) if status in ("missing", "no_button") else None
        owner_text = format_rain_alert(
            raw_text, get_message_link(message), bot_reply=bot_reply, status=status_text
        )
        if fast_notice:
            await fast_notice.edit(owner_text, buttons=owner_buttons, link_preview=False)
            owner_notice = fast_notice
        else:
            owner_notice = await bot_client.send_message(OWNER_ID, owner_text, buttons=owner_buttons, link_preview=False)
        if status == "activity_required":
            queue_rain_retry(source_chat_id, message.id, owner_notice.id)
            print(f"[RAIN] Queued 1-second automatic retry | source_chat={source_chat_id} | source_message={message.id}")
        print(f"[RAIN] Owner notified | status={status} | source_chat={source_chat_id} | source_message={message.id}")
        record_speed_metric("RAIN", speed_started_at)
        return


# RAIN Grab retries and RAIN-triggered inactivity activity are handled independently.


@user_client.on(events.NewMessage(chats=list(SOURCE_CHAT_IDS), incoming=True))
async def source_incoming_reply_handler(event):
    await reply_with_game_sticker_if_needed(event)


@user_client.on(events.NewMessage(chats=list(SOURCE_CHAT_IDS)))
async def source_new_message_handler(event):
    global rain_last_owner_activity_at
    if getattr(event.message, "out", False) and int(event.message.chat_id) == int(RAIN_ACTIVITY_CHAT_ID):
        rain_last_owner_activity_at = time.monotonic()
    await retry_pending_rains_after_owner_activity(event.message)
    await process_source_alert(event.message)

@user_client.on(events.MessageEdited(chats=list(SOURCE_CHAT_IDS)))
async def source_edited_message_handler(event):
    await process_source_alert(event.message)



# ---- Prize statistics (/gueststats-style commands) --------------------------------
USER_HANDLE_RE = re.compile(r"@?lokzai", re.IGNORECASE)

def parse_my_prize(result_text):
    """Points won by our account in a result message (first +N on our line), else 0."""
    for line in (result_text or "").splitlines():
        if USER_HANDLE_RE.search(line):
            match = re.search(r"\+\s*(\d+)", line[USER_HANDLE_RE.search(line).end():])
            return int(match.group(1)) if match else 0
    return 0

def summarize_rounds(rows, game):
    """Compact summary: POT wins mean the sent number is inside the result range; GUESS wins mean points were earned."""
    def is_win(row):
        if game == "POT":
            value = row.get("prediction")
            result_range = row.get("result_range")
            try:
                return (value is not None and isinstance(result_range, (list, tuple))
                        and len(result_range) == 2
                        and int(result_range[0]) <= int(value) <= int(result_range[1]))
            except (TypeError, ValueError):
                return False
        return int(row.get("prize", 0)) > 0

    def stats(items):
        n = len(items)
        won = sum(1 for row in items if is_win(row))
        return n, won

    groups = {"Overall": rows}
    for row in rows:
        key = "Live crowd" if str(row.get("method", "")).startswith("crowd-best-response") else "Old engine"
        groups.setdefault(key, []).append(row)

    lines = ["📊 PERFORMANCE"]
    for name in ("Overall", "Live crowd", "Old engine"):
        if name in groups:
            n, won = stats(groups[name])
            pct = won / n * 100 if n else 0
            lines.append(f"  {name:<12} {won}/{n:<5} {pct:>2.0f}%")
    total = sum(int(row.get("prize", 0)) for row in rows)
    avg = total / len(rows) if rows else 0.0
    lines.append(f"  Total        +{total} pts")
    lines.append(f"  Avg/round    +{avg:.1f}")
    return lines

async def prize_stats(event, game, source_chat_id=SOURCE_CHAT_ID, page=1, edit=False):
    if event.sender_id != OWNER_ID:
        if edit:
            await event.answer("Owner only.", alert=True)
        return
    if not connect_mongodb() or prediction_history_collection is None or result_history_collection is None:
        message = "⚠️ Stats unavailable: MongoDB is disconnected."
        if edit:
            await event.edit(message, buttons=None)
        else:
            await event.reply(message)
        return
    try:
        # Load every resolved round; pagination is only for display, not data retrieval.
        preds = list(prediction_history_collection.find(
            scoped_query({"game": game, "resolved": True}, source_chat_id),
            {"_id": 0, "source_message_id": 1, "prediction": 1, "actual": 1, "method": 1,
             "result_range": 1, "created_at": 1, "source_chat_id": 1}
        ).sort("created_at", DESCENDING))
        ids = [int(p["source_message_id"]) for p in preds]
        results = {}
        for offset in range(0, len(ids), 500):
            batch_ids = ids[offset:offset + 500]
            for doc in result_history_collection.find(
                    scoped_query({"game": game, "round_source_message_id": {"$in": batch_ids}}, source_chat_id),
                    {"_id": 0, "round_source_message_id": 1, "result_text": 1, "actual": 1,
                     "result_range": 1, "user_won": 1, "source_chat_id": 1}):
                results[(int(doc.get("source_chat_id", SOURCE_CHAT_ID)), int(doc["round_source_message_id"]))] = doc
    except Exception as e:
        print(f"[STATS] {game} query failed: {e}")
        message = "⚠️ Could not load stats. Check logs."
        if edit:
            await event.edit(message, buttons=None)
        else:
            await event.reply(message)
        return
    rows = []
    for p in preds:
        res = results.get((int(p.get("source_chat_id", SOURCE_CHAT_ID)), int(p["source_message_id"])))
        if not res:
            continue
        result_text = res.get("result_text", "")
        user_won = res.get("user_won")
        if user_won is None:
            user_won = bool(re.search(r"(?<![\w])@?lokzai(?![\w])", result_text, re.IGNORECASE))
        rows.append({"prediction": p.get("prediction"), "actual": res.get("actual", p.get("actual")),
                     "result_range": res.get("result_range") or p.get("result_range"),
                     "prize": parse_my_prize(result_text), "user_won": bool(user_won),
                     "method": p.get("method", "")})
    if not rows:
        message = f"📈 {game} STATS\nNo scored rounds yet."
        if edit:
            await event.edit(message, buttons=None)
        else:
            await event.reply(message)
        return

    page_size = 10
    total_pages = max(1, (len(rows) + page_size - 1) // page_size)
    page = max(1, min(int(page), total_pages))
    page_rows = rows[(page - 1) * page_size:page * page_size]
    out = [f"📈 {game} STATS · {source_name(source_chat_id)} · {len(rows)} ROUNDS", "━━━━━━━━━━━━━━━━━━━━"]
    out += summarize_rounds(rows, game)

    out += ["", f"🕒 ROUNDS (newest first) · PAGE {page}/{total_pages}"]
    if game == "POT":
        out += ["  #   SENT   WIN RANGE   PRIZE", " ─────────────────────────────"]
        for i, row in enumerate(page_rows, (page - 1) * page_size + 1):
            result_range = row.get("result_range")
            valid_range = isinstance(result_range, (list, tuple)) and len(result_range) == 2
            try:
                range_text = f"{int(result_range[0])}–{int(result_range[1])}" if valid_range else "—"
                in_range = valid_range and int(result_range[0]) <= int(row["prediction"]) <= int(result_range[1])
            except (TypeError, ValueError, KeyError):
                range_text, in_range = "—", False
            status = "✅" if in_range else "❌"
            out.append(f"{i:>3}  {str(row['prediction']):>4}   {range_text:<8} {status}   +{int(row['prize'])}")
        out += ["", "✅ = sent inside win range"]
    else:
        out += ["  #   SENT   ACTUAL   PRIZE", " ─────────────────────────────"]
        for i, row in enumerate(page_rows, (page - 1) * page_size + 1):
            prize = int(row.get("prize", 0))
            badge = "🏆" if prize > 0 else ""
            out.append(f"{i:>3}  {str(row['prediction']):>4}  {str(row['actual']):>6}  {badge:>3}  +{prize}")
        out += ["", "🏆 = points earned"]
    out += [f"📄 Showing {len(page_rows)} of {len(rows)} saved rounds",
            "💡 Judge long-term results after 50–100 rounds"]

    nav = []
    if page > 1:
        nav.append(Button.inline("◀️ Previous", data=f"stats:{game}:{source_chat_id}:{page - 1}".encode()))
    if page < total_pages:
        nav.append(Button.inline("Next ▶️", data=f"stats:{game}:{source_chat_id}:{page + 1}".encode()))
    buttons = [nav] if nav else None
    message = "\n".join(out)
    if edit:
        await event.edit(message, buttons=buttons)
        await event.answer()
    else:
        await event.reply(message, buttons=buttons)


@bot_client.on(events.NewMessage(pattern=r"^/(guess|pot)stats(?:@\w+)?(?:\s+(mazes|mazemoney))?$"))
async def prize_stats_handler(event):
    game = event.pattern_match.group(1).upper()
    source_arg = (event.pattern_match.group(2) or "").lower()
    if not source_arg:
        await event.reply(
            f"📊 Select {game} source data:",
            buttons=[[
                Button.inline("MAZEMONEY", data=f"stats_source:{game}:{-1003929450754}:1".encode()),
                Button.inline("MAZES", data=f"stats_source:{game}:{-1003707950453}:1".encode()),
            ]],
        )
        return
    source_chat_id = next((chat_id for chat_id, label in SOURCE_USERNAMES.items()
                           if label.casefold() == source_arg), SOURCE_CHAT_ID)
    await prize_stats(event, game, source_chat_id=source_chat_id, page=1)


@bot_client.on(events.CallbackQuery(pattern=rb"^stats_source:(POT|GUESS):(-?\d+):(\d+)$"))
async def prize_stats_source_callback(event):
    game = event.pattern_match.group(1).decode("ascii")
    source_chat_id = int(event.pattern_match.group(2))
    page = int(event.pattern_match.group(3))
    await prize_stats(event, game, source_chat_id=source_chat_id, page=page, edit=True)


@bot_client.on(events.CallbackQuery(pattern=rb"^stats:(POT|GUESS):(-?\d+):(\d+)$"))
async def prize_stats_page_callback(event):
    game = event.pattern_match.group(1).decode("ascii")
    source_chat_id = int(event.pattern_match.group(2))
    page = int(event.pattern_match.group(3))
    await prize_stats(event, game, source_chat_id=source_chat_id, page=page, edit=True)


def verify_telegram_webapp_owner(init_data):
    """Validate Telegram Mini App initData and require the configured owner."""
    if not BOT_TOKEN or not init_data:
        return False
    try:
        fields = dict(parse_qsl(init_data, keep_blank_values=True))
        received_hash = fields.pop("hash", "")
        auth_date = int(fields.get("auth_date", "0"))
        if not received_hash or abs(time.time() - auth_date) > 86400:
            return False
        data_check = "\n".join(f"{key}={value}" for key, value in sorted(fields.items()))
        secret_key = hmac.new(b"WebAppData", BOT_TOKEN.encode(), hashlib.sha256).digest()
        calculated = hmac.new(secret_key, data_check.encode(), hashlib.sha256).hexdigest()
        if not hmac.compare_digest(calculated, received_hash):
            return False
        user = json.loads(fields.get("user", "{}"))
        return int(user.get("id", 0)) == int(OWNER_ID)
    except Exception:
        return False


async def health_server():
    port = env_int("PORT", 8000)

    async def handle(reader, writer):
        global hunt_enabled
        try:
            request_head = await reader.readuntil(b"\r\n\r\n")
            header_text = request_head.decode("iso-8859-1", errors="ignore")
            first_line = header_text.split("\r\n", 1)[0]
            parts = first_line.split(" ")
            method = parts[0].upper() if parts else "GET"
            request_target = parts[1] if len(parts) >= 2 else "/"
            path = request_target.split("?", 1)[0]
            headers = {}
            for header_line in header_text.split("\r\n")[1:]:
                if ":" in header_line:
                    key, value = header_line.split(":", 1)
                    headers[key.strip().lower()] = value.strip()
            content_length = min(16384, max(0, int(headers.get("content-length", "0") or 0)))
            request_body = await reader.readexactly(content_length) if method == "POST" and content_length else b""
            status_code = 200
            status_text = "OK"
            if path == "/health":
                body = b'{"status":"ok","service":"nika-joyboy"}'
                content_type = b"application/json"
            elif path == "/api/ping":
                started = time.perf_counter()
                telegram_ok = False
                telegram_latency_ms = None
                try:
                    await bot_client.get_me()
                    telegram_latency_ms = round((time.perf_counter() - started) * 1000)
                    telegram_ok = True
                except Exception as e:
                    print(f"[PING] Telegram API check failed: {e}")
                    record_incident("TELEGRAM", e, "error")
                database_ok = False
                try:
                    database_ok = await asyncio.to_thread(connect_mongodb)
                except Exception as e:
                    print(f"[PING] Database check failed: {e}")
                    record_incident("MONGODB", e, "error")
                payload = {
                    "telegram_ok": telegram_ok,
                    "telegram_latency_ms": telegram_latency_ms,
                    "database_ok": database_ok,
                    "user_session_ok": user_client.is_connected(),
                    "uptime_seconds": round(time.monotonic() - PROCESS_STARTED_AT),
                    "checked_at": datetime.now(timezone.utc).isoformat(),
                }
                body = json.dumps(payload).encode("utf-8")
                content_type = b"application/json; charset=utf-8"
            elif path == "/api/ops":
                payload = {
                    "monitors": MONITOR_STATES,
                    "active_rounds": {
                        "POT": {source_name(chat_id): latest_game_round_ids.get((chat_id, "POT")) for chat_id in SOURCE_CHAT_IDS},
                        "GUESS": {source_name(chat_id): latest_game_round_ids.get((chat_id, "GUESS")) for chat_id in SOURCE_CHAT_IDS},
                        "GUESS_PLAYERS_SEEN": len(guess_crowd.get(active_guess_round, {})) if active_guess_round is not None else 0,
                    },
                    "approved_users_count": len(approved_users),
                    "pending_access_requests": len(get_access_requests(100)),
                    "daily_earnings": get_daily_earnings(),
                    "approved_usernames": get_approved_usernames(),
                    "speed": SPEED_METRICS,
                    "recent_events": list(reversed(SPEED_HISTORY[-30:])),
                    "incidents": list(reversed(INCIDENTS[-30:])),
                    "uptime_seconds": round(time.monotonic() - PROCESS_STARTED_AT),
                    "checked_at": datetime.now(timezone.utc).isoformat(),
                }
                body = json.dumps(payload).encode("utf-8")
                content_type = b"application/json; charset=utf-8"
            elif path == "/api/archive":
                from urllib.parse import parse_qs
                query = parse_qs(urlsplit(parts[1].decode(errors="ignore")).query)
                term = (query.get("q", [""])[0] or "").strip()[:100]
                game = (query.get("game", ["ALL"])[0] or "ALL").upper()
                limit = min(100, max(1, int((query.get("limit", ["30"])[0] or "30"))))
                if game not in ("ALL", "POT", "GUESS", "RAIN", "FLAG"):
                    game = "ALL"
                items = []
                if connect_mongodb():
                    if game in ("ALL", "POT", "GUESS") and result_history_collection is not None:
                        db_filter = {"game": game} if game in ("POT", "GUESS") else {}
                        docs = result_history_collection.find(db_filter, {"_id": 0, "game": 1, "result_text": 1, "actual": 1, "result_range": 1, "created_at": 1, "source_message_id": 1}).sort("created_at", DESCENDING).limit(500)
                        for doc in docs:
                            raw = str(doc.get("result_text", ""))
                            if term and term.casefold() not in raw.casefold() and term not in str(doc.get("actual", "")) and term not in str(doc.get("source_message_id", "")):
                                continue
                            created = doc.get("created_at")
                            items.append({"game": doc.get("game"), "result_text": raw[:2000], "actual": doc.get("actual"), "result_range": doc.get("result_range"), "source_message_id": doc.get("source_message_id"), "created_at": created.isoformat() if created else None})
                    if game in ("ALL", "RAIN", "FLAG") and daily_earnings_collection is not None:
                        earn_filter = {"game": game} if game in ("RAIN", "FLAG") else {"game": {"$in": ["RAIN", "FLAG"]}}
                        docs = daily_earnings_collection.find(earn_filter, {"_id": 0, "game": 1, "raw_text": 1, "points": 1, "created_at": 1, "source_message_id": 1}).sort("created_at", DESCENDING).limit(500)
                        for doc in docs:
                            raw = str(doc.get("raw_text", ""))
                            if term and term.casefold() not in raw.casefold() and term not in str(doc.get("points", "")) and term not in str(doc.get("source_message_id", "")):
                                continue
                            created = doc.get("created_at")
                            items.append({"game": doc.get("game"), "result_text": raw[:2000], "actual": None, "points": doc.get("points"), "source_message_id": doc.get("source_message_id"), "created_at": created.isoformat() if created else None})
                    items.sort(key=lambda item: item.get("created_at") or "", reverse=True)
                    items = items[:limit]
                payload = {"items": items, "query": term, "game": game, "database_ok": bool(connect_mongodb())}
                body = json.dumps(payload).encode("utf-8")
                content_type = b"application/json; charset=utf-8"
            elif path == "/api/monitor" and method == "GET":
                payload = {"monitors": MONITOR_STATES, "hunt_enabled": hunt_enabled,
                           "controls_enabled": True}
                body = json.dumps(payload).encode("utf-8")
                content_type = b"application/json; charset=utf-8"
            elif path == "/api/control" and method == "POST":
                if not verify_telegram_webapp_owner(headers.get("x-telegram-init-data", "")):
                    status_code, status_text = 403, "Forbidden"
                    payload = {"ok": False, "message": "Owner verification failed. Open this app from the owner's Telegram account."}
                else:
                    try:
                        command = json.loads(request_body.decode("utf-8"))
                        action = str(command.get("action", "")).lower()
                        if action == "monitor":
                            game = str(command.get("game", "")).upper()
                            if game not in MONITOR_STATES:
                                raise ValueError("Unknown monitor.")
                            MONITOR_STATES[game] = bool(command.get("enabled"))
                            payload = {"ok": True, "monitors": MONITOR_STATES, "message": f"{game} monitor {'enabled' if MONITOR_STATES[game] else 'paused'}."}
                        elif action == "hunt":
                            hunt_manual_enabled = bool(command.get("enabled"))
                            refresh_effective_hunt_state()
                            payload = {"ok": True, "hunt_enabled": hunt_enabled, "message": f"Manual HUNT {'enabled' if hunt_manual_enabled else 'disabled'}; effective auto-submit {'enabled' if hunt_enabled else 'disabled'}."}
                        else:
                            raise ValueError("Unsupported control action.")
                    except (ValueError, TypeError, json.JSONDecodeError) as exc:
                        status_code, status_text = 400, "Bad Request"
                        payload = {"ok": False, "message": str(exc)}
                body = json.dumps(payload).encode("utf-8")
                content_type = b"application/json; charset=utf-8"
            elif path == "/api/speed":
                payload = {
                    "metrics": SPEED_METRICS,
                    "uptime_seconds": round(time.monotonic() - PROCESS_STARTED_AT),
                    "checked_at": datetime.now(timezone.utc).isoformat(),
                }
                body = json.dumps(payload).encode("utf-8")
                content_type = b"application/json; charset=utf-8"
            elif path == "/ping-app":
                try:
                    with open(os.path.join(os.path.dirname(__file__), "ping.html"), "rb") as page:
                        body = page.read()
                    content_type = b"text/html; charset=utf-8"
                except Exception as e:
                    print(f"[PING] Dashboard file could not be read: {e}")
                    body = "NIKA • JOY BOY dashboard is not installed.".encode("utf-8")
                    content_type = b"text/plain; charset=utf-8"
            elif path == "/logs":
                pot_logs = get_recent_results("POT", 4) or []
                guess_logs = get_recent_results("GUESS", 4) or []

                def render_results(items):
                    items = items[:4]
                    if not items:
                        return "<p>No completed results recorded yet.</p>"
                    cards = []
                    for index, item in enumerate(items, 1):
                        result = (item.get("result_text") or "").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
                        created = item.get("created_at")
                        when = created.astimezone(timezone.utc).strftime("%d %b %Y %H:%M UTC") if created else "time unknown"
                        cards.append(f"<article><div class='meta'>#{index} · {when}</div><pre>{result}</pre></article>")
                    return "".join(cards)

                html = (
                    "<!doctype html><html><head><meta charset='utf-8'>"
                    "<meta name='viewport' content='width=device-width,initial-scale=1'>"
                    "<title>NIKA • JOY BOY · Variant Archives</title><style>"
                    "body{font:15px ui-monospace,SFMono-Regular,monospace;background:radial-gradient(ellipse at top,#303a25 0%,#1b2117 55%,#11140e 100%);color:#e5e8d0;max-width:1000px;margin:0 auto;padding:24px 16px;min-height:100vh}"
                    "h1{font:900 28px system-ui,sans-serif;letter-spacing:2px;color:#c5d99a;text-shadow:0 0 18px #94b36a33}p{color:#a8b18d}section{background:rgba(28,32,22,.94);border:1px solid #596b43;border-radius:8px;padding:18px;margin:20px 0;box-shadow:0 10px 32px #0004}h2{font:700 16px system-ui,sans-serif;letter-spacing:2px;color:#d4c58a;text-transform:uppercase}article{border:1px solid #414c32;border-left:3px solid #a9c77b;background:#171b13;padding:14px;margin:12px 0}article:nth-child(even){border-left-color:#d5a45d}.meta{font-size:12px;color:#b5bd8d;margin-bottom:9px}pre{white-space:pre-wrap;overflow-wrap:anywhere;font:inherit;margin:0;color:#e5e8d0}footer{border-top:1px solid #414c32;color:#929c78;padding-top:14px;font-size:11px;letter-spacing:1px}"
                    "</style></head><body><h1>NIKA • JOY BOY // VARIANT ARCHIVES</h1>"
                    "<p>Last 4 completed result posts per game · times in UTC</p>"
                    "<section><h2>POT</h2>" + render_results(pot_logs) +
                    "</section><section><h2>GUESS</h2>" + render_results(guess_logs) +
                    "</section><footer>☀️ NIKA // JOY BOY'S LOGBOOK</footer></body></html>"
                )
                body = html.encode("utf-8")
                content_type = b"text/html; charset=utf-8"
            else:
                body = "NIKA • JOY BOY temporal services are operational.".encode("utf-8")
                content_type = b"text/plain; charset=utf-8"

            response = (
                f"HTTP/1.1 {status_code} {status_text}\r\n".encode()
                + b"Content-Type: " + content_type + b"\r\n"
                + b"Content-Length: " + str(len(body)).encode()
                + b"\r\nConnection: close\r\n\r\n" + body
            )
            writer.write(response)
            await writer.drain()
        except asyncio.IncompleteReadError:
            # Health probes or clients may connect and close without sending
            # a complete HTTP request. This is normal; do not spam tracebacks.
            pass
        except (ConnectionError, BrokenPipeError):
            # The remote peer disconnected before the response was sent.
            pass
        finally:
            writer.close()
            try:
                await writer.wait_closed()
            except (ConnectionError, BrokenPipeError):
                pass

    server = await asyncio.start_server(handle, "0.0.0.0", port)
    print(f"HTTP health server listening on 0.0.0.0:{port}")
    return server

async def main():
    missing = [
        name
        for name, value in (
            ("API_ID", API_ID),
            ("API_HASH", API_HASH),
            ("BOT_TOKEN", BOT_TOKEN),
            ("TELETHON_SESSION", TELETHON_SESSION),
            ("OWNER_ID", OWNER_ID),
        )
        if not value
    ]

    if missing:
        raise RuntimeError("Missing environment variables: " + ", ".join(missing))

    load_approved_users()
    load_hunt_timer_config()
    load_sticker_pack_name()
    if connect_mongodb():
        print(
            f"[DB] Ready | last_pot_answer={load_last_pot_answer()} "
            f"| last_guess_answer={load_last_guess_answer()}"
        )
    else:
        print("[DB] WARNING: MongoDB unavailable. Approved users and last Pot answer will not persist.")

    # Start the health endpoint before Telegram authorization so Koyeb can keep
    # the instance healthy while Telegram's temporary authorization rate limit expires.
    health = await health_server()

    print("Starting bot...")
    while True:
        try:
            await bot_client.start(bot_token=BOT_TOKEN)
            break
        except FloodWaitError as e:
            wait_seconds = max(1, int(e.seconds)) + 5
            print(
                f"[AUTH] Telegram rate limit while authorizing bot token. "
                f"Waiting {wait_seconds}s before retry; do not restart the service."
            )
            await bot_client.disconnect()
            await asyncio.sleep(wait_seconds)
        except Exception:
            health.close()
            await health.wait_closed()
            raise

    print("Starting Telegram user account...")
    try:
        await user_client.start()
    except AuthKeyDuplicatedError as e:
        print(
            "FATAL: TELETHON_SESSION was used from another active connection. "
            "Stop every other deployment/local bot using this session. "
            "If the conflict continues, generate a fresh TELETHON_SESSION "
            "after all old instances are stopped, then update the environment variable."
        )
        await bot_client.disconnect()
        health.close()
        await health.wait_closed()
        raise RuntimeError("Duplicate Telethon user session; bot startup aborted.") from e

    me = await user_client.get_me()
    await backfill_recent_results(500)
    bot_me = await bot_client.get_me()

    print("=" * 40)
    print("       NIKA • JOY BOY")
    print("=" * 40)
    print(f"User account : {me.id}")
    print(f"Bot          : @{bot_me.username}")
    print(f"Sources      : {sorted(SOURCE_CHAT_IDS)}")
    print(f"Owner        : {OWNER_ID}")
    print("-" * 40)
    print("POT / GUESS / RAIN monitoring is active.")
    print("=" * 40)

    try:
        hunt_timer_task = asyncio.create_task(hunt_timer_worker())
        await asyncio.gather(
            user_client.run_until_disconnected(),
            bot_client.run_until_disconnected(),
            hunt_timer_task,
        )
    finally:
        health.close()
        await health.wait_closed()

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
