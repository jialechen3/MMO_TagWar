from flask import Blueprint, render_template, request
from flask_socketio import emit, join_room
from util.auth import hash_token
from eventlet import sleep
from eventlet.semaphore import Semaphore
import math

from util.database import user_collection
from util.rooms import choose_avatar, enrich_with_avatars

# Constants for map size
MAP_WIDTH = 30
MAP_HEIGHT = 20

terrain = [[0 for _ in range(MAP_WIDTH)] for _ in range(MAP_HEIGHT)]

for y in range(MAP_HEIGHT):
    for x in range(MAP_WIDTH):
        if y < 4 and x >= MAP_WIDTH - 4:
            inside = (x >= MAP_WIDTH - 3 and x <= MAP_WIDTH - 2) and (y >= 1 and y <= 2)
            terrain[y][x] = 0 if inside else 3  # red base walls
        elif y >= MAP_HEIGHT - 4 and x < 4:
            terrain[y][x] = 2  # blue base walls
        elif (x + y) % 11 == 0 and 3 < x < MAP_WIDTH - 4 and 3 < y < MAP_HEIGHT - 4:
            terrain[y][x] = 1  # scattered maze walls


# Global memory
room_player_data = {}  # { room_id: { player_name: {"x": int, "y": int, "team": str} } }
player_status = {}     # { room_id: { player_name: "alive" or "dead", dx: -2.0 - 2.0, dy: -2.0 - 2.0} }
room_player_data_lock = Semaphore()
player_status_lock = Semaphore()

