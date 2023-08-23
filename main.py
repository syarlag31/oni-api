import asyncio
import ast
import datetime
from fastapi import FastAPI, HTTPException, Depends, Request, Body
from fastapi.security import APIKeyHeader
from dotenv import load_dotenv
from typing import Any, Dict
from pydantic import BaseModel
import os
import secrets
import requests
import asyncpg

load_dotenv()
app = FastAPI()
async_tasks = []

api_key_header = APIKeyHeader(name="Oni-API-Key", auto_error=False)

@app.on_event("startup")
async def startup_event():
    app.state.pool = await asyncpg.create_pool(
        host=os.getenv("DB_HOST"),
        port=os.getenv("DB_PORT"),
        database=os.getenv("DB_NAME"),
        user=os.getenv("DB_USER"),
        password=os.getenv("DB_PASSWORD"),
    )
    
    message_task = asyncio.create_task(send_json_message_periodically())
    expired_task = asyncio.create_task(remove_expired_buys())
    sent_task = asyncio.create_task(remove_sent_notifications())

    async_tasks.extend([message_task, expired_task, sent_task])

@app.on_event("shutdown")
async def shutdown_event():
    # Set a timeout for pool close
    try:
        await asyncio.wait_for(app.state.pool.close(), timeout=60)  # 60 seconds timeout
    except asyncio.TimeoutError:
        print("Closing the pool took too long!")
    
    # Cancel all tasks
    for task in async_tasks:
        task.cancel()

    # Give the tasks a chance to clean up after being canceled
    await asyncio.gather(*async_tasks, return_exceptions=True)

api_key_header = APIKeyHeader(name="Oni-API-Key", auto_error=False)

async def authenticate_user(api_key: str = Depends(api_key_header)):
    try:
        async with app.state.pool.acquire() as conn:
            query = "SELECT * FROM users WHERE api_key = $1"
            user = await conn.fetchrow(query, api_key)
            
            if user is None:
                raise HTTPException(status_code=401, detail="Invalid API key")

            user_model = User(id=str(user[0]), session_token=str(user[1]))  # Create a User instance
            return user_model
    except Exception:
        return

def generate_session_token():
    return secrets.token_hex(16)

class User(BaseModel):
    id: str
    session_token: str

@app.post("/login")
async def get_session_token(user=Depends(authenticate_user)):
    session_token = generate_session_token()
    async with app.state.pool.acquire() as conn:
        try:
            query = "UPDATE users SET session_token = $1 WHERE user_id = $2"
            await conn.execute(query, session_token, user.id)
            return {"session token": session_token}
        except Exception as e:
            raise HTTPException(status_code=401, detail="Invalid API key")


@app.post("/alert")
async def alert_users(request: Request):
    async with app.state.pool.acquire() as conn:
        try:
            data = await request.json()
            auth_key = data["oni_auth_key"]
            if auth_key != os.getenv("ONI_AUTH_KEY"):
                raise HTTPException(status_code=401, detail="Improper Authorization Key")
            
            if data["condition"] == "buy":
                await format_and_post_to_discord(data)
                await send_buy_to_valid_users(data)
                query = """
                    INSERT INTO buys (tv_buy_id, ticker, script_version)
                    VALUES ($1, $2, $3)
                    """
                await conn.execute(query, data["tv_buy_id"], data["ticker"], data["script_version"])
            
            if data["condition"] == "stop":
                await send_stop_to_users(data)
                
        except Exception as e:
            print(e)

@app.post("/user_endpoint/{api_key}/{session_token}")
async def handle_user_messages(api_key: str, session_token: str, request: Request):
    return_data = await request.json()
    return_data = ast.literal_eval(return_data)
    
    if session_token is None or api_key is None:
        raise HTTPException(status_code=401, detail="Invalid Credentials")
    
    async with app.state.pool.acquire() as conn:
        query = "SELECT user_id, payment_boolean FROM users WHERE session_token = $1 AND api_key = $2"
        user = await conn.fetchrow(query, session_token, api_key)
        if user["payment_boolean"] is False:
            raise HTTPException(status_code=402, detail="Subsciption Required")
        
        if user["user_id"] and len(return_data.get("condition")) > 0:
            await handle_client_condition(user["user_id"], return_data)
        
@app.post("/user_endpoint/trade_data/{api_key}/{session_token}")
async def give_trade_data(api_key: str, session_token: str):
    if session_token is None or api_key is None:
        raise HTTPException(status_code=401, detail="Invalid Credentials")
    
    async with app.state.pool.acquire() as conn:
        query = "SELECT user_id, payment_boolean FROM users WHERE session_token = $1 AND api_key = $2"
        user = await conn.fetchrow(query, session_token, api_key)
        
        if user["payment_boolean"] is False:
            raise HTTPException(status_code=402, detail="Subsciption Required")
        
        query = '''
        SELECT tv_buy_id, ticker, executed_flag, net_amount, timestamp
        FROM user_trades
        WHERE user_id = $1
        '''
        trade_data = await conn.fetch(query, user["user_id"])
        
        return trade_data
    
