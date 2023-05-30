from fastapi import FastAPI, HTTPException, Request
from dotenv import load_dotenv
import boto3, os, json, requests

load_dotenv()
app = FastAPI()

@app.get('/test')
def get_root():
    return {'Hi': 'There!'}

@app.post("/webhook")
async def webhook(request: Request):
    try:
        try:
            data = await request.json()
        except json.JSONDecodeError:
            try:
                # Attempt to convert the string into JSON
                data = json.loads(await request.body())
            except json.JSONDecodeError:
                raise HTTPException(status_code=400, detail="Invalid JSON data")

        data = await request.json()
        encoded_data = json.dumps(data)
    
        ACCESS_KEY = os.getenv('ACCESS_KEY')
        SECRET_KEY = os.getenv('SECRET_KEY')
        region = 'us-east-2'

        lambda_client = boto3.client('lambda', aws_access_key_id=ACCESS_KEY,
                                    aws_secret_access_key=SECRET_KEY,
                                    region_name=region)
        
        response = lambda_client.invoke(
            FunctionName='testfunction',
            InvocationType='Event',  # Use 'Event' for asynchronous invocation
            Payload=encoded_data  # Optional payload data to pass to the Lambda function
        )
        
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))

@app.post("/alert")
async def format_and_post(json_data: dict):
    # Extract data from the input JSON
    ticker = json_data.get("ticker")
    color = json_data.get("color")
    tp = json_data.get("TP")
    stop_loss = json_data.get("stopLoss")
    timestamp = json_data.get("timestamp")
    
    # Format the data into the desired JSON structure
    formatted_json = {
        "content": None,
        "embeds": [
            {
                "title": f"Sell Alert {ticker}",
                "url": f"https://www.tradingview.com/symbols/{ticker}/",
                "color": color,
                "fields": [
                    {
                        "name": "Sell",
                        "value": str(stop_loss)
                    }
                ],
                "footer": {
                    "text": "Free Beta Algo v1.0.0"
                },
                "timestamp": timestamp
            }
        ]
    }
    
    # Post the formatted JSON to the Discord webhook URL
    webhook_url = "https://discord.com/api/webhooks/1112897541919490190/8QpJ9Qxaw4eZR2BcU-hx4gtcFs-yUPZaBoHRT3Zjm21bvoAg3jsBSb3oea_ZmyfWb4gX"
    response = requests.post(webhook_url, json=formatted_json)
    
    return {"status": response.status_code}