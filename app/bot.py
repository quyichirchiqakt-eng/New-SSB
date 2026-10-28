import asyncio
import os
import logging
from aiohttp import web
from datetime import datetime, timezone
from aiogram import Bot, Dispatcher, F
from aiogram.filters import CommandStart
from aiogram.types import Message, CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from .db import settings, Session, init_db, Survey, Candidate, Employee, Channel, Vote

bot=Bot(os.environ["BOT_TOKEN"])
PORT = int(os.environ.get("PORT", "10000"))
dp=Dispatcher()

def keyboard(rows):
    return InlineKeyboardMarkup(inline_keyboard=rows)

async def subscribed(user_id:int, survey_id:int):
    async with Session() as db:
        channels=(await db.scalars(select(Channel).where(Channel.survey_id==survey_id))).all()
    missing=[]
    for ch in channels:
        try:
            member=await bot.get_chat_member(ch.chat_id,user_id)
            if member.status not in ('creator','administrator','member') and not (member.status=='restricted' and getattr(member,'is_member',False)):
                missing.append(ch)
        except Exception:
            # Fail closed: do not permit votes if verification is unavailable.
            missing.append(ch)
    return missing

async def show_survey(target, survey_id:int, user_id:int):
    async with Session() as db:
        survey=await db.get(Survey,survey_id)
        if not survey or not survey.active or survey.ends_at <= datetime.now(timezone.utc):
            await target.answer('Bu so‘rovnoma faol emas yoki muddati tugagan.')
            return
        voted=await db.scalar(select(Vote.id).where(Vote.survey_id==survey_id,Vote.telegram_user_id==user_id))
        if voted:
            await target.answer('Siz bu so‘rovnomada ovoz bergansiz. Rahmat!')
            return
    missing=await subscribed(user_id,survey_id)
    if missing:
        buttons=[[InlineKeyboardButton(text=f'📢 Kanal {i+1}',url=ch.invite_url)] for i,ch in enumerate(missing)]
        buttons.append([InlineKeyboardButton(text='✅ Obunani tekshirish',callback_data=f'check:{survey_id}')])
        await target.answer('Ovoz berish uchun kanallarga obuna bo‘ling:',reply_markup=keyboard(buttons))
    else:
        await show_candidates(target,survey_id,0)

async def show_candidates(target,survey_id:int,page:int):
    async with Session() as db:
        survey=await db.get(Survey,survey_id)
        if not survey or not survey.active or survey.ends_at<=datetime.now(timezone.utc):
            await target.answer('So‘rovnoma yopilgan.')
            return
        rows=(await db.execute(select(Candidate.id,Employee.name,Employee.region).join(Employee,Employee.id==Candidate.employee_id).where(Candidate.survey_id==survey_id,Employee.active==True).order_by(Employee.name).offset(page*15).limit(15))).all()
    if not rows and page>0:
        await target.answer('Boshqa nomzodlar yo‘q.')
        return
    buttons=[[InlineKeyboardButton(text=f'🟢 {r.name[:36]} — {r.region[:18]}',callback_data=f'vote:{survey_id}:{r.id}')] for r in rows[:8]]
    nav=[]
    if page>0: nav.append(InlineKeyboardButton(text='⬅️',callback_data=f'page:{survey_id}:{page-1}'))
    if len(rows)>8: nav.append(InlineKeyboardButton(text='➡️',callback_data=f'page:{survey_id}:{page+1}'))
    if nav: buttons.append(nav)
    await target.answer(f'🏆 {survey.title}\n\n{survey.description}\n\nNomzodni tanlang:',reply_markup=keyboard(buttons))

@dp.message(CommandStart())
async def start(message:Message):
    payload=(message.text or '').split(maxsplit=1)
    if len(payload)==2 and payload[1].startswith('s') and payload[1][1:].isdigit():
        await show_survey(message,int(payload[1][1:]),message.from_user.id)
    else:
        await message.answer('Assalomu alaykum! So‘rovnoma havolasi orqali ovoz berishingiz mumkin.')

@dp.callback_query(F.data.startswith('check:'))
async def check(call:CallbackQuery):
    await call.answer()
    await show_survey(call.message,int(call.data.split(':')[1]),call.from_user.id)

@dp.callback_query(F.data.startswith('page:'))
async def page(call:CallbackQuery):
    await call.answer()
    _,survey_id,page=call.data.split(':')
    if int(page)<0: return
    await show_candidates(call.message,int(survey_id),int(page))

@dp.callback_query(F.data.startswith('vote:'))
async def vote(call:CallbackQuery):
    await call.answer()
    _,sid,cid=call.data.split(':')
    sid,cid=int(sid),int(cid)
    if await subscribed(call.from_user.id,sid):
        await call.message.answer('Avval majburiy kanallarga obuna bo‘ling. /start havolasidan qayta kiring.')
        return
    async with Session() as db:
        survey=await db.get(Survey,sid)
        candidate=await db.get(Candidate,cid)
        if not survey or not survey.active or survey.ends_at<=datetime.now(timezone.utc) or not candidate or candidate.survey_id!=sid:
            await call.message.answer('Ovoz berish muddati tugagan yoki nomzod noto‘g‘ri.')
            return
        employee=await db.get(Employee,candidate.employee_id)
        if not employee or not employee.active or employee.organization_id!=survey.organization_id:
            await call.message.answer('Nomzod faol emas.')
            return
        db.add(Vote(survey_id=sid,candidate_id=cid,telegram_user_id=call.from_user.id))
        try:
            await db.commit()
        except IntegrityError:
            await db.rollback()
            await call.message.answer('Siz allaqachon ovoz bergansiz.')
            return
    await call.message.answer('✅ Ovozingiz qabul qilindi. Rahmat!')

async def health(request):
    return web.json_response({"status": "ok", "service": "surveybot"})

async def start_web_server():
    app = web.Application()
    app.router.add_get("/", health)
    app.router.add_get("/health", health)
    runner = web.AppRunner(app)
    await runner.setup()
    try:
        site = web.TCPSite(runner, host="0.0.0.0", port=PORT)
        await site.start()
    except Exception:
        await runner.cleanup()
        raise
    logging.info("Health server listening on %s", PORT)
    return runner

async def main():
    logging.basicConfig(level=logging.INFO)
    await init_db()
    runner = await start_web_server()
    try:
        await bot.delete_webhook(drop_pending_updates=False)
        await dp.start_polling(bot, handle_signals=True, close_bot_session=False)
    finally:
        await runner.cleanup()
        await bot.session.close()

if __name__ == '__main__':
    asyncio.run(main())
