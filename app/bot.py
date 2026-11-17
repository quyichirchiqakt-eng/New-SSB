import asyncio
import os
import logging
from aiohttp import web
from datetime import datetime, timezone, timedelta
from collections import defaultdict
from aiogram import Bot, Dispatcher, F
from aiogram.filters import CommandStart
from aiogram.types import Message, CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup
from sqlalchemy import select, func, BigInteger, ForeignKey, UniqueConstraint
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Mapped, mapped_column
from .db import settings, Session, init_db, Survey, Candidate, Employee, Channel, Vote, recent_vote_activity, Base, engine

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
                Employee.name
            )
        )

        rows = (await db.execute(stmt)).all()

    lines = [
        f"📊 {survey.title}",
        "",
        "Ovoz berish natijalari:",
        ""
    ]

    for index, row in enumerate(rows, 1):
        lines.append(
            f"{index}. {row.name} — {row.votes} ta ovoz"
        )

    lines.append("")
    lines.append(
        f"🗳 Jami ovozlar: "
        f"{sum(row.votes for row in rows)}"
    )

    await message.answer("\n".join(lines))


@dp.message(CommandStart())
async def start(message: Message):
    payload = (message.text or '').split(maxsplit=1)

    if len(payload) == 2:
        arg = payload[1]

        if arg.startswith('results') and arg[7:].isdigit():
            await show_results(message, int(arg[7:]))
            return

        if arg.startswith('s') and arg[1:].isdigit():
            await show_survey(
                message,
                int(arg[1:]),
                message.from_user.id
            )
            return

    await message.answer(
        "Assalomu alaykum! So‘rovnoma havolasi orqali "
        "ovoz berishingiz mumkin."
    )


# Administratorlar ro‘yxati Render Environment orqali beriladi:
# AUDIT_ADMIN_IDS=123456789,987654321
# Ro‘yxat bo‘sh bo‘lsa, audit buyrug‘i hech kimga ochilmaydi.
def audit_admin_ids():
    result = set()
    for raw in os.environ.get('AUDIT_ADMIN_IDS', '').split(','):
        raw = raw.strip()
        if raw.isdigit():
            result.add(int(raw))
    return result


@dp.message(F.text.regexp(r'^/audit(?:@\w+)?(?:\s|$)'))
async def audit_votes(message: Message):
    """So‘nggi 24 soatdagi ovozlar oqimini ko‘rib chiqish; ovozlarni o‘zgartirmaydi."""
    if not message.from_user or message.from_user.id not in audit_admin_ids():
        await message.answer('Bu buyruq faqat ruxsat berilgan administrator uchun.')
        return

    parts = (message.text or '').split()
    if len(parts) == 3 and parts[1].isdigit() and int(parts[1]) > 0 and parts[2].lower() == 'all':
        await send_full_audit(message, int(parts[1]))
        return
    if len(parts) != 2 or not parts[1].isdigit() or int(parts[1]) < 1:
        await message.answer('Foydalanish: /audit 9 yoki /audit 9 all')
        return

    survey_id = int(parts[1])
    async with Session() as db:
        survey = await db.get(Survey, survey_id)
        if not survey:
            await message.answer('So‘rovnoma topilmadi.')
            return
        activity, truncated = await recent_vote_activity(db, survey_id)
        candidate_ids = {candidate_id for candidate_id, _ in activity}
        names = {}
        if candidate_ids:
            stmt = (select(Candidate.id, Employee.name)
                    .join(Employee, Employee.id == Candidate.employee_id)
                    .where(Candidate.id.in_(candidate_ids)))
            names = dict((await db.execute(stmt)).all())

    # Bir daqiqalik vaqt oraliqlarida har bir nomzod bo‘yicha sanash.
    per_minute = defaultdict(int)
    for candidate_id, created_at in activity:
        minute = created_at.replace(second=0, microsecond=0)
        per_minute[(candidate_id, minute)] += 1

    # 1 daqiqada 10+ ovoz: faqat tekshirish signali, firibgarlik dalili emas.
    spikes = sorted(
        ((count, cid, minute) for (cid, minute), count in per_minute.items()
         if count >= 10),
        reverse=True
    )
    lines = [f'🛡️ {survey.title}',
             'So‘nggi 24 soatdagi ovozlar tahlili',
             f'Ko‘rib chiqilgan ovozlar: {len(activity)} ta',
             'Mezon: bir nomzodga 1 daqiqada 10 ta yoki undan ko‘p ovoz.', '']
    if not spikes:
        lines.append('✅ Belgilangan mezon bo‘yicha keskin faollik topilmadi.')
    else:
        lines.append(f'⚠️ Tekshirishga arziydigan oraliqlar: {len(spikes)} ta')
        for count, cid, minute in spikes[:12]:
            lines.append(f'• {names.get(cid, "Nomzod " + str(cid))}: '
                         f'{count} ta — {minute:%d.%m.%Y %H:%M} UTC')
        if len(spikes) > 12:
            lines.append(f'... yana {len(spikes) - 12} ta oraliq')
    if truncated:
        lines.append('⚠️ Oxirgi 5000 ovoz tahlil qilindi; natija to‘liq emas.')
    lines.append('ℹ️ Bu faqat signal. Ovozlar o‘chirilmaydi va soxta deb belgilanmaydi.')
    await message.answer('\n'.join(lines))


