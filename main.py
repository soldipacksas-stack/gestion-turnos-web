import os,json,datetime as dt, secrets, hmac
from fastapi import FastAPI,Request,HTTPException
from fastapi.responses import HTMLResponse
from starlette.middleware.sessions import SessionMiddleware
from sqlalchemy import create_engine,text
from ortools.sat.python import cp_model
import holidays
from pathlib import Path
app=FastAPI(); app.add_middleware(SessionMiddleware,secret_key=os.environ.get('SESSION_SECRET',secrets.token_hex(32)),https_only=bool(os.getenv('RENDER')))
url=os.getenv('DATABASE_URL','sqlite:///turnos.db').replace('postgres://','postgresql+psycopg://',1).replace('postgresql://','postgresql+psycopg://',1)
engine=create_engine(url)
NAMES=[f'Operador {i:02d}' for i in range(1,13)]
DEFAULT={'operators':[{'id':i+1,'name':n,'group':i//4,'active':True} for i,n in enumerate(NAMES)],'config':{'coverage':2,'rest':2,'gap':12,'maxrun':6,'alerts':15,'shifts':[['05:30','14:00'],['13:30','22:00'],['21:30','06:00']]},'novelties':[],'draft':None,'versions':[]}
with engine.begin() as c:
 c.execute(text('CREATE TABLE IF NOT EXISTS state (id INTEGER PRIMARY KEY, payload TEXT NOT NULL)'))
 c.execute(text('INSERT INTO state (id,payload) VALUES (1,:p) ON CONFLICT (id) DO NOTHING'),{'p':json.dumps(DEFAULT)})
def load(c):return json.loads(c.execute(text('SELECT payload FROM state WHERE id=1')).scalar_one())
def admin(r):
 if r.session.get('role')!='admin':raise HTTPException(403,'Acceso administrativo requerido')
def date(s):return dt.date.fromisoformat(s)
def days(a,b):return [a+dt.timedelta(days=i) for i in range((b-a).days+1)]
def base(d,g):return ['T1','T3','T2'][((d-date('2026-11-23')).days//7+[0,2,1][g%3])%3]
def mins(s):h,m=map(int,s.split(':'));return h*60+m
def gap(c,a,b):
 x,y=c['shifts'][int(a[1])-1];z=c['shifts'][int(b[1])-1][0];return 1440+mins(z)-mins(y)-(1440 if mins(y)<=mins(x) else 0)
def published(s):
 out={}
 for v in s['versions']:
  for k,row in v['rows'].items():out.setdefault(k,{}).update(row)
 return out
def validate(s,p):
 c=s['config']; ds=days(date(p['start']),date(p['end'])); errors=[]; rows=p['rows']; old=published(s)
 for d in ds:
  k=str(d)
  for t in ['T1','T2','T3']:
   n=sum(r.get(k)==t for r in rows.values())
   if n<c['coverage']:errors.append(f'{k} {t}: cobertura {n}/{c["coverage"]}')
 for op in s['operators']:
  k=str(op['id']);r={**old.get(k,{}),**rows.get(k,{})};run=0;prev=None;week={}
  for d in days(ds[0]-dt.timedelta(days=7),ds[-1]+dt.timedelta(days=7)):
   v=r.get(str(d)); work=v in ['T1','T2','T3'];run=run+1 if work else 0
   if work and run>c['maxrun'] and d>=ds[0]:errors.append(f'{op["name"]} {d}: exceso de días consecutivos')
   if work and prev and gap(c,prev,v)<c['gap']*60 and d>=ds[0]:errors.append(f'{op["name"]} {d}: descanso insuficiente')
   prev=v if work else None
   week.setdefault(d-dt.timedelta(days=d.weekday()),[]).append(v)
  for w,vs in week.items():
   if len(vs)==7 and None not in vs and w<=ds[-1] and w+dt.timedelta(days=6)>=ds[0] and vs.count('DESC')!=c['rest'] and all(v in ['T1','T2','T3','DESC'] for v in vs):errors.append(f'{op["name"]} semana {w}: descansos incorrectos')
  for n in s['novelties']:
   if str(n['operator'])==k:
    for d in ds:
     if n['start']<=str(d)<=n['end'] and rows.get(k,{}).get(str(d))!=n['type']:errors.append(f'{op["name"]} {d}: novedad no respetada')
 return errors

def generate(s,a,b):
 if b<a or (b-a).days>370:raise HTTPException(400,'Periodo inválido (máximo 371 días)')
 c=s['config']; start=a-dt.timedelta(days=a.weekday());end=b+dt.timedelta(days=6-b.weekday());ds=days(start,end);ops=[o for o in s['operators'] if o['active']];m=cp_model.CpModel();ts=['T1','T2','T3','DESC','NOV'];x={};nov={};old=published(s); penalties=[]
 for i,o in enumerate(ops):
  for j,d in enumerate(ds):
   for t in ts:x[i,j,t]=m.NewBoolVar(f'{i}_{j}_{t}')
   m.AddExactlyOne(x[i,j,t] for t in ts)
   n=next((n['type'] for n in s['novelties'] if n['operator']==o['id'] and n['start']<=str(d)<=n['end']),None);nov[i,j]=n;m.Add(x[i,j,'NOV']==int(bool(n)))
   if not a<=d<=b and str(d) in old.get(str(o['id']),{}):
    v=old[str(o['id'])][str(d)];m.Add(x[i,j,v if v in ts else 'NOV']==1)
   for t in ts[:3]:
    if t!=base(d,o['group']):penalties.append(10*x[i,j,t])
   if j:
    for t in ts[:3]:
     for u in ts[:3]:
      if gap(c,t,u)<c['gap']*60:m.Add(x[i,j-1,t]+x[i,j,u]<=1)
    q=m.NewBoolVar(f'pair{i}_{j}');m.Add(q<=x[i,j-1,'DESC']);m.Add(q<=x[i,j,'DESC']);m.Add(q>=x[i,j-1,'DESC']+x[i,j,'DESC']-1);penalties.append(-q)
   if j>=c['maxrun']:m.Add(sum(x[i,k,t] for k in range(j-c['maxrun'],j+1) for t in ts[:3])<=c['maxrun'])
  for j in range(0,len(ds),7):m.Add(sum(x[i,k,'DESC'] for k in range(j,j+7))==min(c['rest'],sum(not nov[i,k] for k in range(j,j+7))))
 for j,d in enumerate(ds):
  for t in ts[:3]:
   shortage=m.NewIntVar(0,c['coverage'],f'short{j}{t}');m.Add(sum(x[i,j,t] for i in range(len(ops)))+shortage>=c['coverage']);penalties.append(10000*shortage)
 m.Minimize(sum(penalties));solver=cp_model.CpSolver();solver.parameters.num_search_workers=1;solver.parameters.max_deterministic_time=12;solver.parameters.random_seed=0;status=solver.Solve(m)
 if status not in (cp_model.OPTIMAL,cp_model.FEASIBLE):raise HTTPException(409,'No se encontró borrador: revise descansos, novedades y disponibilidad')
 return {'start':str(a),'end':str(b),'rows':{str(o['id']):{str(d):next((nov[i,j] if t=='NOV' else t for t in ts if solver.Value(x[i,j,t])), 'DESC') for j,d in enumerate(ds) if a<=d<=b} for i,o in enumerate(ops)}}
@app.get('/')
def home():return HTMLResponse(Path(__file__).with_name('index.html').read_text())
@app.post('/login')
async def login(r:Request):
 p=await r.json();role=p.get('role');key=os.getenv('ADMIN_PASSWORD' if role=='admin' else 'OPERATOR_PASSWORD','');
 if role not in ('admin','operator') or not key or not hmac.compare_digest(key,str(p.get('password',''))):raise HTTPException(401,'Clave incorrecta o no configurada')
 r.session['role']=role;return {'role':role}
@app.post('/logout')
def logout(r:Request):r.session.clear();return {}
@app.get('/data')
def data(r:Request):
 if not r.session.get('role'):raise HTTPException(401)
 with engine.connect() as c:s=load(c)
 if r.session['role']!='admin':s={'operators':s['operators'],'config':s['config'],'published':published(s)}
 else:s['published']=published(s);s['errors']=validate(s,s['draft']) if s['draft'] else []
 s['role']=r.session['role'];return s
@app.post('/action/{action}')
async def action(action:str,r:Request):
 admin(r);p=await r.json()
 with engine.begin() as c:
  if engine.dialect.name=='postgresql':c.execute(text('SELECT id FROM state WHERE id=1 FOR UPDATE'))
  s=load(c)
  if action=='generate':s['draft']=generate(s,date(p['start']),date(p['end']))
  elif action=='edit':
   d=s['draft'];k=str(p['operator']);day=p['date'];v=p['value'];
   if not d or k not in d['rows'] or day not in d['rows'][k] or v not in ['T1','T2','T3','DESC','VAC','INC','PER','LIC','CAP','AUS']:raise HTTPException(400,'Celda inválida')
   d['rows'][k][day]=v
  elif action=='publish':
   d=s['draft'];
   if not d:raise HTTPException(400,'Sin borrador')
   if date(d['start'])<dt.date.today():raise HTTPException(400,'Periodo histórico bloqueado')
   errors=validate(s,d)
   if errors:raise HTTPException(409,errors)
   s['versions'].append({**d,'published_at':dt.datetime.now(dt.timezone.utc).isoformat(),'reason':p.get('reason','')});s['draft']=None
  elif action=='delete_draft':s['draft']=None
  elif action=='operators':s['operators']=p['items']
  elif action=='novelties':s['novelties']=p['items']
  elif action=='config':s['config']=p
  else:raise HTTPException(404)
  c.execute(text('UPDATE state SET payload=:p WHERE id=1'),{'p':json.dumps(s)})
 return {'ok':True}
