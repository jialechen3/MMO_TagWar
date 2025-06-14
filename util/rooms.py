import uuid
import html

from flask_socketio import emit, join_room
from flask import request
from util.auth import hash_token
from bson import ObjectId
from util.rounds import kick_off_round_system


connected_users = {}

def choose_avatar(username, room_doc, user_doc):
    """
    Return an avatar filename (no leading /static/ part).
    • If the user uploaded one (user_doc["avatar"]), use it.
    • Otherwise return team default PNG.
    """
    if user_doc.get("avatar"):
        return user_doc["avatar"]
    if username in room_doc["red_team"]:
        return "defaultRedTeamPNG.png"
    if username in room_doc["blue_team"]:
        return "defaultBlueTeamPNG.png"

def enrich_with_avatars(room_doc, user_coll):
    """
    Return a fresh list of player dicts, each with an .avatar key that is
    **just the filename** (no /static/ prefix).  Front-end prepends that.
    """
    players_out = []
    for p in room_doc.get("players", []):
        uid       = p["id"]
        user_doc  = user_coll.find_one({"username": uid}) or {}
        avatar_fn = choose_avatar(uid, room_doc, user_doc)
        players_out.append({**p, "avatar": avatar_fn})
    return players_out


def register_room_handlers(socketio, user_collection, room_collection):

    def _emit_team_counts(room_id: str):
        """
        Broadcast {"red": <int>, "blue": <int>} to everyone in <room_id>.
        """
        room = room_collection.find_one(
            {"id": room_id},
            {"red_team": 1, "blue_team": 1, "_id": 0}
        )
        if not room:
            return

        socketio.emit(
            'team_counts',
            {
                "red": len(room.get("red_team", [])),
                "blue": len(room.get("blue_team", [])),
            },
            room=room_id,
            namespace='/lobby'
        )

    @socketio.on('create_room', namespace='/lobby')
    def handle_create_room(room_name):
        auth_token = request.cookies.get('auth_token')
        if not auth_token:
            return

        user = user_collection.find_one({'auth_token': hash_token(auth_token)})
        if not user:
            return

        username = user['username']
        room_id = str(uuid.uuid4())

        # 🔥 Generate randomized terrain
        generated_terrain = generate_battlefield_terrain()

        new_room = {
            "id": room_id,
            "room_name": room_name,
            "owner": username,
            "red_team": [],
            "blue_team": [],
            "no_team": [],
            "players": [],
            "game_started": False,
            "terrain": generated_terrain  # 🔥 store it in MongoDB
        }
        room_collection.insert_one(new_room)

        all_rooms = [
            {"id": str(room["id"]), "name": html.escape(room["room_name"])}
            for room in room_collection.find({"game_started": False})
        ]
        emit('room_list', all_rooms, broadcast=True)


    @socketio.on('get_rooms', namespace='/lobby')
    def handle_get_rooms():
        all_rooms = [
            {"id": str(room["id"]), "name": html.escape(room["room_name"])}
            for room in room_collection.find({"game_started": False})
        ]
        emit('room_list', all_rooms)

    @socketio.on('join_room', namespace='/lobby')
    def handle_join_room(data):
        room_id = data.get('room_id')  # or 'roomId' depending on your frontend

        join_room(room_id)

    @socketio.on('page_ready', namespace='/lobby')
    def handle_page_ready(data):
        room_id = data.get('room_id')
        page = data.get('page')
        if page == 'create_lobby':
            handle_get_rooms()
            return

        if page == 'team_select' and room_id:
            auth_token = request.cookies.get('auth_token')
            user = user_collection.find_one({'auth_token': hash_token(auth_token)})
            if not user:
                return

            username = user['username']

            # 🔥 Disconnect cleanup for the same user (see next section)
            for sid, name in list(connected_users.items()):
                if name == username:
                    connected_users.pop(sid)

            connected_users[request.sid] = username
            join_room(room_id)  # <-- 🔥 this is the missing key!

            room = room_collection.find_one({"id": room_id})
            if not room:
                return

            if username not in room["red_team"] + room["blue_team"] + room["no_team"]:
                room_collection.update_one(
                    {"id": room_id},
                    {"$push": {"no_team": username}}
                )

            updated = room_collection.find_one({"id": room_id})
            emit('team_red_list', updated["red_team"], room=room_id)
            emit('team_blue_list', updated["blue_team"], room=room_id)
            emit('no_team_list', updated["no_team"], room=room_id)
            _emit_team_counts(room_id)

    @socketio.on('join_team', namespace='/lobby')
    def handle_join_team(data):
        team = data.get('team')
        room_id = data.get('room_id')

        auth_token = request.cookies.get('auth_token')
        if not auth_token:
            return

        user = user_collection.find_one({'auth_token': hash_token(auth_token)})
        if not user:
            return

        username = user['username']
        room = room_collection.find_one({"id": room_id})
        if not room:
            return


        # Remove from all teams
        room_collection.update_one(
            {"id": room_id},
            {"$pull": {
                "red_team": username,
                "blue_team": username,
                "no_team": username
            }}
        )

        # Add to selected team
        if team == "red":
            room_collection.update_one({"id": room_id}, {"$push": {"red_team": username}})
        elif team == "blue":
            room_collection.update_one({"id": room_id}, {"$push": {"blue_team": username}})
        else:
            room_collection.update_one({"id": room_id}, {"$push": {"no_team": username}})

        # Ensure socket joins the room
        join_room(room_id)

        # Emit updated teams
        updated = room_collection.find_one({"id": room_id})
        emit('team_red_list', updated["red_team"], room=room_id)
        emit('team_blue_list', updated["blue_team"], room=room_id)
        emit('no_team_list', updated["no_team"], room=room_id)
        emit('joined_team', {'room_id': room_id, 'team': team}, to=request.sid)
        _emit_team_counts(room_id)
    @socketio.on('am_i_owner', namespace='/lobby')
    def handle_am_i_owner(data):
        room_id = data.get('room_id')
        auth_token = request.cookies.get('auth_token')

        if not auth_token:
            emit('owner_status', {'is_owner': False})
            return

        user = user_collection.find_one({'auth_token': hash_token(auth_token)})
        if not user:
            emit('owner_status', {'is_owner': False})
            return

        username = user['username']
        room = room_collection.find_one({'id': room_id})

        if not room:
            emit('owner_status', {'is_owner': False})
            return

        if username == room.get('owner'):
            emit('owner_status', {'is_owner': True})
        else:
            emit('owner_status', {'is_owner': False})

    @socketio.on('start_game', namespace='/lobby')
    def handle_start_game(data):
        room_id = data.get('room_id')

        auth_token = request.cookies.get('auth_token')
        if not auth_token:
            return

        user = user_collection.find_one({'auth_token': hash_token(auth_token)})
        if not user:
            return

        username = user['username']

        room = room_collection.find_one({'id': room_id})
        if not room:
            return

        if username != room.get('owner'):
            return  # ❌ Only owner can start


        # ✅ Loop through all players on red and blue teams
        players_to_start = room.get('red_team', []) + room.get('blue_team', [])

        battlefield_players = []

        for player in players_to_start:
            # Determine spawn position
            if player in room.get('red_team', []):
                spawn_x, spawn_y = 1, 1
                team = "red"
            else:
                spawn_x, spawn_y = 29, 24
                team = "blue"

            # Check if player already exists in players list (shouldn't, but safe check)
            if any(p['id'] == player for p in room.get('players', [])):
                continue

            # Prepare player data
            battlefield_players.append({
                'id': player,
                'x': spawn_x,
                'y': spawn_y,
                'team': team
            })

        # Push all players at once
        if battlefield_players:
            room_collection.update_one(
                {'id': room_id},
                {
                    '$push': {'players': {'$each': battlefield_players}},
                    '$set': {'game_started': True}  # ⬅️ Update here
                }
            )

        # Emit updated players
        updated_room = room_collection.find_one({'id': room_id})

        players_out = []
        for p in updated_room.get('players', []):
            uid = p['id']
            avatar_fn = choose_avatar(
                uid,
                updated_room,
                user_collection.find_one({'username': uid}) or {}
            )
            players_out.append({
                "id": uid,
                "x": p["x"],
                "y": p["y"],
                "team": p.get("team"),
                "avatar": avatar_fn
            })

        all_rooms = [
            {"id": str(room["id"]), "name": html.escape(room["room_name"])}
            for room in room_collection.find({"game_started": False})
        ]
        socketio.emit('room_list', all_rooms, namespace='/lobby')

        emit('player_positions', players_out, room=room_id)

        # Optionally, tell frontend: game started
        emit('game_started', room=room_id)
        kick_off_round_system(socketio, room_collection, room_id)


    @socketio.on('disconnect', namespace='/lobby')
    def handle_disconnect():
        sid = request.sid
        username = connected_users.pop(sid, None)

        if not username:
            return


        # ✅ 1. First, find rooms BEFORE you modify the DB
        rooms = list(room_collection.find({
            "$or": [
                {"red_team": username},
                {"blue_team": username},
                {"no_team": username},
            ]
        }))

        # ✅ 2. Then remove the user
        room_collection.update_many(
            {"id": {"$in": [r["id"] for r in rooms]}},
            {"$pull": {
                "red_team": username,
                "blue_team": username,
                "no_team": username,
            }}
        )

        # ✅ 3. Emit to all rooms the user was in
        for room in rooms:
            room_id = room["id"]
            updated = room_collection.find_one({"id": room_id})
            socketio.emit('team_red_list', updated["red_team"], room=room_id)
            socketio.emit('team_blue_list', updated["blue_team"], room=room_id)
            socketio.emit('no_team_list', updated["no_team"], room=room_id)
            _emit_team_counts(room_id)


    @socketio.on('connect', namespace='/lobby')
    def handle_connect():
        page = request.args.get('page')
        room_id = request.args.get('room_id')


