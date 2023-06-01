from fastapi import FastAPI, HTTPException, Request
from dotenv import load_dotenv
import boto3, os, json, requests

load_dotenv()
app = FastAPI(
    #docs_url=None, # Disable docs (Swagger UI) for PROD
    #redoc_url=None, # Disable redoc for PROD
)

# @app.post("/webhook")
# async def webhook(request: Request):
#     try:
#         try:
#             data = await request.json()
#         except json.JSONDecodeError:
#             try:
#                 # Attempt to convert the string into JSON
#                 data = json.loads(await request.body())
#             except json.JSONDecodeError:
#                 raise HTTPException(status_code=400, detail="Invalid JSON data")

#         data = await request.json()
#         encoded_data = json.dumps(data)
    
#         ACCESS_KEY = os.getenv('ACCESS_KEY')
#         SECRET_KEY = os.getenv('SECRET_KEY')
#         region = 'us-east-2'

#         lambda_client = boto3.client('lambda', aws_access_key_id=ACCESS_KEY,
#                                     aws_secret_access_key=SECRET_KEY,
#                                     region_name=region)
        
#         response = lambda_client.invoke(
#             FunctionName='testfunction',
#             InvocationType='Event',  # Use 'Event' for asynchronous invocation
#             Payload=encoded_data  # Optional payload data to pass to the Lambda function
#         )
        
#     except Exception as e:
#         raise HTTPException(status_code=400, detail=str(e))

@app.post("/alert")
async def format_and_post_to_discord(data: dict):
    try:
        ticker = data["ticker"]
        color = data["color"]
        entry = data["entry"]
        TP = data["TP"]
        stopLoss = data["stopLoss"]
        timestamp = data["timestamp"]
    except KeyError as e:
        raise HTTPException(status_code=400, detail=f"Missing required field: {str(e)}")

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
        raise HTTPException(status_code=500, detail=f"Failed to post to Discord: {str(e)}")

    return {"message": "Formatted JSON posted to Discord successfully"}