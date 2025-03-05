import asyncio
from fastapi import FastAPI, Form, Request, Response, BackgroundTasks
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.templating import Jinja2Templates
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from sqlalchemy import create_engine, Column, Integer, String, DateTime
from sqlalchemy.ext.declarative import declarative_base
from sqlalchemy.orm import sessionmaker
import os
from dotenv import load_dotenv
import schedule
import requests
from datetime import datetime, timedelta, timezone
import logging

# Logging setup
logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
logger = logging.getLogger(__name__)

# Load environment variables
load_dotenv()

# FastAPI setup
app = FastAPI()
templates = Jinja2Templates(directory="templates")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:3000"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Database setup
DATABASE_URL = os.getenv("DATABASE_URL")
if not DATABASE_URL:
    logger.error("DATABASE_URL is not set in environment variables")
    DATABASE_URL = "postgresql://user:password@postgres:5432/reminder_db"  # Fallback
logger.info(f"Using DATABASE_URL: {DATABASE_URL}")
engine = create_engine(DATABASE_URL)
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
Base = declarative_base()

class Task(Base):
    __tablename__ = "tasks"
    id = Column(Integer, primary_key=True)
    task = Column(String)
    due = Column(DateTime)
    chat_id = Column(String)
    telegram_message_id = Column(Integer, nullable=True)

Base.metadata.create_all(engine)
SessionLocal = sessionmaker(bind=engine)

# Telegram API setup
BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
DEFAULT_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")
TELEGRAM_API = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
TELEGRAM_EDIT_API = f"https://api.telegram.org/bot{BOT_TOKEN}/editMessageText"

class CompleteTaskRequest(BaseModel):
    task_id: int

def send_telegram_reminder(task_id):
    logger.info(f"Checking reminder for task ID: {task_id}")
    with SessionLocal() as session:
        task = session.get(Task, task_id)
        if task:
            logger.info(f"Task found: {task.task}, Due: {task.due}, Chat ID: {task.chat_id}")
            
            # Convert due time to UTC and subtract 4 hours
            task_due_utc = task.due.replace(tzinfo=timezone.utc) - timedelta(hours=5, minutes=30)
            current_time_utc = datetime.now(timezone.utc)

            if task_due_utc <= current_time_utc and not task.telegram_message_id:
                message = f"Reminder: {task.task} is due now!"
                logger.info(f"Sending message to chat_id {task.chat_id}: {message}")
                try:
                    response = requests.post(TELEGRAM_API, json={"chat_id": task.chat_id, "text": message})
                    if response.status_code == 200:
                        logger.info("Message sent successfully")
                        response_data = response.json()
                        task.telegram_message_id = response_data['result']['message_id']
                        session.commit()
                        jobs = schedule.get_jobs(tag=task_id)
                        if jobs:
                            schedule.cancel_job(jobs[0])
                    else:
                        logger.error(f"Failed to send message: {response.status_code} - {response.text}")
                except Exception as e:
                    logger.error(f"Error sending Telegram message: {str(e)}")
            else:
                logger.info(f"Task not due yet or already sent: {task.due} (Adjusted: {task_due_utc})")
        else:
            logger.warning(f"Task {task_id} not found - cancelling job")
            jobs = schedule.get_jobs(tag=task_id)
            if jobs:
                schedule.cancel_job(jobs[0])
                
@app.get("/test")
async def test_endpoint():
    """ Test endpoint to verify if backend is running """
    logger.info("Test endpoint hit")
    return {"message": "Backend is alive"}

@app.get("/", response_class=HTMLResponse)
async def home(request: Request, response: Response):
    """ Renders tasks on the home page """
    logger.info("GET / endpoint hit")
    with SessionLocal() as session:
        tasks = session.query(Task).all()
        if "application/json" in request.headers.get("Accept", ""):
            return JSONResponse(content={"tasks": [{"id": t.id, "task": t.task, "due": t.due.isoformat(), "chat_id": t.chat_id} for t in tasks]})
        return templates.TemplateResponse("index.html", {"request": request, "tasks": tasks})

@app.post("/add-task")
async def add_task(task: str = Form(...), due: str = Form(...), chat_id: str = Form(default=DEFAULT_CHAT_ID)):
    """ Adds a task and schedules a reminder """
    logger.info(f"Adding task: {task}, Due: {due}, Chat ID: {chat_id}")
    with SessionLocal() as session:
        new_task = Task(task=task, due=datetime.fromisoformat(due), chat_id=chat_id)
        session.add(new_task)
        session.commit()
        task_id = new_task.id
        logger.info(f"Task added: ID {task_id}")

    # Schedule a reminder every 15 seconds
    schedule.every(15).seconds.do(send_telegram_reminder, task_id=task_id).tag(task_id)
    return {"message": "Task added"}

@app.post("/complete-task")
async def complete_task(request: CompleteTaskRequest):
    """ Marks a task as completed and cancels its reminder """
    task_id = request.task_id
    logger.info(f"Completing task ID: {task_id}")
    with SessionLocal() as session:
        task = session.get(Task, task_id)
        if task:
            if task.telegram_message_id:
                try:
                    response = requests.post(TELEGRAM_EDIT_API, json={
                        "chat_id": task.chat_id,
                        "message_id": task.telegram_message_id,
                        "text": f"Task '{task.task}' completed, Have a great Day",
                    })
                    if response.status_code == 200:
                        logger.info("Telegram message updated")
                    else:
                        logger.error(f"Failed to update Telegram: {response.status_code} - {response.text}")
                except Exception as e:
                    logger.error(f"Error updating Telegram: {str(e)}")
            session.delete(task)
            session.commit()
            jobs = schedule.get_jobs(tag=task_id)
            if jobs:
                schedule.cancel_job(jobs[0])
            return {"message": "Task completed"}
        else:
            return {"message": "Task not found"}, 404

async def run_scheduler():
    """ Runs the scheduler in a non-blocking way """
    logger.info("Scheduler started")
    while True:
        schedule.run_pending()
        await asyncio.sleep(1)  # Non-blocking sleep

@app.on_event("startup")
async def startup_event():
    """ Starts the scheduler on FastAPI startup """
    asyncio.create_task(run_scheduler())

if __name__ == "__main__":
    import threading
    threading.Thread(target=run_scheduler, daemon=True).start()
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=5000)  # Change 'localhost' to '0.0.0.0'