async def handle_client_condition(user_id, data):
    async with app.state.pool.acquire() as conn:
        print(data, type(data))
        try:
            if data.get('condition') == 'buy':
                query = """
                INSERT INTO user_trades (user_id, tv_buy_id, market_buy_id, take_profit_id, ticker)
                VALUES (
                    (SELECT user_id FROM users WHERE user_id = $1),
                    $2,
                    $3,
                    $4,
                    $5
                )
                """
                await conn.execute(query, user_id, data.get('tv_buy_id'), data.get('market_buy_id'), data.get('take_profit_id'), data.get('ticker'))
            
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
            if data.get('condition') == 'executed':
                orders = data.get('orders')
                if orders is not None:
                    for order in orders:
                        tv_buy_id = order.get('tv_buy_id')
                        net_amount = order.get('net_amount')
                        query = """
                        UPDATE user_trades
                        SET stop_loss_id = $1, executed_flag = true, net_amount = $2
                        WHERE user_id = $3
                        AND tv_buy_id = $4
                        """
                        await conn.execute(query, 'INVALID', net_amount, user_id, tv_buy_id)

        except Exception as e:
            print("Error in Handling Message: ", e)

async def send_buy_to_valid_users(data: dict):
    # If the user has the payment_boolean as true, then sends to those users
    # The data needs to consist of the TradingView String.
    async with app.state.pool.acquire() as conn:
        query = """
        SELECT user_id
        FROM users
        WHERE payment_boolean = True
        """
        users = await conn.fetch(query)
        for user in users:
            user_id = user["user_id"]

            query = """
            INSERT INTO notifications (user_id, notification, notification_type, is_sent)
            VALUES ($1, $2, $3, $4)
            """
            notification_data = {
                "user_id": user_id,
                "notification": str(data),
                "notification_type": "timeout", # Timeout notification for buys
                "is_sent": False,
            }
            await conn.execute(query, *notification_data.values())
        
async def send_stop_to_users(data: dict):
    # Adds the stop message to the notification queue with user specific info
    async with app.state.pool.acquire() as conn:
        condition = data["condition"]
        ticker = data["ticker"]
        tv_buy_id = data["tv_buy_id"]
        
        query = """
        SELECT user_id, market_buy_id, take_profit_id
        FROM user_trades
        WHERE tv_buy_id = $1
        """
        users = await conn.fetch(query, tv_buy_id)
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
                user_id = str(row['user_id'])
                
                query = """
                INSERT INTO notifications (user_id, notification, notification_type, is_sent)
                VALUES ($1, $2, $3, $4)
                """
                notification_data = {
                    "user_id": user_id,
                    "notification": str(response),
                    "notification_type": "persistent", # Persistent notification for stop
                    "is_sent": False,
                }
                await conn.execute(query, *notification_data.values())
        

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
    async with app.state.pool.acquire() as conn:
        while True:
            # Sends message every 3 hours
            await asyncio.sleep(3 * 60 * 60)
            
            query = """
            SELECT user_id, tv_buy_id, market_buy_id, take_profit_id
            FROM user_trades
            WHERE executed_flag = false
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
                    query = """
                    SELECT payment_boolean
                    FROM users
                    WHERE user_id = $1
                    """
                    payment_status = await conn.fetchval(query, user_id)

                    if payment_status:
                        message = {
                            "condition": "check",
                            "orders": orders
                        }
                        # Insert or update the notification in the notifications table
                        query = """
                        INSERT INTO notifications (user_id, notification, notification_type, is_sent)
                        VALUES ($1, $2, $3, $4)
                        ON CONFLICT (user_id) WHERE (notification_type = 'one-time') DO UPDATE
                        SET notification = $2, notification_type = $3, is_sent = $4
                        """
                        notification_data = {
                            "user_id": user_id,
                            "notification": str(message),
                            "notification_type": "one-time",
                            "is_sent": False,
                        }
                        await conn.execute(query, *notification_data.values())
                    
async def remove_expired_buys():
    async with app.state.pool.acquire() as conn:
        while True:
            # Checks every 30 minutes
            await asyncio.sleep(30 * 60)
            
            # Calculate the datetime one hour ago from now
            one_hour_ago = datetime.datetime.utcnow() - datetime.timedelta(hours=1)

            delete_query = '''
            DELETE FROM notifications
            WHERE notification_type = 'timeout' AND created_at < $1
            '''
            await conn.execute(delete_query, one_hour_ago)

async def remove_sent_notifications():
    async with app.state.pool.acquire() as conn:
        while True:
            # Checks every 45 minutes
            await asyncio.sleep(45 * 60)
            
            query = '''
            SELECT id
            FROM notifications
            WHERE is_sent = true
            '''
            rows = await conn.fetch(query)

            notification_ids = [row['id'] for row in rows]

            if notification_ids:
                # Perform the bulk deletion using a single query
                delete_query = '''
                DELETE FROM notifications
                WHERE id = ANY($1)
                '''
                await conn.execute(delete_query, notification_ids)
                
@app.get("/wait_for_notification/{session_token}")
async def wait_for_notification(session_token: str):
    async with app.state.pool.acquire() as conn:
        while True:
            user = await conn.fetchrow('''
                SELECT user_id, payment_boolean
                FROM users
                WHERE session_token = $1    
            ''', session_token)
            user_id = user.get("user_id")
            payment_boolean = user.get("payment_boolean")
            
            if payment_boolean is False:
                raise HTTPException(status_code=402, detail="Subsciption Required")
            
            if user_id or payment_boolean is None:
                raise HTTPException(status_code=401, detail="Unauthorized User")
            
            notification = await conn.fetchrow('''
                SELECT id, notification
                FROM notifications
                WHERE user_id = $1 AND is_sent = false
                ORDER BY id ASC
                LIMIT 1
            ''', user_id)
        
            if notification is not None:
                await conn.execute('''
                    UPDATE notifications
                    SET is_sent = true
                    WHERE id = $1
                ''', notification['id'])
                return notification['notification']
            
            await asyncio.sleep(60)