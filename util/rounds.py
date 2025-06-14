# util/rounds.py
"""
Very small helper that runs two 2-minute rounds, swaps taggers,
shows a 5-second countdown banner, then declares the winner.

All state is kept in-memory (round_state) plus MongoDB’s players
list for tagger / team info.
"""

import random, time
from threading import Timer
from typing     import Dict
from flask_socketio import SocketIO

# ─── Tunables ────────────────────────────────────────────
ROUND_TIME_SEC = 60          # 2-minute rounds
PAUSE_BETWEEN  = 5            # 5-second prep banner
MAX_ROUNDS     = 2

# ─── In-memory tracker:  room_id → {"round": int, "taggers": "red"/"blue"} ──
round_state: Dict[str, Dict] = {}

# ─── Public entry-point ─────────────────────────────────
def kick_off_round_system(sock: SocketIO, room_collection, room_id: str) -> None:
    """Call once, right after the owner presses ‘Start Game’."""
    first_taggers = random.choice(["red", "blue"])
    round_state[room_id] = {"round": 1, "taggers": first_taggers}

    _flag_taggers_in_db(room_collection, room_id, first_taggers)

    # ✨ Set initial attacking_team immediately
    room_collection.update_one(
        {'id': room_id},
        {'$set': {'attacking_team': first_taggers}}
    )

    _start_round(sock, room_id, room_collection)


# ─── Internal helpers ───────────────────────────────────
def _flag_taggers_in_db(room_collection, room_id: str, taggers: str) -> None:
    """Set players.$[].is_tagger = True / False based on chosen colour."""
    room = room_collection.find_one({"id": room_id})
    if not room:
        return

    new_players = []
    for p in room.get("players", []):
        p["is_tagger"] = (p.get("team") == taggers)
        new_players.append(p)

    room_collection.update_one({"id": room_id},
                               {"$set": {"players": new_players}})

def _start_round(sock: SocketIO, room_id: str, room_collection) -> None:
    s = round_state[room_id]

    # ── 5-second pre-start countdown ────────────────────
    for sec in range(PAUSE_BETWEEN, 0, -1):
        Timer(PAUSE_BETWEEN - sec,
              lambda x=sec: sock.emit('round_prep',
                                       {"seconds": x,
                                        "next_round": s["round"],
                                        "taggers":   s["taggers"]},
                                       room=room_id,
                                       namespace='/battlefield')).start()

    # ── Real start after PAUSE_BETWEEN seconds ──────────
    def _fire_start():
        sock.emit('round_start',
                  {"round":     s["round"],
                   "taggers":   s["taggers"],
                   "duration":  ROUND_TIME_SEC},
                  room=room_id, namespace='/battlefield')
        # schedule round end
        Timer(ROUND_TIME_SEC, _end_round,
              args=(sock, room_id, room_collection)).start()

    Timer(PAUSE_BETWEEN, _fire_start).start()

def _end_round(sock: SocketIO, room_id: str, room_collection) -> None:
    s = round_state[room_id]

    room = room_collection.find_one({'id': room_id}) or {}
    red  = sum(1 for p in room.get('players', []) if p.get('team') == "red")
    blue = sum(1 for p in room.get('players', []) if p.get('team') == "blue")
    winner = "draw"
    if   red  > blue: winner = "red"
    elif blue > red:  winner = "blue"

    sock.emit('round_end',
              {"round": s["round"], "winner": winner},
              room=room_id, namespace='/battlefield')

    if s["round"] >= MAX_ROUNDS:
        sock.emit('match_over',
                  {"winner": winner, "red": red, "blue": blue},
                  room=room_id, namespace='/battlefield')

        # ✅ UPDATE USER WINS
        if winner in ["red", "blue"]:
            winners = [p['id'] for p in room.get('players', []) if p.get('team') == winner]
            for uid in winners:
                room_collection.database['users'].update_one(  # ⚠️ adjust to your actual user collection
                    {"username": uid},
                    {"$inc": {"wins": 1}}
                )
            sock.emit('leaderboard_updated', namespace='/lobby')

        # 🔥 Cleanup room
        room_collection.delete_one({'id': room_id})
        round_state.pop(room_id, None)
        return

    # flip taggers for next round
    s["taggers"] = "blue" if s["taggers"] == "red" else "red"
    _flag_taggers_in_db(room_collection, room_id, s["taggers"])

    # ✨ sync attacking_team in MongoDB too
    room_collection.update_one(
        {'id': room_id},
        {'$set': {'attacking_team': s["taggers"]}}
    )

    # bump round counter and start next
    s["round"] += 1
    _start_round(sock, room_id, room_collection)


