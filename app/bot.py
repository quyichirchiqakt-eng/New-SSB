import asyncio
import os
import logging
from aiohttp import web
from datetime import datetime, timezone
from aiogram import Bot, Dispatcher, F
from aiogram.filters import CommandStart
from aiogram.types import Message, CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup
from sqlalchemy import select, func
from sqlalchemy.exc import IntegrityError
from .db import settings, Session, init_db, Survey, Candidate, Employee, Channel, Vote

bot=Bot(os.environ["BOT_TOKEN"])
PORT = int(os.environ.get("PORT", "10000"))
dp=Dispatcher()
PAGE_SIZE = 15

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
        rows=(await db.execute(select(Candidate.id,Employee.name,Employee.region).join(Employee,Employee.id==Candidate.employee_id).where(Candidate.survey_id==survey_id,Employee.active==True).order_by(Employee.name).offset(page*PAGE_SIZE).limit(PAGE_SIZE + 1))).all()
    if not rows and page>0:
        await target.answer('Boshqa nomzodlar yo‘q.')
        return
    buttons=[[InlineKeyboardButton(text=f'🟢 {r.name[:36]} — {r.region[:18]}',callback_data=f'vote:{survey_id}:{r.id}')] for r in rows[:PAGE_SIZE]]
    nav=[]
    if page>0: nav.append(InlineKeyboardButton(text='⬅️',callback_data=f'page:{survey_id}:{page-1}'))
    if len(rows)>PAGE_SIZE: nav.append(InlineKeyboardButton(text='➡️',callback_data=f'page:{survey_id}:{page+1}'))
    if nav: buttons.append(nav)
    await target.answer(f'🏆 {survey.title}\n\n{survey.description}\n\nNomzodni tanlang:',reply_markup=keyboard(buttons))

async def show_results(message: Message, survey_id: int):
    async with Session() as db:
        survey = await db.get(Survey, survey_id)

        if not survey:
            await message.answer("So‘rovnoma topilmadi.")
            return

        stmt = (
            select(
                Employee.name,
                func.count(Vote.id).label("votes")
            )
            .select_from(Candidate)
            .join(
                Employee,
                Employee.id == Candidate.employee_id
            )
            .outerjoin(
                Vote,
                (Vote.candidate_id == Candidate.id)
                & (Vote.survey_id == survey_id)
            )
            .where(Candidate.survey_id == survey_id)
            .group_by(Candidate.id, Employee.name)
            .order_by(
                func.count(Vote.id).desc(),