async def send_full_audit(message: Message, survey_id: int):
    """PostgreSQL ichida daqiqalik guruhlash; barcha ovozlarni sanaydi."""
    async with Session() as db:
        survey = await db.get(Survey, survey_id)
        if not survey:
            await message.answer('So‘rovnoma topilmadi.')
            return
        minute_expr = func.date_trunc('minute', Vote.created_at)
        stmt = (select(Vote.candidate_id, minute_expr.label('minute'),
                       func.count(Vote.id).label('count'))
                .where(Vote.survey_id == survey_id)
                .group_by(Vote.candidate_id, minute_expr)
                .order_by(minute_expr))
        rows = (await db.execute(stmt)).all()
        names = dict((await db.execute(
            select(Candidate.id, Employee.name)
            .join(Employee, Employee.id == Candidate.employee_id)
            .where(Candidate.survey_id == survey_id)
        )).all())

    total = sum(row.count for row in rows)
    candidate_counts = defaultdict(int)
    hourly_counts = defaultdict(int)
    spikes = []
    for row in rows:
        candidate_counts[row.candidate_id] += row.count
        hour = row.minute.replace(minute=0, second=0, microsecond=0)
        hourly_counts[hour] += row.count
        if row.count >= 10:
            spikes.append((row.count, row.candidate_id, row.minute))

    spikes.sort(key=lambda item: item[0], reverse=True)
    busiest = sorted(hourly_counts.items(), key=lambda item: item[1], reverse=True)
    lines = [f'🛡️ {survey.title}', 'Butun davr bo‘yicha ovozlar tahlili',
             f'Jami tekshirilgan ovozlar: {total} ta',
             'Mezon: bir nomzodga 1 daqiqada 10+ ovoz.', '',
             '📊 Nomzodlar kesimida:']
    for cid, count in sorted(candidate_counts.items(), key=lambda x: x[1], reverse=True):
        lines.append(f'• {names.get(cid, "Nomzod " + str(cid))}: {count} ta')
    if not candidate_counts:
        lines.append('Ovozlar mavjud emas.')
    lines.extend(['', '🕒 Eng faol soatlar (UTC):'])
    for hour, count in busiest[:8]:
        lines.append(f'• {hour:%d.%m.%Y %H:00} — {count} ta')
    if not busiest:
        lines.append('Ma’lumot yo‘q.')
    lines.extend(['', f'⚠️ Keskin faollik oraliqlari: {len(spikes)} ta'])
    for count, cid, minute in spikes[:10]:
        lines.append(f'• {names.get(cid, "Nomzod " + str(cid))}: '
                     f'{count} ta — {minute:%d.%m.%Y %H:%M} UTC')
    if len(spikes) > 10:
        lines.append(f'... yana {len(spikes) - 10} ta oraliq')
    if not spikes:
        lines.append('✅ Belgilangan mezon bo‘yicha keskin faollik topilmadi.')
    lines.append('ℹ️ Bu signal, soxtalik isboti emas. Ovozlar o‘zgartirilmaydi.')

    # Telegram xabari 4096 belgidan oshmasligi uchun qismlarga bo‘lamiz.
    chunk = ''
    for line in lines:
        if len(chunk) + len(line) + 1 > 3800:
            await message.answer(chunk)
            chunk = ''
        chunk += line + '\n'
    if chunk:
        await message.answer(chunk)


