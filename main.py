from fastapi import FastAPI, HTTPException, Request
from dotenv import load_dotenv
import boto3, os, json

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
            Payload=f'{encoded_data}'  # Optional payload data to pass to the Lambda function
        )
        
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))
