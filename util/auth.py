import re
import os
import html
import logging
from flask import Flask, render_template, Blueprint, redirect, jsonify
from flask import request, g
from flask import request,make_response
import uuid
import bcrypt
import hashlib
from util.database import user_collection
from flask import current_app, render_template, request, redirect, url_for, g
from werkzeug.utils import secure_filename
auth_bp = Blueprint('auth', __name__)

@auth_bp.route('/register', methods=['GET', 'POST'])
def register():
    if request.method == 'GET':
        return render_template('register.html')

    data = request.form
    user = data.get('username')
    password = data.get('password')

    if not user or not password:
        logging.info(f"Registration attempt failed: missing credentials (user: {user})")
        return render_template('register.html', error="Missing credentials")

    if not validate_password(password):
        logging.info(f"Registration attempt failed: weak password (user: {user})")
        return render_template('register.html', error="Password must be at least 8 characters, contain uppercase, lowercase, number, and special character.")

    if user_collection.find_one({"username": user}):
        logging.info(f"Registration attempt failed: username '{user}' already taken")
        return render_template('register.html', error="Username already taken")

    # Success
    hashed_pw = bcrypt.hashpw(password.encode(), bcrypt.gensalt()).decode()
    user_id = str(uuid.uuid4())

    user_collection.insert_one({
        "id": user_id,
        "username": user,
        "password": hashed_pw,
        "auth_token": None,
        "wins": 0
    })

    logging.info(f"Registration successful: user '{user}' created")
    resp = make_response(redirect("/"))
    resp.set_cookie("session", user_id)
    return resp

@auth_bp.route('/login', methods=['GET', 'POST'])
def login():
    if request.method == 'GET':
        return render_template('login.html')

    data = request.form
    user = data.get('username')
    password = data.get('password')

    if not user or not password:
        logging.info(f"Login attempt failed: missing credentials (user: {user})")
        return "Missing credentials", 400

    dbEntry = user_collection.find_one({'username': user})
    if not dbEntry:
        logging.info(f"Login attempt failed: username '{user}' does not exist")
        return render_template("login.html", error="Incorrect username")

    if not bcrypt.checkpw(password.encode(), dbEntry["password"].encode()):
        logging.info(f"Login attempt failed: wrong password for user '{user}'")
        return render_template("login.html", error="Incorrect password")

    # Success
    token = str(uuid.uuid4())
    hashed = hash_token(token)
    user_collection.update_one({"username": user}, {"$set": {"auth_token": hashed}})
    logging.info(f"Login successful: user '{user}'")

    resp = make_response(redirect("/lobby"))
    resp.set_cookie("auth_token", token, httponly=True, max_age=3600)
    return resp

@auth_bp.route('/logout', methods=['POST'])
def logout():
    token = request.cookies.get("auth_token")

    if token:
        hashed = hash_token(token)
        user = user_collection.find_one({"auth_token": hashed})
        if user:
            logging.info(f"User '{user['username']}' logged out")
            user_collection.update_one({"auth_token": hashed}, {"$unset": {"auth_token": ""}})

    resp = make_response(redirect("/"))
    resp.set_cookie("auth_token", '', expires=0)
    return resp

def validate_password(string):
    if len(string) < 8:
        return False
    if not re.search(r'[a-z]', string): return False
    if not re.search(r'[A-Z]', string): return False
    if not re.search(r'\d', string): return False
    if not re.search(r'[!@#$%^&()\-_=]', string): return False
    if not re.fullmatch(r'[A-Za-z0-9!@#$%^&()\-_=]+', string): return False
    return True

# hash auth token for DB storage
def hash_token(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


@auth_bp.before_app_request
def load_CurrentUser():

    token = request.cookies.get("auth_token")
    if token:
        g.user = user_collection.find_one({"auth_token": hash_token(token)})
    else:
        g.user = None
ALLOWED_EXT = {'png','jpg','jpeg'}
def allowed_file(fn):
    return '.' in fn and fn.rsplit('.',1)[1].lower() in ALLOWED_EXT

@auth_bp.route('/profile', methods=['GET','POST'])
def profile():
    # ensure logged in
    if not getattr(g, 'user', None):
        return redirect(url_for('auth.login'))

    user = g.user

    if request.method == 'POST':
        file = request.files.get('avatar')
        if file and allowed_file(file.filename):
            ext      = secure_filename(file.filename).rsplit('.',1)[1].lower()
            filename = f"{user['username']}.{ext}"
            out_path = os.path.join(current_app.root_path, 'static', 'avatars', filename)
            file.save(out_path)
            # update Mongo
            user_collection.update_one(
                {'username': user['username']},
                {'$set': {'avatar': filename}}
            )
        return redirect(url_for('auth.profile'))

    # GET — render form

    return render_template('profile.html', wins=user.get('wins', 0))


@auth_bp.route('/api/whoami')
def whoami():
    token = request.cookies.get("auth_token")
    if not token:
        return jsonify({"username": None})

    user = user_collection.find_one({"auth_token": hash_token(token)})
    if user:
        return jsonify({"username": user["username"]})

    return jsonify({"username": None})

@auth_bp.route('/api/leaderboard')
def leaderboard():
    # Fetch all users
    users = list(user_collection.find({}, {"_id": 0, "username": 1, "wins": 1}))

    # Add default 0 for missing 'wins' field
    for u in users:
        u["wins"] = u.get("wins", 0)

    # Sort in Python instead of MongoDB
    sorted_users = sorted(users, key=lambda u: u["wins"], reverse=True)

    return jsonify(sorted_users)