# 4-bosqich: yakuniy natijalarni administrator tasdig‘i bilan e’lon qilish.
# Yangi jadval faqat e’lon tarixini saqlaydi; votes jadvaliga tegmaydi.
class FinalPublication(Base):
    __tablename__ = 'final_publications'
    __table_args__ = (UniqueConstraint('survey_id', 'chat_id', name='uq_final_publication'),)
    id: Mapped[int] = mapped_column(primary_key=True)
    survey_id: Mapped[int] = mapped_column(ForeignKey('surveys.id'))
    chat_id: Mapped[int] = mapped_column(BigInteger)
    message_id: Mapped[int] = mapped_column(BigInteger)


_final_publish_lock = asyncio.Lock()


async def final_report(db, survey_id: int):
    survey = await db.get(Survey, survey_id)
    if not survey:
        return None, None
    stmt = (select(Employee.name, func.count(Vote.id).label('votes'))
            .select_from(Candidate)
            .join(Employee, Employee.id == Candidate.employee_id)
            .outerjoin(Vote, (Vote.candidate_id == Candidate.id) & (Vote.survey_id == survey_id))
            .where(Candidate.survey_id == survey_id)
            .group_by(Candidate.id, Employee.name)
            .order_by(func.count(Vote.id).desc(), Employee.name))
    rows = (await db.execute(stmt)).all()
    total = sum(r.votes for r in rows)
    lines = [f'🏆 {survey.title}', '📊 YAKUNIY NATIJALAR', '']
    for i, r in enumerate(rows, 1):
        pct = (r.votes / total * 100) if total else 0
        lines.append(f'{i}. {r.name} — {r.votes} ta ({pct:.2f}%)')
    lines.extend(['', f'🗳 Jami ovozlar: {total} ta'])
    if rows and total:
        max_votes = rows[0].votes
        winners = [r.name for r in rows if r.votes == max_votes]
        lines.append('🥇 G‘olib: ' + ', '.join(winners))
    else:
        lines.append('G‘olib aniqlanmadi: ovozlar mavjud emas.')
    return survey, '\n'.join(lines)


@dp.message(F.text.regexp(r'^/final(?:@\w+)?(?:\s|$)'))
async def final_preview(message: Message):
    if not message.from_user or message.from_user.id not in audit_admin_ids():
        await message.answer('Bu buyruq faqat ruxsat berilgan administrator uchun.')
        return
    parts = (message.text or '').split()
    if len(parts) != 2 or not parts[1].isdigit() or int(parts[1]) < 1:
        await message.answer('Foydalanish: /final 9')
        return
    sid = int(parts[1])
    async with Session() as db:
        survey, report = await final_report(db, sid)
        if not survey:
            await message.answer('So‘rovnoma topilmadi.')
            return
        if survey.ends_at > datetime.now(timezone.utc) and survey.active:
            await message.answer('So‘rovnoma hali davom etmoqda. Yakuniy e’lon qilish mumkin emas.')
            return
        channels = (await db.scalars(select(Channel).where(Channel.survey_id == sid))).all()
        published = set((await db.scalars(select(FinalPublication.chat_id)
                                          .where(FinalPublication.survey_id == sid))).all())
    await message.answer('🔎 Administrator uchun ko‘rib chiqish:\n\n' + report)
    buttons = [[InlineKeyboardButton(text=f'📢 Kanalga e’lon qilish ({ch.chat_id})',
                                     callback_data=f'finalpub:{sid}:{ch.chat_id}')]
               for ch in channels if ch.chat_id not in published]
    if buttons:
        await message.answer('Natijalar to‘g‘ri bo‘lsa, e’lon qilinadigan kanalni tanlang:',
                             reply_markup=keyboard(buttons))
    else:
        await message.answer('E’lon qilinmagan bog‘langan kanallar yo‘q. '
                             'Kanal belgilanmagan yoki natijalar avval e’lon qilingan.')


