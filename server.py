import eventlet
eventlet.monkey_patch()
import hashlib
import os
import logging
import traceback
from flask import Response as FlaskResponse
from flask import request, jsonify, Blueprint, g
from flask import Flask, render_template
from flask_socketio import SocketIO
from util.auth import auth_bp, hash_token
from util.battlefield import battlefield_bp, register_battlefield_handlers
from util.database import user_collection, room_collection
from util.rooms import register_room_handlers

app = Flask(__name__)
socketio = SocketIO(app, async_mode='eventlet')
@app.context_processor
def inject_user():
    return dict(current_user=g.user)

@app.after_request
def add_security_headers(response):
    response.headers['X-Content-Type-Options'] = 'nosniff'

    ip = request.remote_addr
    method = request.method
    path = request.path
    status = response.status_code
    username = getattr(g, 'user', {}).get('username') if getattr(g, 'user', None) else 'Unauthenticated'


    logging.info(f"{ip} - {username} - {method} {path} → {status}")

    if response.content_type and not response.content_type.startswith('text'):
        raw_logger.info(f"RESPONSE: {method} {path} → {status} — {response.content_type} (not logged)")
    else:
        try:
            preview = response.get_data(as_text=True)[:2048]
        except Exception:
            preview = "[Could not decode response body]"
        raw_logger.info(
            f"RESPONSE: {method} {path} → {status}\n"
            f"Headers: {dict(response.headers)}\n"
            f"Body:\n{preview}"
        )

    return response

app.config['SECRET_KEY'] = 'secret!'  # Replace with a secure key in production

# Setup logging
if not os.path.exists('logs'):
    os.makedirs('logs')

# Setup main logger
logging.basicConfig(
    filename='logs/server.log',
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(message)s',
    datefmt='%Y-%m-%d %H:%M:%S'
)

# Setup raw HTTP logger
raw_logger = logging.getLogger('raw')
raw_logger.setLevel(logging.INFO)
raw_logger.propagate = False  
raw_handler = logging.FileHandler('logs/raw_http.log')
raw_handler.setFormatter(logging.Formatter('%(asctime)s [%(levelname)s] %(message)s'))
raw_logger.addHandler(raw_handler)

def log_request_info():
    ip = request.remote_addr
    method = request.method
    path = request.path
    username = getattr(g, 'user', {}).get('username') if getattr(g, 'user', None) else 'Unauthenticated'

    logging.info(f"{ip} - {username} - {method} {path}")

    # Raw request logging (limit to 2048 bytes, redact sensitive info)
    if request.content_type and 'multipart' in request.content_type:
        raw_logger.info(f"{method} {path} from {ip} — multipart form (headers only)")
        return

    headers = dict(request.headers)
    headers.pop('Cookie', None)  # Remove cookies
    sanitized_headers = {k: v for k, v in headers.items() if 'auth_token' not in v.lower()}

    body_preview = request.get_data()[:2048].decode(errors='replace') if request.data else ''

    raw_logger.info(f"REQUEST: {method} {path} from {ip}\nHeaders: {sanitized_headers}\nBody:\n{body_preview}")

app.before_request(log_request_info)

# Blueprints and socketio event registration
app.register_blueprint(auth_bp)
app.register_blueprint(battlefield_bp)
register_room_handlers(socketio, user_collection, room_collection)
register_battlefield_handlers(socketio, user_collection, room_collection)

@app.errorhandler(Exception)
def handle_exception(e):
    logging.exception(f"Unhandled exception during request to {request.path}:")
    return "Internal Server Error", 500


# Routes
@app.route('/')
def index():
    return render_template('login.html')

@app.route('/lobby')
def lobby():
    return render_template('lobby.html')

@app.route('/lobby/<lobby_id>')
def lobby_by_id(lobby_id):
    room = room_collection.find_one({"id": lobby_id})
    if not room:
        return "Room not found", 404
    return render_template('lobby_by_id.html', lobby_id=lobby_id, room_name=room["room_name"])

# SocketIO Server run
if __name__ == '__main__':
    try:
        socketio.run(app, host='0.0.0.0', port=8080, allow_unsafe_werkzeug=True, debug=False)
    except Exception:
        logging.exception("Unhandled server exception:\n" + traceback.format_exc())
