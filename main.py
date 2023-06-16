from fastapi import FastAPI, HTTPException, Depends, WebSocket, WebSocketDisconnect
from fastapi.security import APIKeyHeader
import psycopg2
from dotenv import load_dotenv
import os, secrets, time, requests
from typing import Dict
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

api_key_header = APIKeyHeader(name="Oni-API-Key", auto_error=False)

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
    try:
        cursor = connection.cursor()
        query = "SELECT * FROM users WHERE id = %s"
        cursor.execute(query, (api_key,))
        user = cursor.fetchone()
        cursor.close()
    except Exception:
        return

    if user is None:
        raise HTTPException(status_code=401, detail="Invalid API key")

    user_model = User(id=str(user[0]), session_token=str(user[1]))  # Create a User instance
    return user_model

def generate_session_token():
    return secrets.token_hex(16)

class User(BaseModel):
    id: str
    session_token: str

@app.post("/login")
async def get_session_token(user=Depends(authenticate_user)):
    session_token = generate_session_token()

    try:
        cursor = connection.cursor()
        query = "UPDATE users SET session_token = %s WHERE id = %s"
        cursor.execute(query, (session_token, user.id))
        connection.commit()
        cursor.close()

        return {"session token": session_token}
    except Exception as e:
        connection.rollback()  # Rollback the transaction in case of an error
        raise HTTPException(status_code=401, detail="Invalid API key")
    
app.post("/validate-session")
async def validate_user_session(api_key: str, session_token: str, user=Depends(authenticate_user)):
    if api_key == user.id and session_token == user.session_token:
        return "Success", 200
    else:
        return HTTPException(status_code=498, detail="Improper Session")

class TokenBucket:
    def __init__(self, tokens, fill_rate):
        """
        tokens is the total tokens in the bucket. fill_rate is the
        rate in tokens/second that the bucket will be refilled.
        """
        self.capacity = float(tokens)
        self._tokens = float(tokens)
        self.fill_rate = float(fill_rate)
        self.timestamp = time.monotonic()

    def consume(self, tokens):
        """
        Consume tokens from the bucket. Returns 0 if there were
        sufficient tokens, otherwise the expected time to wait
        until enough tokens become available.
        """
        if tokens <= self._tokens:
            self._tokens -= tokens
            return 0
        else:
            delta = time.monotonic() - self.timestamp
            self._tokens += delta * self.fill_rate
            self.timestamp = time.monotonic()
            if tokens <= self._tokens:
                self._tokens -= tokens
                return 0
            else:
                return (tokens - self._tokens) / self.fill_rate

rate_limits = {}  # Create a global dictionary to manage the rate limits

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

    # Set up rate limit for this user
    rate_limit = rate_limits.get(user_id, TokenBucket(5, 1))  # Allow 5 messages per second

    try:
        while True:
            wait_time = rate_limit.consume(1)  # Each message consumes one token
            if wait_time == 0:
                data = await websocket.receive_text()
                print(data)
                # Add message verification
            else:
                # If rate limit is exceeded, close the connection
                await manager.disconnect(session_token)
                return
    except WebSocketDisconnect:
        await manager.disconnect(session_token)

class Message(BaseModel):
    data: Dict


@app.post("/send-message")
async def send_message(message: Message):
    if message["oni auth key"] == os.getenv("ONI_AUTH_KEY"):
            raise HTTPException(status_code=401, detail="Improper Authorization Key")
    await manager.send_json(message.data)

@app.post("/alert")
async def format_and_post_to_discord(data: dict):
    try:
        auth_key = data["oni auth key"]
        ticker = data["ticker"]
        color = data["color"]
        entry = data["entry"]
        TP = data["TP"]
        stopLoss = data["stopLoss"]
        timestamp = data["timestamp"]

        if auth_key != os.getenv("ONI_AUTH_KEY"):
            raise HTTPException(status_code=401, detail="Improper Authorization Key")
    except KeyError as e:
        raise HTTPException(status_code=400, detail=f"Missing required field")

    formatted_json = {
        "content": None,
        "embeds": [
            {
                "title": f"Entry Alert {ticker}",
                "url": f"https://www.tradingview.com/symbols/{ticker}/",
                "color": int(color),
                "fields": [
                    {
                        "name": "Entry Buy",
                        "value": str(entry)
                    },
                    {
                        "name": "TP 1",
                        "value": str(TP[0]),
                        "inline": True
                    },
                    {
                        "name": "TP 2",
                        "value": str(TP[1]),
                        "inline": True
                    },
                    {
                        "name": "TP 3",
                        "value": str(TP[2]),
                        "inline": True
                    },
                    {
                        "name": "Stop Loss",
                        "value": str(stopLoss),
                        "inline": True
                    }
                ],
                "footer": {
                    "text": "Oni Algo v1.0.0"
                },
                "timestamp": timestamp
            }
        ]
    }

    try:
        # Sends request to the URL below. Currently static and works for only one URL that can be replaced below
        response = requests.post(
            "https://discord.com/api/webhooks/1112897541919490190/8QpJ9Qxaw4eZR2BcU-hx4gtcFs-yUPZaBoHRT3Zjm21bvoAg3jsBSb3oea_ZmyfWb4gX",
            json=formatted_json
        )
        response.raise_for_status()
    except requests.exceptions.RequestException as e:
        raise HTTPException(status_code=500, detail=f"Failed to post to Discord")

    return {"message": "Formatted JSON posted to Discord successfully"}