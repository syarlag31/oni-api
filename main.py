import asyncio
import json
from fastapi import FastAPI, HTTPException, Depends, WebSocket, WebSocketDisconnect
from fastapi.security import APIKeyHeader
from dotenv import load_dotenv
from typing import Any, Dict
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
    
    asyncio.create_task(send_json_message_periodically())

@app.on_event("shutdown")
async def shutdown_event():
    await app.state.conn.close()

api_key_header = APIKeyHeader(name="Oni-API-Key", auto_error=False)

class Connection:
    def __init__(self, websocket: WebSocket, session_token: str):
        self.websocket = websocket
        self.session_token = session_token

class ConnectionManager:
    def __init__(self):
        self.active_connections: Dict[str, Connection] = {}  # user_id: Connection

    async def connect(self, user_id: str, websocket: WebSocket, session_token: str):
        if user_id in self.active_connections:
            old_connection = self.active_connections[user_id]
            if old_connection.session_token != session_token:
                try:
                    await old_connection.websocket.send_text("Another session was initiated, disconnecting.")
                    await old_connection.websocket.close(code=1000)
                except WebSocketDisconnect:
                    pass  # If it is already disconnected, pass.
                del self.active_connections[user_id]  # Remove old connection
            else:
                try:
                    await old_connection.websocket.send_text("Duplicate session detected, disconnecting.")
                    await old_connection.websocket.close(code=1000)
                except WebSocketDisconnect:
                    pass  # If it is already disconnected, pass.
                del self.active_connections[user_id]  # Remove old connection
            await asyncio.sleep(3)
        self.active_connections[user_id] = Connection(websocket, session_token)
        return self.active_connections[user_id]

    async def disconnect(self, user_id: str):
        if user_id in self.active_connections:
            connection = self.active_connections[user_id]
            if connection.websocket:
                await connection.websocket.close(code=1001)
            del self.active_connections[user_id]

    async def send_personal_message(self, user_id: str, message: Dict[str, Any]):
        if user_id not in self.active_connections:
            return
        connection = self.active_connections[user_id]
        try:
            await connection.websocket.send_json(message)
        except WebSocketDisconnect:
            print(f"Failed to send message to user {user_id}")
            await self.handle_disconnect(user_id)

    async def broadcast(self, message: Dict[str, Any]):
        disconnected_users = []
        for user_id, connection in self.active_connections.items():
            try:
                await connection.websocket.send_json(message)
            except WebSocketDisconnect:
                print(f"Failed to send message to user {user_id}")
                disconnected_users.append(user_id)
        for user_id in disconnected_users:
            await self.handle_disconnect(user_id)

    async def handle_disconnect(self, user_id: str):
        print("disconnecting user due to message sent", user_id)
        await self.disconnect(user_id)
        # Reconnection logic can be added here if necessary

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
    query = "SELECT id FROM users WHERE session_token = $1 AND api_key = $2"
    user = await conn.fetch(query, session_token, api_key)
    
    if len(user) > 0:
        return True
    else: 
        return False


@app.websocket("/ws/{session_token}")
async def websocket_endpoint(websocket: WebSocket, session_token: str):
    conn = app.state.conn
    query = "SELECT id, session_token FROM users WHERE session_token = $1"
    session = await conn.fetchrow(query, session_token)

    if session is None:
        await websocket.close(code=1008)
        return

    user_id, session_token = str(session[0]), session[1]  # Convert UUID to string
    await websocket.accept()
    connection = await manager.connect(user_id, websocket, session_token)

    try:
        while True:
            data = await connection.websocket.receive_json()
            if data.get("type") == "ping": # Needs a ping every 5 min to maintain persistent connection
                msg = {'type': 'pong'}
                await manager.send_personal_message(user_id, msg)
            elif data.get("condition"):
                await handle_client_condition(user_id, data)
                msg = {'type': 'Message Properly Recieved!'}
                await manager.send_personal_message(user_id, msg)
            else:
                print('Improper Data!')
    except WebSocketDisconnect:
        if user_id in manager.active_connections:
            del manager.active_connections[user_id]
    except Exception as e:
        print(e)
        await manager.disconnect(user_id)

