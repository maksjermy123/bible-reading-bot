import os
from datetime import datetime, date, timedelta
from zoneinfo import ZoneInfo
from contextlib import asynccontextmanager
from fastapi import FastAPI, Request
from pydantic import BaseModel
from apscheduler.schedulers.asyncio import AsyncIOScheduler
import httpx

BOT_TOKEN = os.environ["BOT_TOKEN"]
BOT_USERNAME = os.environ["BOT_USERNAME"]
SUPABASE_URL = os.environ["SUPABASE_URL"]
SUPABASE_KEY = os.environ["SUPABASE_KEY"]
GITHUB_PAGES_URL = os.environ["GITHUB_PAGES_URL"]

TG_API = f"https://api.telegram.org/bot{BOT_TOKEN}"
SB_HEADERS = {
    "apikey": SUPABASE_KEY,
    "Authorization": f"Bearer {SUPABASE_KEY}",
    "Content-Type": "application/json",
    "Prefer": "return=representation",
}
MSK = ZoneInfo("Europe/Moscow")
SLOT_HOURS = {"morning": 8, "afternoon": 13, "evening": 20}
scheduler = AsyncIOScheduler(timezone=MSK)

@asynccontextmanager
async def lifespan(app: FastAPI):
    scheduler.start()
    yield
    scheduler.shutdown()

app = FastAPI(lifespan=lifespan)

async def sb_get(user_id: int):
    async with httpx.AsyncClient() as client:
        r = await client.get(
            f"{SUPABASE_URL}/rest/v1/plan_progress",
            headers=SB_HEADERS,
            params={"user_id": f"eq.{user_id}", "limit": "1"},
        )
        data = r.json()
        return data[0] if data else None

async def sb_upsert(payload: dict):
    async with httpx.AsyncClient() as client:
        await client.post(
            f"{SUPABASE_URL}/rest/v1/plan_progress",
            headers={**SB_HEADERS, "Prefer": "resolution=merge-duplicates,return=representation"},
            json=payload,
        )

async def sb_patch(user_id: int, payload: dict):
    async with httpx.AsyncClient() as client:
        await client.patch(
            f"{SUPABASE_URL}/rest/v1/plan_progress",
            headers=SB_HEADERS,
            params={"user_id": f"eq.{user_id}"},
            json=payload,
        )

async def sb_get_slot(slot: str) -> list:
    today = date.today().isoformat()
    async with httpx.AsyncClient() as client:
        r = await client.get(
            f"{SUPABASE_URL}/rest/v1/plan_progress",
            headers=SB_HEADERS,
            params={
                "notify_slot": f"eq.{slot}",
                "notify_on": "eq.true",
                "last_read_date": f"neq.{today}",
                "select": "user_id,plan_id,streak",
            },
        )
        return r.json() if r.status_code == 200 else []

async def tg_send(chat_id: int, text: str, reply_markup: dict = None):
    payload = {"chat_id": chat_id, "text": text, "parse_mode": "HTML"}
    if reply_markup:
        payload["reply_markup"] = reply_markup
    async with httpx.AsyncClient() as client:
        await client.post(f"{TG_API}/sendMessage", json=payload)

def mini_app_button(label: str = "Открыть план") -> dict:
    return {
        "inline_keyboard": [[{
            "text": label,
            "web_app": {"url": GITHUB_PAGES_URL}
        }]]
    }

class RegisterBody(BaseModel):
    user_id: int
    plan_id: str
    notify_slot: str = "morning"
    notify_on: bool = True

class ReadBody(BaseModel):
    user_id: int
    day_number: int

class SettingsBody(BaseModel):
    user_id: int
    notify_slot: str
    notify_on: bool

@app.get("/")
@app.head("/")
async def healthcheck():
    return {"status": "ok"}

@app.post("/plan/register")
async def register(body: RegisterBody):
    await sb_upsert({
        "user_id": body.user_id,
        "plan_id": body.plan_id,
        "start_date": date.today().isoformat(),
        "notify_slot": body.notify_slot,
        "notify_on": body.notify_on,
        "streak": 0,
        "max_streak": 0,
        "last_read_date": None,
        "days_done": [],
    })
    return {"ok": True}

@app.get("/plan/status")
async def status(user_id: int):
    row = await sb_get(user_id)
    if not row:
        return {"registered": False}
    return {"registered": True, **row}

@app.post("/plan/read")
async def mark_read(body: ReadBody):
    row = await sb_get(body.user_id)
    if not row:
        return {"ok": False, "error": "not registered"}
    today = date.today().isoformat()
    yesterday = (date.today() - timedelta(days=1)).isoformat()
    last = row.get("last_read_date")
    if last == today and body.day_number in (row.get("days_done") or []):
        return {"ok": True, "streak": row["streak"], "already": True}
    streak = row.get("streak", 0)
    if last == yesterday:
        streak += 1
    elif last != today:
        streak = 1
    max_streak = max(row.get("max_streak", 0), streak)
    days_done = list(set((row.get("days_done") or []) + [body.day_number]))
    await sb_patch(body.user_id, {
        "streak": streak,
        "max_streak": max_streak,
        "last_read_date": today,
        "days_done": days_done,
    })
    return {"ok": True, "streak": streak, "max_streak": max_streak}

@app.post("/plan/settings")
async def settings(body: SettingsBody):
    await sb_patch(body.user_id, {
        "notify_slot": body.notify_slot,
        "notify_on": body.notify_on,
    })
    return {"ok": True}

@app.post("/webhook")
async def webhook(request: Request):
    data = await request.json()
    message = data.get("message", {})
    if not message:
        return {"ok": True}
    chat_id = message.get("chat", {}).get("id")
    text = message.get("text", "")
    if not chat_id:
        return {"ok": True}
    if text.startswith("/start"):
        await tg_send(
            chat_id,
            "Привет! Читай Библию по плану и отслеживай прогресс.\n\nОткрой приложение:",
            mini_app_button("📖 Открыть план чтения"),
        )
    else:
        await tg_send(chat_id, "Открой план:", mini_app_button())
    return {"ok": True}

@scheduler.scheduled_job("cron", minute=0)
async def send_reminders():
    now_msk = datetime.now(MSK)
    current_hour = now_msk.hour
    for slot, hour in SLOT_HOURS.items():
        if current_hour != hour:
            continue
        users = await sb_get_slot(slot)
        for u in users:
            streak = u.get("streak", 0)
            streak_text = f"🔥 {streak} дней подряд" if streak > 0 else "Начни сегодня!"
            await tg_send(
                u["user_id"],
                f"📅 Время читать Библию\n{streak_text}",
                mini_app_button(),
            )
