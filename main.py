import json
from fastapi import FastAPI, HTTPException, Depends, WebSocket, WebSocketDisconnect
from fastapi.security import APIKeyHeader
from dotenv import load_dotenv
from typing import Dict
from pydantic import BaseModel
import os
import secrets
import time
import requests
import asyncpg

load_dotenv()
app = FastAPI()

api_key_header = APIKeyHeader(name="Oni-API-Key", auto_error=False)

@app.on_event("startup")
async def startup_event():
    app.state.conn = await asyncpg.connect(
        host=os.getenv("DB_HOST"),
        port=os.getenv("DB_PORT"),
        database=os.getenv("DB_NAME"),
        user=os.getenv("DB_USER"),
        password=os.getenv("DB_PASSWORD"),
    )

@app.on_event("shutdown")
async def shutdown_event():
    await app.state.conn.close()

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

    async def send_json_to_all(self, data: str):
        for websocket in self.active_connections.values():
            await websocket.send_json(data)
            
    async def send_json_to_user(self, user_id: str, data: str):
        websocket = self.active_connections.get(user_id)
        if websocket:
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
        conn = app.state.conn
        query = "SELECT * FROM users WHERE api_key = $1"
        user = await conn.fetchrow(query, api_key)
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
        conn = app.state.conn
        query = "UPDATE users SET session_token = $1 WHERE id = $2"
        await conn.execute(query, session_token, user.id)
        return {"session token": session_token}
    except Exception as e:
        raise HTTPException(status_code=401, detail="Invalid API key")
    
async def check_session_and_api_key(session_token: str, api_key: str) -> bool:
    if session_token is None or api_key is None:
        return False
    
    conn = app.state.conn
    query = "SELECT * FROM users WHERE session_token = $1 AND api_key = $2"
    user = await conn.fetchrow(query, session_token, api_key)
    
    if user is None:
        return False
    else: 
        return True

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
    conn = app.state.conn
    query = "SELECT id, session_token FROM users WHERE session_token = $1"
    session = await conn.fetchrow(query, session_token)

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
                try:
                    message = await websocket.receive()
                    try:
                        data = json.loads(message)
                        print(data)
                        print(type(data))
                    except json.JSONDecodeError:
                        print("Received message is not in JSON format.")
                except Exception as e:
                    print("An error occurred while receiving the WebSocket message:", e)
                
                if await check_session_and_api_key(data.get('session_token'), data.get('api_key')):
                    print("checking condition")
                    # Buy Condition
                    if data.get('condition') == 'buy':
                        print("buy")
                        query = """
                        INSERT INTO user_trades (user_id, tv_buy_id, market_buy_id, take_profit_id, order_error)
                        VALUES (
                            (SELECT id FROM users WHERE session_token = $1),
                            $2,
                            $3,
                            $4,
                            false
                        )
                        """
                        await conn.execute(query, session_token, data.get('tv_buy_id'), data.get('market_buy_id'), data.get('take_profit_id'))
                    
                    # Stop Condition    
                    if data.get('condition') == 'stop':
                        print("stop")
                        query = """
                        UPDATE user_trades
                        SET stop_loss_id = $1, executed_flag = true, net_amount = $2
                        WHERE user_id = (SELECT id FROM users WHERE session_token = $3)
                        AND tv_buy_id = $4
                        """
                        await conn.execute(query, data.get('stop_loss_id'), data.get('net_amount'), session_token, data.get('tv_buy_id'))
                    
                    # TP Condition
                    if data.get('condition') == 'tp':
                        print("tp")
                        query = """
                        UPDATE user_trades
                        SET stop_loss_id = $1, executed_flag = true, net_amount = $2
                        WHERE user_id = (SELECT id FROM users WHERE session_token = $3)
                        AND tv_buy_id = $4
                        """
                        await conn.execute(query, data.get('stop_loss_id'), data.get('net_amount'), session_token, data.get('tv_buy_id'))
                else:
                    # Invalid session based on old session token or api key
                    print("how tf am i here")
                    return                  
            else:
                # If rate limit is exceeded, close the connection
                print("no chance its rate limit")
                await manager.disconnect(session_token)
                return
    except WebSocketDisconnect as e:
        print(e)
        await manager.disconnect(session_token)

class Message(BaseModel):
    data: Dict


async def send_message(message: Message):
    await manager.send_json_to_all(message)

@app.post("/alert")
async def handle_tradingview_alerts(data: str):
    try:
        conn = app.state.conn
        data = json.loads(data)
        auth_key = data["oni_auth_key"]
        if auth_key != os.getenv("ONI_AUTH_KEY"):
            raise HTTPException(status_code=401, detail="Improper Authorization Key")
        client_json = {
            "condition": data["condition"],
            "ticker": data["ticker"],
            "tv_buy_id": data["tv_buy_id"]
        }

        if data["condition"] == 'buy':
            query = """
                INSERT INTO buys (tv_buy_id, ticker, script_version)
                VALUES ($1, $2, $3)
                """
            await conn.execute(query, data["tv_buy_id"], data["ticker"], data["script_version"])
            
            discord = {
                "ticker": data["ticker"],
                "color": data["color"],
                "entry": data["entry"],
                "tp": data["tp"],
                "stop": data["stop"],
                "timestamp": data["timestamp"],
                "script_version": data["script_version"]
            }
            await format_and_post_to_discord(discord)
            client_json["tp"] = data["tp"]
        else:
            raise HTTPException(status_code=400, detail=f"Incorrect condition")
        await send_message(client_json)
    except KeyError as e:
        raise HTTPException(status_code=400, detail=f"Missing required field")
    
@app.post('/alert/tp')
async def alert_take_profit(data: str):
    conn = app.state.conn
    data = json.loads(data)
    auth_key = data["oni_auth_key"]
    if auth_key != os.getenv("ONI_AUTH_KEY"):
        raise HTTPException(status_code=401, detail="Improper Authorization Key")
    condition = data["condition"]
    ticker = data["ticker"]
    tv_buy_id = data["tv_buy_id"]
    query = """
    SELECT user_id
    FROM user_trades
    WHERE tv_buy_id = $1
    """
    await conn.execute(query, tv_buy_id)
    users = conn.fetchall()
    if users:
            for row in users:
                # Get the buy and tp id's
                market_buy_id = row['market_buy_id']
                take_profit_id = row['take_profit_id']
                # Format some json with fields
                response = {
                    "condition": condition,
                    "ticker": ticker,
                    "tv_buy_id": tv_buy_id,
                    "market_buy_id": market_buy_id,
                    "take_profit_id": take_profit_id
                }
                # Send json to the specific websocket with the corresponding market_buy_id
                user_id = row['user_id']
                await manager.send_json_to_user(user_id, response)
    
async def format_and_post_to_discord(data: dict):
    ticker = data.get("ticker")
    color = data.get("color")
    entry = data.get("entry")
    tp = data.get("tp")
    stop = data.get("stop")
    timestamp = data.get("timestamp")
    script_version = data.get("script_version")

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
                        "value": str(tp[0]),
                        "inline": True
                    },
                    {
                        "name": "TP 2",
                        "value": str(tp[1]),
                        "inline": True
                    },
                    {
                        "name": "TP 3",
                        "value": str(tp[2]),
                        "inline": True
                    },
                    {
                        "name": "Stop Loss",
                        "value": str(stop),
                        "inline": True
                    }
                ],
                "footer": {
                    "text": f"Oni Algo {script_version}"
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