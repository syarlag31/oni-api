from fastapi import FastAPI, HTTPException, Depends, WebSocket, WebSocketDisconnect
from fastapi.security import APIKeyHeader
import psycopg2
from dotenv import load_dotenv
import os, secrets
from typing import List, Dict
from pydantic import BaseModel

load_dotenv()
app = FastAPI()

# Database Connection
connection = psycopg2.connect(
    host=os.getenv("DB_HOST"),
    port=os.getenv("DB_PORT"),
    dbname=os.getenv("DB_NAME"),
    user=os.getenv("DB_USER"),
    password=os.getenv("DB_PASSWORD")
)

api_key_header = APIKeyHeader(name="X-API-Key", auto_error=False)

class ConnectionManager:
    def __init__(self):
        self.active_connections: Dict[str, WebSocket] = {}
        self.user_connections: Dict[str, str] = {}

    async def connect(self, websocket: WebSocket, session_token: str, user_id: str):
        await websocket.accept()
        if user_id in self.user_connections:  # User has an existing session
            old_token = self.user_connections[user_id]
            await self.check_and_close(old_token)
        self.active_connections[session_token] = websocket
        self.user_connections[user_id] = session_token
        print(self.active_connections)

    async def disconnect(self, session_token: str):
        websocket = self.active_connections.get(session_token)
        if websocket:
            try:
                await websocket.close()
            except RuntimeError:
                pass  # The socket was already closed
            del self.active_connections[session_token]

    async def send_json(self, data: str):
        for websocket in self.active_connections.values():
            await websocket.send_json(data)

    async def check_and_close(self, old_token: str):
        if old_token in self.active_connections:
            websocket = self.active_connections[old_token]
            if websocket:
                try:
                    await websocket.close()
                except RuntimeError:
                    pass  # The socket was already closed
                del self.active_connections[old_token]

manager = ConnectionManager()

async def authenticate_user(api_key: str = Depends(api_key_header)):
    cursor = connection.cursor()
    query = "SELECT * FROM users WHERE id = %s"
    cursor.execute(query, (api_key,))
    user = cursor.fetchone()
    cursor.close()

    if user is None:
        raise HTTPException(status_code=401, detail="Invalid API key")

    return user

def generate_session_token():
    return secrets.token_hex(16)

@app.post("/session-token")
async def get_session_token(user=Depends(authenticate_user)):
    session_token = generate_session_token()
    user_id = str(user[0]) # Convert UUID to string

    try:
        cursor = connection.cursor()
        query = "UPDATE users SET session_token = %s WHERE id = %s"
        cursor.execute(query, (session_token, user_id))
        connection.commit()
        cursor.close()

        return {"session_token": session_token}
    except Exception as e:
        connection.rollback()  # Rollback the transaction in case of an error
        raise e

@app.websocket("/ws/{session_token}")
async def websocket_endpoint(websocket: WebSocket, session_token: str):
    cursor = connection.cursor()
    query = "SELECT id, session_token FROM users WHERE session_token = %s"
    cursor.execute(query, (session_token,))
    session = cursor.fetchone()
    cursor.close()

    if session is None:
        await websocket.close(code=1008)
        return

    user_id, session_token = str(session[0]), session[1]  # Convert UUID to string
    await manager.connect(websocket, session_token, user_id)
    try:
        while True:
            data = await websocket.receive_text()
            # Add message verification
    except WebSocketDisconnect:
        await manager.disconnect(session_token)

class Message(BaseModel):
    data: Dict

@app.post("/send-message")
async def send_message(message: Message):
    await manager.send_json(message.data)
