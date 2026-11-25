import copy,os,json,datetime as dt, secrets, hmac, threading
from starlette.concurrency import run_in_threadpool
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
generation_lock=threading.Lock()
NAMES=[f'Operador {i:02d}' for i in range(1,13)]
DEFAULT={'operators':[{'id':i+1,'name':n,'group':i//4,'active':True} for i,n in enumerate(NAMES)],'config':{'coverage':2,'rest':2,'gap':12,'maxrun':6,'alerts':15,'shifts':[['05:30','14:00'],['13:30','22:00'],['21:30','06:00']]},'novelties':[],'draft':None,'versions':[]}
with engine.begin() as c:
 c.execute(text('CREATE TABLE IF NOT EXISTS state (id INTEGER PRIMARY KEY, payload TEXT NOT NULL)'))
 c.execute(text('INSERT INTO state (id,payload) VALUES (1,:p) ON CONFLICT (id) DO NOTHING'),{'p':json.dumps(DEFAULT)})
def load(c):return json.loads(c.execute(text('SELECT payload FROM state WHERE id=1')).scalar_one())
def admin(r):
 if r.session.get('role')!='admin':raise HTTPException(403,'Acceso administrativo requerido')
def today():return dt.datetime.now(dt.timezone(dt.timedelta(hours=-5))).date()
def date(s):return dt.date.fromisoformat(s)
def days(a,b):return [a+dt.timedelta(days=i) for i in range((b-a).days+1)]
def base(d,g):return ['T1','T3','T2'][((d-date('2026-11-23')).days//7+[0,2,1][g%3])%3]
def mins(s):h,m=map(int,s.split(':'));return h*60+m
def gap(c,a,b):
 x,y=c['shifts'][int(a[1])-1];z=c['shifts'][int(b[1])-1][0];return 1440+mins(z)-mins(y)-(1440 if mins(y)<=mins(x) else 0)
def published(s):
 out={}
 for v in s['versions']:
  for row in out.values():
   for day in list(row):
    if v['start']<=day<=v['end']:row.pop(day)
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
  k=str(op['id']);
  for d in ds:
   v=rows.get(k,{}).get(str(d)); active=op_at(op,d)['active']
   if active and not v:errors.append(f'{op["name"]} {d}: falta asignación')
   if not active and v:errors.append(f'{op["name"]} {d}: operador no disponible')
  r={**{day:v for day,v in old.get(k,{}).items() if not p['start']<=day<=p['end']},**rows.get(k,{})};run=0;prev=None;week={}
  for d in days(ds[0]-dt.timedelta(days=7),ds[-1]+dt.timedelta(days=7)):
   v=r.get(str(d)); work=v in ['T1','T2','T3'];run=run+1 if work else 0
   if work and run>c['maxrun'] and d>=ds[0]:errors.append(f'{op["name"]} {d}: exceso de días consecutivos')
   if work and prev and gap(c,prev[1],v)+1440*((d-prev[0]).days-1)<c['gap']*60 and d>=ds[0] and prev[0]<=ds[-1]:errors.append(f'{op["name"]} {d}: descanso insuficiente')
   if work:prev=(d,v)
   week.setdefault(d-dt.timedelta(days=d.weekday()),[]).append(v)
  for w,vs in week.items():
   if len(vs)==7 and None not in vs and w<=ds[-1] and w+dt.timedelta(days=6)>=ds[0] and vs.count('DESC')!=min(c['rest'],sum(v in ('T1','T2','T3','DESC') for v in vs)):errors.append(f'{op["name"]} semana {w}: descansos incorrectos')
  for n in s['novelties']:
   if str(n['operator'])==k:
    for d in ds:
     if op_at(op,d)['active'] and n['start']<=str(d)<=n['end'] and rows.get(k,{}).get(str(d))!=n['type']:errors.append(f'{op["name"]} {d}: novedad no respetada')
 return errors

def op_at(o,d):
 state={'active':o.get('initial_active',o['active']),'group':o.get('initial_group',o['group'])}
 if str(d)<o.get('entry','0001-01-01'):state['active']=False
 for e in sorted(o.get('events',[]),key=lambda e:e['date']):
  if e['date']<=str(d):state.update(e['changes'])
 return state

def operator_action(s,p):
 today=dt.datetime.now(dt.timezone(dt.timedelta(hours=-5))).date(); kind=p.get('kind'); ops=s['operators']
 def fail(msg):raise HTTPException(400,msg)
 if kind in ('new','edit'):
  name=str(p.get('name','')).strip();code=str(p.get('code','')).strip()
  if not name or len(name)>150 or len(code)>80:fail('Nombre o código inválido')
  if any(o['id']!=p.get('id') and (o['name'].casefold()==name.casefold() or (code and o.get('code','').casefold()==code.casefold())) for o in ops):fail('Nombre o código duplicado')
 if kind=='new':
  try:entry=date(p['date']);g=int(p['group'])
  except (ValueError,KeyError,TypeError):fail('Fecha o grupo inválido')
  if g not in range(3) or entry<today:fail('Grupo inválido o fecha histórica')
  ops.append({'id':max([o['id'] for o in ops]+[0])+1,'name':name,'code':code,'group':g,'active':True,'entry':str(entry),'events':[]})
  return
 o=next((o for o in ops if o['id']==p.get('id')),None)
 if not o:fail('Operador inexistente')
 if kind=='edit':
  o.setdefault('events',[]).append({'date':str(today),'kind':'edit','changes':{},'before':{'name':o['name'],'code':o.get('code','')},'after':{'name':name,'code':code}});o.update(name=name,code=code);return
 try:d=date(p['date'])
 except (ValueError,KeyError,TypeError):fail('Fecha inválida')
 if d<today or str(d)<o.get('entry','0001-01-01'):fail('Fecha histórica o anterior al ingreso')
 if kind=='transfer':
  g=p.get('group')
  if g not in range(3):fail('Grupo inválido')
  changes={'group':g}
 elif kind in ('inactive','active'):changes={'active':kind=='active'}
 else:fail('Acción inválida')
 if any(v['end']>=str(d) and str(o['id']) in v['rows'] for v in s['versions']):fail('Existe programación publicada afectada; no se puede aplicar este movimiento todavía.')
 o.setdefault('initial_group',o['group']);o.setdefault('initial_active',o['active']);o.setdefault('events',[]).append({'date':str(d),'changes':changes,'kind':kind,'before':op_at(o,d),'recorded':dt.datetime.now(dt.timezone.utc).isoformat()})

def generate(s,a,b):
 if b<a or (b-a).days>370:raise HTTPException(400,'Periodo inválido (máximo 371 días)')
 c=s['config']; start=a-dt.timedelta(days=a.weekday());end=b+dt.timedelta(days=6-b.weekday());ds=days(start,end);ops=[o for o in s['operators'] if any(op_at(o,d)['active'] for d in ds)];m=cp_model.CpModel();ts=['T1','T2','T3','DESC','NOV'];x={};nov={};old=published(s); penalties=[]
 for i,o in enumerate(ops):
  for j,d in enumerate(ds):
   for t in ts:x[i,j,t]=m.NewBoolVar(f'{i}_{j}_{t}')
   m.AddExactlyOne(x[i,j,t] for t in ts)
   n=next((n['type'] for n in s['novelties'] if n['operator']==o['id'] and n['start']<=str(d)<=n['end']),None);n=n if op_at(o,d)['active'] else 'INACTIVE';nov[i,j]=n;m.Add(x[i,j,'NOV']==int(bool(n)))
   if not a<=d<=b and str(d) in old.get(str(o['id']),{}):
    v=old[str(o['id'])][str(d)];m.Add(x[i,j,v if v in ts else 'NOV']==1)
   for t in ts[:3]:
    if t!=base(d,op_at(o,d)['group']):penalties.append(10*x[i,j,t])
   if j:
    for t in ts[:3]:
     for u in ts[:3]:
      if gap(c,t,u)<c['gap']*60:m.Add(x[i,j-1,t]+x[i,j,u]<=1)
    q=m.NewBoolVar(f'pair{i}_{j}');m.Add(q<=x[i,j-1,'DESC']);m.Add(q<=x[i,j,'DESC']);m.Add(q>=x[i,j-1,'DESC']+x[i,j,'DESC']-1);penalties.append(-q)
   if j>=c['maxrun']:m.Add(sum(x[i,k,t] for k in range(j-c['maxrun'],j+1) for t in ts[:3])<=c['maxrun'])
  for j in range(0,len(ds),7):m.Add(sum(x[i,k,'DESC'] for k in range(j,j+7))==min(c['rest'],sum(not nov[i,k] for k in range(j,j+7))))
 for j,d in enumerate(ds):
  for t in ts[:3]:
   m.Add(sum(x[i,j,t] for i in range(len(ops)))>=c['coverage'])
 # Equidad acumulada: minimizar diferencias de descansos por categoría.
 for category in ('sat','sun','holiday'):
  counts=[]
  for i,o in enumerate(ops):
   def match(d):return d.weekday()==5 if category=='sat' else d.weekday()==6 if category=='sun' else d in holidays.Colombia(years=d.year)
   prior=sum(v=='DESC' and date(k)<a and date(k).year==a.year and match(date(k)) for k,v in old.get(str(o['id']),{}).items())
   count=m.NewIntVar(0,800,f'eq_{category}_{i}');m.Add(count==prior+sum(x[i,j,'DESC'] for j,d in enumerate(ds) if a<=d<=b and match(d)));counts.append(count)
  if counts:
   hi=m.NewIntVar(0,800,'hi_'+category);lo=m.NewIntVar(0,800,'lo_'+category);m.AddMaxEquality(hi,counts);m.AddMinEquality(lo,counts);penalties.append(8*(hi-lo))
 for j,d in enumerate(ds):
  if a<=d<=b:
   counts=[sum(x[i,j,t] for i in range(len(ops))) for t in ts[:3]]
   hi=m.NewIntVar(0,len(ops),f'hi{j}');lo=m.NewIntVar(0,len(ops),f'lo{j}');m.AddMaxEquality(hi,counts);m.AddMinEquality(lo,counts);penalties.append(hi-lo)
 m.Minimize(sum(penalties));solver=cp_model.CpSolver();solver.parameters.num_search_workers=1;solver.parameters.max_deterministic_time=12;solver.parameters.max_time_in_seconds=30;solver.parameters.random_seed=0;status=solver.Solve(m)
 if status==cp_model.UNKNOWN:raise HTTPException(503,'Tiempo de búsqueda agotado sin solución. Intente un periodo más corto.')
 if status==cp_model.INFEASIBLE:raise HTTPException(409,'Programación inviable con las reglas actuales. Revise novedades, disponibilidad y cobertura; considere incorporar operadores o ajustar descansos sin vulnerar las reglas obligatorias.')
 if status not in (cp_model.OPTIMAL,cp_model.FEASIBLE):raise HTTPException(500,'El optimizador no pudo procesar el modelo.')
 return {'start':str(a),'end':str(b),'rows':{str(o['id']):{str(d):next((nov[i,j] if t=='NOV' else t for t in ts if solver.Value(x[i,j,t])), 'DESC') for j,d in enumerate(ds) if a<=d<=b and op_at(o,d)['active']} for i,o in enumerate(ops)}}
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
 
 for o in s['operators']:
  o.update(op_at(o,dt.datetime.now(dt.timezone(dt.timedelta(hours=-5))).date()))
  if r.session['role']!='admin':
   for k in ('events','code','initial_active','initial_group'):o.pop(k,None)
 s['role']=r.session['role'];return s
def generate_and_save(p):
 if not generation_lock.acquire(blocking=False):raise HTTPException(409,'Ya hay una generación en curso')
 try:
  with engine.connect() as c:s=load(c)
  snapshot=json.dumps(s,sort_keys=True)
  draft=generate(s,date(p['start']),date(p['end']))
  errors=validate(s,draft)
  if errors:raise HTTPException(409,'El borrador no supera la validación: '+ '; '.join(errors[:10])+'. Revise los límites del periodo y la programación publicada.')
  with engine.begin() as c:
   if engine.dialect.name=='postgresql':c.execute(text('SELECT id FROM state WHERE id=1 FOR UPDATE'))
   current=load(c)
   if json.dumps(current,sort_keys=True)!=snapshot:raise HTTPException(409,'Los datos cambiaron durante la generación. Intente nuevamente.')
   current['draft']=draft
   c.execute(text('UPDATE state SET payload=:p WHERE id=1'),{'p':json.dumps(current)})
  return {'ok':True}
 finally:generation_lock.release()
@app.post('/action/{action}')
async def action(action:str,r:Request):
 admin(r);p=await r.json()
 if action=='generate':return await run_in_threadpool(generate_and_save,p)
 with engine.begin() as c:
  if engine.dialect.name=='postgresql':c.execute(text('SELECT id FROM state WHERE id=1 FOR UPDATE'))
  s=load(c)
  if action=='edit':
   d=s['draft'];k=str(p['operator']);day=p['date'];v=p['value'];
   if not d or k not in d['rows'] or day not in d['rows'][k] or v not in ['T1','T2','T3','DESC','VAC','INC','PER','LIC','CAP','AUS']:raise HTTPException(400,'Celda inválida')
   if date(day)<today():raise HTTPException(400,'Fecha histórica bloqueada')
   d['rows'][k][day]=v
  elif action=='publish':
   d=s['draft'];
   if not d:raise HTTPException(400,'Sin borrador')
   if date(d['start'])<today():raise HTTPException(400,'Periodo histórico bloqueado')
   errors=validate(s,d)
   if errors:raise HTTPException(409,errors)
   s['versions'].append({**d,'published_at':dt.datetime.now(dt.timezone.utc).isoformat(),'reason':p.get('reason',''),'config':copy.deepcopy(s['config']),'operators':copy.deepcopy(s['operators'])});s['draft']=None
  elif action=='delete_draft':s['draft']=None
  elif action=='operator':operator_action(s,p)
  elif action=='novelties':update_novelties(s,p['items'])
  elif action=='swap':swap(s,p)
  elif action=='replacement':apply_replacement(s,p)
  elif action=='config':
   validate_config(p);s.setdefault('config_history',[]).append({'date':str(today()),'config':copy.deepcopy(s['config'])});s['config']=p
  else:raise HTTPException(404)
  c.execute(text('UPDATE state SET payload=:p WHERE id=1'),{'p':json.dumps(s)})
 return {'ok':True}


def update_novelties(s,items):
 if not isinstance(items,list):raise HTTPException(400,'Lista inválida')
 old=s['novelties']; historical=lambda n:date(n['start'])<today()
 if [n for n in old if historical(n)]!=[n for n in items if historical(n)]:raise HTTPException(400,'Novedades históricas protegidas')
 for i,n in enumerate(items):
  if n['type'] not in ('VAC','INC','PER','LIC','CAP','AUS') or date(n['start'])>date(n['end']):raise HTTPException(400,'Novedad o fechas inválidas')
  o=next((o for o in s['operators'] if o['id']==n['operator']),None)
  if not o:raise HTTPException(400,'Operador inexistente')
  if n not in old and not all(op_at(o,d)['active'] for d in days(date(n['start']),date(n['end']))):raise HTTPException(400,'Operador no disponible en el periodo')
  for other in items[:i]:
   if other['operator']==n['operator'] and other['start']<=n['end'] and n['start']<=other['end']:raise HTTPException(409,f"Conflicto con {other['type']} del {other['start']} al {other['end']}")
 d=s['draft']
 if not d:
  affected=[n for n in old+items if n not in old or n not in items]
  pub=published(s)
  dates_affected=[day for n in affected for day in pub.get(str(n['operator']),{}) if n['start']<=day<=n['end'] and day>=str(today())]
  if dates_affected:
   a,b=min(dates_affected),max(dates_affected)
   a=max(str(today()),str(date(a)-dt.timedelta(days=date(a).weekday())))
   b=str(date(b)+dt.timedelta(days=6-date(b).weekday()))
   known=sorted({day for row in pub.values() for day in row if a<=day<=b})
   if known:
    a,b=known[0],known[-1]
    d=s['draft']={'start':a,'end':b,'rows':{k:{day:v for day,v in row.items() if a<=day<=b} for k,row in pub.items()}}
 if d:
  backup=d.setdefault('before_novelties',{})
  for k,row in d['rows'].items():
   for day,v in list(row.items()):
    n=next((n for n in items if str(n['operator'])==k and n['start']<=day<=n['end']),None)
    key=k+'|'+day
    if n:
     if v in ('T1','T2','T3','DESC'):backup[key]=v
     row[day]=n['type']
    elif key in backup:row[day]=backup.pop(key)
 s['novelties']=items

def swap(s,p):
 d=s['draft']
 if not d:raise HTTPException(400,'Genere un borrador primero')
 a,b=str(p['a']),str(p['b']);x,y=p['x'],p['y']
 if min(date(x),date(y))<today():raise HTTPException(400,'Fecha histórica bloqueada')
 try:u,v=d['rows'][a][x],d['rows'][b][y]
 except KeyError:raise HTTPException(400,'Asignación inexistente')
 if u not in ('T1','T2','T3','DESC') or v not in ('T1','T2','T3','DESC'):raise HTTPException(400,'No se intercambian novedades')
 d['rows'][a][x],d['rows'][b][y]=v,u
 errors=validate(s,d)
 if errors:raise HTTPException(409,errors[:20])
 s.setdefault('swaps',[]).append({'a':a,'b':b,'x':x,'y':y,'before':[u,v],'after':[v,u],'reason':str(p.get('reason','')),'recorded':dt.datetime.now(dt.timezone.utc).isoformat()})

def validate_config(p):
 for k,lo,hi in [('coverage',1,100),('rest',0,6),('gap',0,48),('maxrun',1,14),('alerts',1,365)]:
  if type(p.get(k)) is not int or not lo<=p[k]<=hi:raise HTTPException(400,'Parámetro inválido: '+k)
 try:
  assert len(p['shifts'])==3
  for a,b in p['shifts']:
   dt.time.fromisoformat(a);dt.time.fromisoformat(b);assert a!=b
 except (ValueError,TypeError,KeyError,AssertionError):raise HTTPException(400,'Horarios inválidos')


WORK=('T1','T2','T3')
def replacement_options(s):
 d=s.get('draft')
 if not d:return []
 rows=d['rows']; c=s['config']; baseline=set(validate(s,d)); out=[]
 for day in days(max(today(),date(d['start'])),date(d['end'])):
  k=str(day)
  for t in WORK:
   count=sum(r.get(k)==t for r in rows.values())
   if count>=c['coverage']:continue
   candidates=[]
   absent_groups={op_at(o,day)['group'] for o in s['operators'] if d.get('before_novelties',{}).get(str(o['id'])+'|'+k)==t}
   for o in s['operators']:
    oid=str(o['id']); row=rows.get(oid,{}); prev=row.get(k)
    if not op_at(o,day)['active'] or prev not in (*WORK,'DESC') or prev==t:continue
    plans=[]
    if prev in WORK and sum(r.get(k)==prev for r in rows.values())>c['coverage']:
     plans.append([(k,prev,t)])
    if prev=='DESC':
     monday=day-dt.timedelta(days=day.weekday())
     for other in days(monday,monday+dt.timedelta(days=6)):
      q=str(other); v=row.get(q)
      if other>=today() and v in WORK and sum(r.get(q)==v for r in rows.values())>c['coverage']:
       plans.append([(k,prev,t),(q,v,'DESC')])
    for plan in plans:
     for q,old,new in plan:row[q]=new
     try: errors=set(validate(s,d))
     finally:
      for q,old,new in plan:row[q]=old
     if errors-baseline:continue
     candidates.append({'operator':o['id'],'name':o['name'],'group':op_at(o,day)['group']+1,'same':op_at(o,day)['group'] in absent_groups,'changes':plan})
   candidates.sort(key=lambda x:(not x['same'],len(x['changes']),x['operator']))
   out.append({'date':k,'shift':t,'count':count,'required':c['coverage'],'options':candidates[:12]})
 return out

@app.get('/replacements')
def replacements(r:Request):
 admin(r)
 with engine.connect() as c:s=load(c)
 return replacement_options(s)

def apply_replacement(s,p):
 for issue in replacement_options(s):
  if issue['date']==p.get('date') and issue['shift']==p.get('shift'):
   for option in issue['options']:
    if option['operator']==p.get('operator') and option['changes']==[tuple(x) for x in p.get('changes',[])]:
     row=s['draft']['rows'][str(option['operator'])]
     for day,old,new in option['changes']:row[day]=new
     s['draft'].setdefault('replacements',[]).append({**option,'recorded':dt.datetime.now(dt.timezone.utc).isoformat()})
     return
 raise HTTPException(409,'La sugerencia ya no es válida. Consulte nuevamente.')