async def handle_client_condition(user_id, data):
    conn = app.state.conn
    if data.get('condition') == 'buy':
        query = """
        INSERT INTO user_trades (user_id, tv_buy_id, market_buy_id, take_profit_id, tp, order_error)
        VALUES (
            (SELECT id FROM users WHERE id = $1),
            $2,
            $3,
            $4,
            $5,
            false
        )
        """
        await conn.execute(query, user_id, data.get('tv_buy_id'), data.get('market_buy_id'), data.get('take_profit_id'), data.get('tp'))
    
    # Stop Condition    
    if data.get('condition') == 'stop':
        query = """
        UPDATE user_trades
        SET stop_loss_id = $1, executed_flag = true, net_amount = $2
        WHERE user_id = $3
        AND tv_buy_id = $4
        """
        await conn.execute(query, data.get('stop_loss_id'), data.get('net_amount'), user_id, data.get('tv_buy_id'))
    
    # TP Condition
    if data.get('condition') == 'tp':
        query = """
        UPDATE user_trades
        SET stop_loss_id = $1, executed_flag = true, net_amount = $2
        WHERE user_id = $3
        AND tv_buy_id = $4
        """
        await conn.execute(query, data.get('stop_loss_id'), data.get('net_amount'), user_id, data.get('tv_buy_id'))
    
    

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
            await manager.broadcast(client_json)
            
        elif data["condition"] == 'stop':
            condition = data["condition"]
            ticker = data["ticker"]
            tv_buy_id = data["tv_buy_id"]
            query = """
            SELECT user_id, market_buy_id, take_profit_id
            FROM user_trades
            WHERE tv_buy_id = $1
            """
            users = await conn.fetch(query, tv_buy_id)
            
        elif data["condition"] == "executed":
            
            
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
                    user_id = str(row['user_id'])
                    await manager.send_personal_message(user_id, response)
        
        # elif data["condition"] == 'tp':
        #     condition = data["condition"]
        #     ticker = data["ticker"]
        #     tv_buy_id = data["tv_buy_id"]
        #     tp = data["tp"]
        #     query = """
        #     SELECT user_id, market_buy_id, take_profit_id
        #     FROM user_trades
        #     WHERE tv_buy_id = $1 and tp = $2
        #     """
        #     users = await conn.fetch(query, tv_buy_id, tp)
            
        #     if users:
        #         for row in users:
        #             # Get the buy and tp id's
        #             market_buy_id = row['market_buy_id']
        #             take_profit_id = row['take_profit_id']
        #             # Format some json with fields
        #             response = {
        #                 "condition": condition,
        #                 "ticker": ticker,
        #                 "tv_buy_id": tv_buy_id,
        #                 "market_buy_id": market_buy_id,
        #                 "take_profit_id": take_profit_id
        #             }
        #             # Send json to the specific websocket with the corresponding market_buy_id
        #             user_id = str(row['user_id'])
        #             await manager.send_personal_message(user_id, response)

        else:
            raise HTTPException(status_code=400, detail=f"Incorrect condition")
    except KeyError as e:
        raise HTTPException(status_code=400, detail=f"Missing required field")
    
    
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

async def send_json_message_periodically():
    while True:
        await asyncio.sleep(3 * 60 * 60)  # Wait for 3 hours
        conn = app.state.conn
        query = """
        SELECT user_id, tv_buy_id, market_buy_id, take_profit_id
        FROM user_trades
        WHERE executed_flag = true
        """
        executed_trades = await conn.fetch(query)

        if executed_trades:
            user_orders = {}

            for row in executed_trades:
                user_id = str(row['user_id'])
                order = {
                    "tv_buy_id": row['tv_buy_id'],
                    "market_buy_id": row['market_buy_id'],
                    "take_profit_id": row['take_profit_id']
                }
                if user_id in user_orders:
                    user_orders[user_id].append(order)
                else:
                    user_orders[user_id] = [order]

            for user_id, orders in user_orders.items():
                message = {
                    "condition": "check",
                    "orders": orders
                }
                await manager.send_personal_message(user_id, message)
