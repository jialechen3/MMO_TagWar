# Use an official Python base image
FROM python:3.10-slim

# Set working directory
WORKDIR /mmogame

# Copy source code
COPY . .

# Install dependencies
RUN pip install --no-cache-dir -r requirements.txt

# Expose port Flask will run on
EXPOSE 8080


RUN pip install --no-cache-dir gunicorn

# Run the Flask app using flask_socketio
CMD ["python", "server.py"]