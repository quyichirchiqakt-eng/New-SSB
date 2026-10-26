from datetime import datetime, timezone
from io import BytesIO
from pathlib import Path
from typing import Annotated
import secrets
from fastapi import FastAPI, Depends, HTTPException, Header, UploadFile, File
from fastapi.responses import FileResponse, StreamingResponse
from pydantic import BaseModel, Field
from sqlalchemy import select, func
from sqlalchemy.ext.asyncio import AsyncSession
from openpyxl import load_workbook, Workbook
from .db import Session, init_db, settings, Organization, Employee, Survey, Candidate, Channel, Vote

app = FastAPI(title='Survey Bot Admin API', docs_url=None, redoc_url=None, openapi_url=None)

@app.on_event('startup')
async def startup():
    await init_db()

async def auth(x_admin_key: Annotated[str | None, Header()] = None):
    if not x_admin_key or not secrets.compare_digest(x_admin_key, settings.admin_api_key):
        raise HTTPException(401, 'Admin kaliti noto‘g‘ri')

async def session():
    async with Session() as db:
        yield db

class OrgIn(BaseModel):
    name: str = Field(min_length=2, max_length=200)

class SurveyIn(BaseModel):
    organization_id: int
    title: str = Field(min_length=2, max_length=250)
    description: str = ''
    ends_at: datetime

class CandidateIn(BaseModel):
    employee_id: int

class ChannelIn(BaseModel):
    chat_id: int
    invite_url: str

@app.get('/')
async def home():
    return FileResponse(Path(__file__).parent / 'static' / 'index.html')

@app.post('/api/organizations', dependencies=[Depends(auth)])
async def add_org(data: OrgIn, db: AsyncSession = Depends(session)):
    org = Organization(name=data.name)
    db.add(org)
    await db.commit()
    await db.refresh(org)
    return {'id':org.id,'name':org.name}

@app.get('/api/organizations', dependencies=[Depends(auth)])
async def list_orgs(db: AsyncSession = Depends(session)):
    rows = (await db.scalars(select(Organization).order_by(Organization.id))).all()
    return [{'id':o.id,'name':o.name} for o in rows]

@app.post('/api/surveys', dependencies=[Depends(auth)])
async def add_survey(data: SurveyIn, db: AsyncSession = Depends(session)):
    if not await db.get(Organization, data.organization_id):
        raise HTTPException(404,'Tashkilot topilmadi')
    if data.ends_at.tzinfo is None or data.ends_at <= datetime.now(timezone.utc):
        raise HTTPException(400,'Kelajakdagi vaqt va vaqt zonasi kerak')
    survey = Survey(**data.model_dump())
    db.add(survey)
    await db.commit()
    await db.refresh(survey)
    return {'id':survey.id,'title':survey.title,'bot_link':f'https://t.me/{settings.public_bot_username}?start=s{survey.id}'}

@app.get('/api/surveys', dependencies=[Depends(auth)])
async def list_surveys(db: AsyncSession = Depends(session)):
    rows = (await db.scalars(select(Survey).order_by(Survey.id.desc()))).all()
    return [{'id':s.id,'organization_id':s.organization_id,'title':s.title,'ends_at':s.ends_at.isoformat(),'active':s.active,'bot_link':f'https://t.me/{settings.public_bot_username}?start=s{s.id}'} for s in rows]

@app.post('/api/surveys/{survey_id}/candidates', dependencies=[Depends(auth)])
async def add_candidate(survey_id:int, data:CandidateIn, db:AsyncSession=Depends(session)):
    survey, employee = await db.get(Survey,survey_id), await db.get(Employee,data.employee_id)
    if not survey or not employee or survey.organization_id != employee.organization_id:
        raise HTTPException(400,'Nomzod va so‘rovnoma tashkiloti mos emas')
    row = Candidate(survey_id=survey_id,employee_id=data.employee_id)
    db.add(row)
    await db.commit()
    return {'id':row.id}