# server-side battlefield terrain generation (Python)
import random

def generate_battlefield_terrain(width=30, height=20):
    terrain = [[0 for _ in range(width)] for _ in range(height)]

    # Define safe zones (smaller for 50x50)
    safe_zone_red = (0, 0, 2, 2)
    safe_zone_blue = (width-2, height-2, width, height)

    def in_safe_zone(x, y):
        return (safe_zone_red[0] <= x < safe_zone_red[2] and safe_zone_red[1] <= y < safe_zone_red[3]) or \
               (safe_zone_blue[0] <= x < safe_zone_blue[2] and safe_zone_blue[1] <= y < safe_zone_blue[3])

    # Step 1: Create medium-sized wall blocks
    for _ in range(5):  # fewer blocks for small map
        block_width = random.randint(1, 2)
        block_height = random.randint(1, 2)
        start_x = random.randint(0, width - block_width - 1)
        start_y = random.randint(0, height - block_height - 1)

        for x in range(start_x, start_x + block_width):
            for y in range(start_y, start_y + block_height):
                if not in_safe_zone(x, y):
                    terrain[y][x] = 1  # Wall

    # Step 2: Sprinkle small obstacles
    for _ in range(20):  # Scaled down for smaller map
        x = random.randint(0, width - 1)
        y = random.randint(0, height - 1)
        if not in_safe_zone(x, y) and terrain[y][x] == 0:
            terrain[y][x] = 1

    # Step 3: Mark safe zones with team numbers
    for x in range(width):
        for y in range(height):
            if 0 <= x < 2 and 0 <= y < 2:
                terrain[y][x] = 3  # Red team safe
            elif width - 2 <= x < width and height - 2 <= y < height:
                terrain[y][x] = 2  # Blue team safe

    return terrain

