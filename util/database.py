import os
from pymongo import MongoClient

# Check if running inside Docker
docker_db = os.environ.get('DOCKER_DB', "false").lower() == "true"

# Set up MongoDB connection string
if docker_db:
    mongo_client = MongoClient("mongodb://mongo:27017")  # docker-compose service name
else:
    mongo_client = MongoClient("mongodb://localhost:27017")

# Access the database and collections
db = mongo_client["mmo_game"]
user_collection = db["users"]
chat_collection = db["chat"]
room_collection = db["rooms"]