@app.post('/api/surveys/{survey_id}/channels', dependencies=[Depends(auth)])
async def add_channel(survey_id:int, data:ChannelIn, db:AsyncSession=Depends(session)):
    if not await db.get(Survey,survey_id):
        raise HTTPException(404,'So‘rovnoma topilmadi')
    if not data.invite_url.startswith('https://t.me/'):
        raise HTTPException(400,'Telegram kanal havolasi kerak')
    row = Channel(survey_id=survey_id,**data.model_dump())
    db.add(row)
    await db.commit()
    return {'id':row.id}

@app.post('/api/organizations/{org_id}/employees/import', dependencies=[Depends(auth)])
async def import_employees(org_id:int, file:UploadFile=File(...), db:AsyncSession=Depends(session)):
    if not await db.get(Organization,org_id):
        raise HTTPException(404,'Tashkilot topilmadi')
    raw = await file.read(5_000_001)
    if len(raw)>5_000_000 or not (file.filename or '').lower().endswith('.xlsx'):
        raise HTTPException(400,'Faqat 5 MB gacha .xlsx')
    try:
        wb = load_workbook(BytesIO(raw),read_only=True,data_only=True)
        ws = wb.active
        rows = ws.iter_rows(values_only=True)
        header = [str(x or '').strip().lower() for x in next(rows)]
        required = ['id','name','region','position']
        if not all(c in header for c in required):
            raise ValueError('Ustunlar: id, name, region, position')
        idx = [header.index(c) for c in required]
        parsed = []
        for row in rows:
            if not any(v is not None for v in row): continue
            vals = [str(row[i] if i<len(row) and row[i] is not None else '').strip() for i in idx]
            if not vals[0] or not vals[1]: raise ValueError('ID va name bo‘sh bo‘lmasin')
            parsed.append(vals)
            if len(parsed)>10000: raise ValueError('Bir yuklashda ko‘pi bilan 10000 xodim')
        if len({r[0] for r in parsed})!=len(parsed): raise ValueError('Takroriy ID')
    except Exception as exc:
        raise HTTPException(400,f'Excel xatosi: {exc}')
    existing = {e.external_id:e for e in (await db.scalars(select(Employee).where(Employee.organization_id==org_id))).all()}
    created=updated=0
    for ext,name,region,position in parsed:
        if ext in existing:
            e=existing[ext]
            e.name,e.region,e.position,e.active=name,region,position,True
            updated+=1
        else:
            db.add(Employee(organization_id=org_id,external_id=ext,name=name,region=region,position=position))
            created+=1
    await db.commit()
    return {'created':created,'updated':updated,'unchanged_other_employees':True}

@app.get('/api/organizations/{org_id}/employees', dependencies=[Depends(auth)])
async def employees(org_id:int, db:AsyncSession=Depends(session)):
    rows=(await db.scalars(select(Employee).where(Employee.organization_id==org_id).order_by(Employee.id).limit(10000))).all()
    return [{'id':e.id,'external_id':e.external_id,'name':e.name,'region':e.region,'position':e.position} for e in rows]

@app.get('/api/surveys/{survey_id}/results', dependencies=[Depends(auth)])
async def results(survey_id:int, db:AsyncSession=Depends(session)):
    q=(select(Candidate.id,Employee.name,Employee.region,func.count(Vote.id).label('votes'))
       .join(Employee,Employee.id==Candidate.employee_id)
       .outerjoin(Vote,Vote.candidate_id==Candidate.id)
       .where(Candidate.survey_id==survey_id)
       .group_by(Candidate.id,Employee.name,Employee.region)
       .order_by(func.count(Vote.id).desc()))
    rows=(await db.execute(q)).all()
    return [{'candidate_id':r.id,'name':r.name,'region':r.region,'votes':r.votes} for r in rows]

@app.get('/api/surveys/{survey_id}/results.xlsx', dependencies=[Depends(auth)])
async def export_results(survey_id:int, db:AsyncSession=Depends(session)):
    data=await results(survey_id,db)
    wb=Workbook();ws=wb.active;ws.title='Natijalar'
    ws.append(['Nomzod ID','F.I.Sh.','Hudud','Ovozlar'])
    for r in data: ws.append([r['candidate_id'],r['name'],r['region'],r['votes']])
    output=BytesIO();wb.save(output);output.seek(0)
    return StreamingResponse(output,media_type='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',headers={'Content-Disposition':f'attachment; filename="survey_{survey_id}.xlsx"'})
