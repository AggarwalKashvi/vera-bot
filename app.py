from fastapi import FastAPI
from pydantic import BaseModel
import subprocess

app = FastAPI()

class Request(BaseModel):
    message: str

@app.get("/")
def home():
    return {"status": "vera bot is running"}

@app.post("/chat")
def chat(req: Request):
    # call your existing bot logic
    response = "Your bot response here"
    return {"response": response}