def register_battlefield_handlers(socketio, user_collection, room_collection):

    @socketio.on('connect', namespace='/battlefield')
    def handle_battlefield_connect():
        print('Client connected to battlefield')

    @socketio.on('join_room', namespace='/battlefield')
    def handle_battlefield_join_room(data):
        room_id = data.get('room_id')
        player_id = data.get('player')

        if room_id:
            join_room(room_id)

            # 🔥 Immediately emit the current player positions after joining
            room = room_collection.find_one({"id": room_id})
            if not room:
                return

            players = room.get('players', [])
            updated_players = []

            room_doc = room_collection.find_one({"id": room_id})

            for p in players:
                user_doc = user_collection.find_one({"username": p["id"]})
                p["avatar"] = choose_avatar(p["id"], room_doc, user_doc)
                updated_players.append(p)

            emit('player_positions', updated_players, room=request.sid, namespace='/battlefield')
            terrain_data = room.get('terrain')
            if terrain_data:
                emit('load_terrain', {'terrain': terrain_data}, room=request.sid, namespace='/battlefield')

    @socketio.on('move', namespace='/battlefield')
    def handle_move(data):
        room_id = data.get('roomId')
        player = data.get('player')
        keyPress = data.get('direction')

        if not room_id or not player or not keyPress:
            return

        room = room_collection.find_one({'id': room_id})
        if not room:
            return

        # fetch once
        terrain = room.get('terrain', [[0] * MAP_WIDTH for _ in range(MAP_HEIGHT)])
        player_list = room.get('players', [])
        player_data = next((p for p in player_list if p['id'] == player), None)
        if not player_data:
            return

        with player_status_lock:
            if player_status.get(room_id, {}).get(player, {}).get('status') == "dead":
                return


        # movement logic
        new_x, new_y = player_data['x'], player_data['y']
        if keyPress['ArrowUp']:
            new_y = round((new_y - 0.1)*100)/100
        if keyPress['ArrowDown']:
            new_y = round((new_y + 0.1)*100)/100
        if keyPress['ArrowLeft']:
            new_x = round((new_x - 0.1)*100)/100
        if keyPress['ArrowRight']:
            new_x = round((new_x + 0.1)*100)/100

        x_check, y_check = False, False
        if not 0 <= new_x <= MAP_WIDTH-1:
            new_x = clamp(new_x, 0, MAP_WIDTH-1)
            x_check = True
        if not 0 <= new_y <= MAP_HEIGHT-1:
            new_y = clamp(new_y, 0, MAP_HEIGHT-1)
            y_check = True
        if x_check and y_check:
            return

        f_new_x = math.floor(new_x)
        c_new_x = math.ceil(new_x) if new_x % 1 != 0 else f_new_x
        f_new_y = math.floor(new_y)
        c_new_y = math.ceil(new_y) if new_y % 1 != 0 else f_new_y

        tileTL = terrain[f_new_y][f_new_x]
        tileTR = terrain[f_new_y][c_new_x]
        tileBL = terrain[c_new_y][f_new_x]
        tileBR = terrain[c_new_y][c_new_x]

        old_x = new_x
        enemy_team_num = 3 if player_data.get('team') == 'blue' else 2

        if new_x != player_data['x'] and f_new_x != c_new_x:
            if new_x < player_data['x']:
                if (tileTL in (1, enemy_team_num)) or (tileBL in (1, enemy_team_num)):
                    new_x = player_data['x']
            elif new_x > player_data['x']:
                if (tileTR in (1, enemy_team_num)) or (tileBR in (1, enemy_team_num)):
                    new_x = player_data['x']

        if new_y != player_data['y'] and f_new_y != c_new_y:
            if new_y > player_data['y']:
                if (tileBL in (1, enemy_team_num)) or (tileBR in (1, enemy_team_num)):
                    new_y = player_data['y']
            elif new_y < player_data['y']:
                if (tileTL in (1, enemy_team_num)) or (tileTR in (1, enemy_team_num)):
                    new_y = player_data['y']

        result = room_collection.update_one(
            {'id': room_id, 'players.id': player},
            {'$set': {'players.$.x': new_x, 'players.$.y': new_y}}
        )
        if result.matched_count == 0:
            return

        # update in-memory and enrich with avatars
        user_cache = {}
        for p in room['players']:
            pid = p['id']
            if pid not in user_cache:
                user_cache[pid] = user_collection.find_one({"username": pid}) or {}
            p['avatar'] = choose_avatar(pid, room, user_cache[pid])

        with room_player_data_lock:
            room_player_data[room_id] = {
                p['id']: {'x': p['x'], 'y': p['y']}
                for p in room['players'] if p.get('id')
            }


        # tagging logic
        attacking_team = room.get('attacking_team')
        if attacking_team:
            with room_player_data_lock:
                for other_id, pos in room_player_data[room_id].items():
                    if other_id == player:
                        continue
                    if abs(pos['x'] - new_x) <= 1 and abs(pos['y'] - new_y) <= 1:
                        target_data = next((p for p in room['players'] if p['id'] == other_id), None)
                        if not target_data:
                            continue

                        mover_team = player_data.get('team')
                        target_team = target_data.get('team')
                        if mover_team == target_team:
                            continue

                        if mover_team == attacking_team:
                            victim, tagger = other_id, player
                        elif target_team == attacking_team:
                            victim, tagger = player, other_id
                        else:
                            continue

                        with player_status_lock:
                            if player_status.get(room_id, {}).get(victim, {}).get('status') == 'dead':
                                continue
                            player_status.setdefault(room_id, {})[victim] = {'status': 'dead', 'tagger': tagger}
                        emit('player_tagged', {'tagger': tagger, 'target': victim}, room=room_id)
                        socketio.start_background_task(respawn_player, socketio, room_collection, room_id, victim)
                        break

        emit('player_moved', {'id': player, 'x': new_x, 'y': new_y}, room=room_id, namespace='/battlefield')

    @socketio.on('disconnect', namespace='/battlefield')
    def handle_battlefield_disconnect():
        sid = request.sid

        auth_token = request.cookies.get('auth_token')
        if not auth_token:
            return

        user = user_collection.find_one({'auth_token': hash_token(auth_token)})
        if not user:
            return

        username = user['username']


        rooms = list(room_collection.find({"players.id": username}))

        for room in rooms:
            room_id = room["id"]
            room_collection.update_one(
                {"id": room_id},
                {"$pull": {"players": {"id": username}}
            })

            updated = room_collection.find_one({"id": room_id})
            socketio.emit('player_left', {'id': username}, room=room_id, namespace='/battlefield')

    #gives latest player info after respawn
    @socketio.on('request_positions', namespace='/battlefield')
    def handle_request_positions():
        auth_token = request.cookies.get('auth_token')
        if not auth_token:
            return

        user = user_collection.find_one({'auth_token': hash_token(auth_token)})
        if not user:
            return

        username = user['username']

        # Find the room the user is in and get the latest full document
        room = room_collection.find_one({"players.id": username})
        if not room:
            return

        room_id = room['id']
        
        # Now re-fetch the full room document by its id to ensure up-to-date player list
        updated_room = room_collection.find_one({"id": room_id})
        if not updated_room:
            return

        players_out = enrich_with_avatars(updated_room, user_collection)
        emit('player_positions', players_out, namespace='/battlefield')


def respawn_player(socketio, room_collection, room_id, player):
    sleep(5)  # 5 seconds dead

    with player_status_lock:
        if room_id not in player_status or player not in player_status[room_id]:
            return
        tagger = player_status[room_id][player].get('tagger')

    # Fetch the tagger's team
    room = room_collection.find_one({'id': room_id})
    if not room:
        return

    tagger_data = next((p for p in room.get('players', []) if p['id'] == tagger), None)
    if not tagger_data:
        return

    new_team = tagger_data['team']

    # Update the player's team in database
    room_collection.update_one(
        {"id": room_id, "players.id": player},
        {"$set": {"players.$.team": new_team}}
    )
    updated_room = room_collection.find_one({"id": room_id})
    players_out = enrich_with_avatars(updated_room, user_collection)
    socketio.emit('player_positions',players_out,room = room_id, namespace = '/battlefield')

    # Mark as alive
    with player_status_lock:
        player_status[room_id][player] = {
            "status": "alive"
        }

    # Tell clients
    socketio.emit('player_respawned', {"player": player}, room=room_id, namespace='/battlefield')


# Blueprint
battlefield_bp = Blueprint('battlefield', __name__)

@battlefield_bp.route('/battlefield')
def battlefield():
    room_id = request.args.get('room')
    if not room_id:
        return "Missing room ID", 400
    return render_template('battlefield.html', room_id=room_id)

def clamp(value, min_value, max_value):
    return max(min_value, min(value, max_value))