@dp.callback_query(F.data.startswith('finalpub:'))
async def final_publish(call: CallbackQuery):
    if not call.from_user or call.from_user.id not in audit_admin_ids():
        await call.answer('Ruxsat yo‘q.', show_alert=True)
        return
    try:
        _, sid_raw, chat_raw = call.data.split(':')
        sid, chat_id = int(sid_raw), int(chat_raw)
    except (ValueError, AttributeError):
        await call.answer('Noto‘g‘ri so‘rov.', show_alert=True)
        return
    async with _final_publish_lock:
        async with Session() as db:
            survey, report = await final_report(db, sid)
            if not survey:
                await call.answer('So‘rovnoma topilmadi.', show_alert=True)
                return
            if survey.ends_at > datetime.now(timezone.utc) and survey.active:
                await call.answer('So‘rovnoma hali davom etmoqda.', show_alert=True)
                return
            allowed = await db.scalar(select(Channel.id).where(
                Channel.survey_id == sid, Channel.chat_id == chat_id))
            if not allowed:
                await call.answer('Kanal bu so‘rovnomaga biriktirilmagan.', show_alert=True)
                return
            existing = await db.scalar(select(FinalPublication.id).where(
                FinalPublication.survey_id == sid, FinalPublication.chat_id == chat_id))
            if existing:
                await call.answer('Bu kanalga avval e’lon qilingan.', show_alert=True)
                return
            if len(report) > 4096:
                await call.answer('Hisobot Telegram xabariga sig‘maydi.', show_alert=True)
                return
            try:
                posted = await bot.send_message(chat_id, report)
            except Exception:
                logging.exception('Yakuniy natijani kanalga yuborib bo‘lmadi')
                await call.answer('Kanalga yuborilmadi. Botning kanal huquqlarini tekshiring.', show_alert=True)
                return
            db.add(FinalPublication(survey_id=sid, chat_id=chat_id, message_id=posted.message_id))
            try:
                await db.commit()
            except Exception:
                logging.exception('E’lon tarixi saqlanmadi; kanalga xabar yuborilgan bo‘lishi mumkin')
                await db.rollback()
                await call.answer('Xabar yuborildi, lekin tarix saqlanmadi. Qayta bosmang!', show_alert=True)
                return
    await call.answer('Yakuniy natijalar e’lon qilindi.')
    await call.message.answer(f'✅ {sid}-so‘rovnoma yakuniy natijalari {chat_id} kanaliga e’lon qilindi.')


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

async def update_results_button():
    """Update the existing Survey 4 channel post without replacing its photo/caption."""
    markup = keyboard([
        [InlineKeyboardButton(text="🗳 Ovoz berish", url="https://t.me/new_SSB_bot?start=s4")],
        [InlineKeyboardButton(text="📊 Natijalarni ko‘rish", url="https://t.me/new_SSB_bot?start=results4")],
    ])
    await bot.edit_message_reply_markup(
        chat_id=-1002872785266,
        message_id=48,
        reply_markup=markup,
    )


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
    # E’lon tarixini saqlash uchun alohida jadval yaratish (mavjud ovozlarga tegmaydi).
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    runner = await start_web_server()

    try:
        await bot.delete_webhook(drop_pending_updates=False)

        try:
            await update_results_button()
        except Exception:
            logging.exception("Post tugmalari yangilab bo‘lmadi")

        try:
            await update_survey8_post()
        except Exception:
            logging.exception("Survey 8 post tugmalari yangilanmadi")

        await dp.start_polling(
            bot,
            handle_signals=True,
            close_bot_session=False
        )
    finally:
        await runner.cleanup()
        await bot.session.close()



async def update_survey8_post():
    buttons = InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="🗳 Ovoz berish",
                    url="https://t.me/new_SSB_bot?start=s8"
                )
            ],
            [
                InlineKeyboardButton(
                    text="📊 Natijalarni ko‘rish",
                    url="https://t.me/new_SSB_bot?start=results8"
                )
            ]
        ]
    )

    await bot.edit_message_reply_markup(
        chat_id="@ohangaronttibbiyotbirlashmasi",
        message_id=8613,
        reply_markup=buttons
    )

if __name__ == '__main__':
    asyncio.run(main())
