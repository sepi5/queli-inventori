import os, sqlite3, secrets, json, hashlib, re
from datetime import datetime, timezone
from zoneinfo import ZoneInfo
from pathlib import Path
from functools import wraps
from threading import Thread
from concurrent.futures import ThreadPoolExecutor
from flask import Flask, request, redirect, url_for, session, render_template, flash, abort, Response, jsonify, send_file
from werkzeug.security import generate_password_hash, check_password_hash
D=Path(__file__).resolve().parent
DB=os.environ.get('SHOESHOP_DB',str(D/'users.db'))
app=Flask(__name__); app.secret_key=os.environ.get('SECRET_KEY') or secrets.token_hex(32)
app.config.update(SESSION_COOKIE_HTTPONLY=True,SESSION_COOKIE_SAMESITE='Lax',SESSION_COOKIE_SECURE=os.environ.get('COOKIE_SECURE','0')=='1',PERMANENT_SESSION_LIFETIME=43200)
def db():
 c=sqlite3.connect(DB, timeout=30); c.row_factory=sqlite3.Row; c.execute('PRAGMA busy_timeout=30000'); return c

def jalali_to_gregorian(jy,jm,jd):
 # Integer-calendar conversion; accepts Persian (Jalali) date parts.
 jy += 1595
 days = -355668 + 365*jy + (jy//33)*8 + ((jy%33+3)//4) + jd
 if jm < 7: days += (jm-1)*31
 else: days += (jm-7)*30+186
 gy = 400*(days//146097)
 days %= 146097
 if days > 36524:
  gy += 100*((days-1)//36524)
  days = (days-1)%36524
  if days >= 365: days += 1
 gy += 4*(days//1461)
 days %= 1461
 if days > 365:
  gy += (days-1)//365
  days = (days-1)%365
 gd = days+1
 leap = gy%4==0 and (gy%100!=0 or gy%400==0)
 month_days=[0,31,29 if leap else 28,31,30,31,30,31,31,30,31,30,31]
 gm=1
 while gm < 12 and gd > month_days[gm]:
  gd -= month_days[gm]; gm += 1
 return datetime(gy,gm,gd).date()
def init():
 with db() as c:
  c.execute("CREATE TABLE IF NOT EXISTS users(id INTEGER PRIMARY KEY,username TEXT UNIQUE NOT NULL,display_name TEXT NOT NULL,password_hash TEXT NOT NULL,role TEXT NOT NULL CHECK(role IN ('admin','seller')),created_at TEXT DEFAULT CURRENT_TIMESTAMP)")
  c.execute("CREATE TABLE IF NOT EXISTS app_state(key TEXT PRIMARY KEY, value TEXT NOT NULL, updated_at TEXT DEFAULT CURRENT_TIMESTAMP)")
  c.execute("CREATE TABLE IF NOT EXISTS push_subscriptions(id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER NOT NULL, endpoint TEXT UNIQUE NOT NULL, subscription_json TEXT NOT NULL, created_at TEXT DEFAULT CURRENT_TIMESTAMP, FOREIGN KEY(user_id) REFERENCES users(id))")
  # One-time replacement of the full product inventory from 3 Aban.
  # This replaces only app_state.products; users, requests, sales, and outgoing history stay intact.
  stock_marker='stock_replace_3aban_2026_v1'
  done=c.execute('SELECT 1 FROM app_state WHERE key=?',(stock_marker,)).fetchone()
  stock_file=D/'stock_update_3aban.json'
  if not done and stock_file.exists():
   try:
    stock_data=json.loads(stock_file.read_text(encoding='utf-8'))
    if isinstance(stock_data,list) and stock_data:
     c.execute("INSERT INTO app_state(key,value,updated_at) VALUES('products',?,CURRENT_TIMESTAMP) ON CONFLICT(key) DO UPDATE SET value=excluded.value,updated_at=CURRENT_TIMESTAMP",(json.dumps(stock_data,ensure_ascii=False,separators=(',',':')),))
     c.execute("INSERT INTO app_state(key,value,updated_at) VALUES(?,?,CURRENT_TIMESTAMP)",(stock_marker,json.dumps({'count':len(stock_data),'source':'موجودی ۳ آبان'},ensure_ascii=False)))
     print(f'3 Aban inventory replaced: {len(stock_data)} product rows')
   except Exception as e:
    print('3 Aban inventory import failed:',repr(e))
  c.execute("CREATE TABLE IF NOT EXISTS seller_requests(id INTEGER PRIMARY KEY AUTOINCREMENT, seller_id INTEGER NOT NULL, barcode TEXT NOT NULL, model TEXT NOT NULL, color TEXT, size TEXT, qty INTEGER NOT NULL DEFAULT 1, status TEXT NOT NULL DEFAULT 'pending' CHECK(status IN ('pending','sold','kept')), created_at TEXT DEFAULT CURRENT_TIMESTAMP, resolved_at TEXT, FOREIGN KEY(seller_id) REFERENCES users(id))")
  # Safe migration for existing databases: add the seller's requested shoe side/type.
  request_columns={row['name'] for row in c.execute("PRAGMA table_info(seller_requests)").fetchall()}
  if 'request_type' not in request_columns:
   c.execute("ALTER TABLE seller_requests ADD COLUMN request_type TEXT NOT NULL DEFAULT ''")
  c.execute("CREATE TABLE IF NOT EXISTS sales(id INTEGER PRIMARY KEY AUTOINCREMENT, request_id INTEGER UNIQUE NOT NULL, seller_id INTEGER NOT NULL, barcode TEXT NOT NULL, model TEXT NOT NULL, color TEXT, size TEXT, qty INTEGER NOT NULL, sold_at TEXT DEFAULT CURRENT_TIMESTAMP, FOREIGN KEY(seller_id) REFERENCES users(id))")
  name=os.environ.get('ADMIN_USERNAME','admin'); pw=os.environ.get('ADMIN_PASSWORD')
  if not c.execute('SELECT id FROM users WHERE username=?',(name,)).fetchone():
   if not pw:
    pw=secrets.token_urlsafe(14); print('\nFIRST ADMIN PASSWORD (SAVE IT): '+pw+'\n')
   c.execute('INSERT INTO users(username,display_name,password_hash,role) VALUES(?,?,?,?)',(name,'مدیر',generate_password_hash(pw),'admin'))
def who():
 if not session.get('user_id'): return None
 with db() as c: return c.execute('SELECT id,username,display_name,role FROM users WHERE id=?',(session['user_id'],)).fetchone()
def login_req(f):
 @wraps(f)
 def w(*a,**k):
  if not who(): return redirect(url_for('login',next=request.path))
  return f(*a,**k)
 return w
def admin_req(f):
 @wraps(f)
 def w(*a,**k):
  u=who()
  if not u: return redirect(url_for('login'))
  if u['role']!='admin': abort(403)
  return f(*a,**k)
 return w
@app.after_request
def headers(r):
 r.headers['X-Content-Type-Options']='nosniff'; r.headers['X-Frame-Options']='SAMEORIGIN'; r.headers['Referrer-Policy']='strict-origin-when-cross-origin'; return r
def _vapid_public_key():
 try:
  return (D/'vapid_public.txt').read_text(encoding='utf-8').strip()
 except Exception:
  return ''

def _send_admin_push(title, body, url='/inventory'):
 # Push delivery is optional until VAPID keys and pywebpush are configured.
 private_key=D/'vapid_private.pem'
 public_key=_vapid_public_key()
 if not public_key or not private_key.exists(): return
 try:
  from pywebpush import webpush, WebPushException
 except Exception as e:
  print('Web push unavailable; install pywebpush:', repr(e)); return
 with db() as c:
  subs=[dict(r) for r in c.execute("SELECT id,endpoint,subscription_json FROM push_subscriptions WHERE user_id IN (SELECT id FROM users WHERE role='admin')").fetchall()]
 def deliver(sub):
  try:
   webpush(subscription_info=json.loads(sub['subscription_json']), data=json.dumps({'title':title,'body':body,'url':url},ensure_ascii=False), vapid_private_key=str(private_key), vapid_claims={'sub':os.environ.get('VAPID_SUBJECT','mailto:admin@queli.dearshop.ir')})
  except Exception as e:
   print('Admin web push failed:',repr(e))
   # Remove expired/invalid subscriptions without blocking other deliveries.
   if getattr(e,'response',None) is not None and getattr(e.response,'status_code',0) in (404,410):
    try:
     with db() as c: c.execute('DELETE FROM push_subscriptions WHERE id=?',(sub['id'],))
    except Exception as cleanup_error:
     print('Push subscription cleanup failed:',repr(cleanup_error))
 # Deliver to admin devices concurrently so one slow endpoint does not delay the rest.
 if subs:
  with ThreadPoolExecutor(max_workers=min(8,len(subs))) as pool:
   list(pool.map(deliver,subs))


@app.route('/manifest.json')
def pwa_manifest():
 return jsonify({
  "id":"/","name":"کوئلی شاپ","short_name":"کوئلی شاپ","description":"مدیریت موجودی و درخواست‌های کفش",
  "lang":"fa","dir":"rtl","start_url":"/","scope":"/","display":"standalone",
  "background_color":"#f3f5f9","theme_color":"#6d28d9",
  "icons":[
   {"src":"/static/queli-icon-192.png","sizes":"192x192","type":"image/png","purpose":"any"},
   {"src":"/static/queli-icon-512.png","sizes":"512x512","type":"image/png","purpose":"any maskable"}
  ]
 })

@app.route('/sw.js')
def service_worker():
 js = r'''self.addEventListener('push', function(event) {
  let data = {}; try { data = event.data ? event.data.json() : {}; } catch(e) {}
  const title = data.title || 'درخواست جدید کوئلی شاپ';
  const options = { body: data.body || 'یک درخواست جدید ثبت شد.', icon: '/favicon.ico', badge: '/favicon.ico', data: { url: data.url || '/inventory' }, tag: 'queli-new-request', renotify: true };
  event.waitUntil(self.registration.showNotification(title, options));
});
self.addEventListener('notificationclick', function(event) { event.notification.close(); const target = event.notification.data && event.notification.data.url || '/inventory'; event.waitUntil(clients.matchAll({type:'window', includeUncontrolled:true}).then(function(list){ for (const c of list) { if ('focus' in c) { c.navigate(target); return c.focus(); } } return clients.openWindow(target); })); });
self.addEventListener('fetch', function(event) {
 if (event.request.method !== 'GET' || event.request.mode !== 'navigate') return;
 event.respondWith(fetch(event.request).catch(function() {
  return new Response('<!doctype html><html lang="fa" dir="rtl"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>کوئلی شاپ</title><body style="font-family:Tahoma;text-align:center;padding:40px;line-height:2"><h2>اتصال اینترنت برقرار نیست</h2><p>برای استفاده از کوئلی شاپ، اتصال اینترنت را بررسی و دوباره تلاش کن.</p></body></html>', {headers:{'Content-Type':'text/html; charset=utf-8'}});
 }));
});
'''  
 return Response(js,mimetype='application/javascript',headers={'Service-Worker-Allowed':'/' ,'Cache-Control':'no-cache'})

@app.route('/api/push/public-key')
@login_req
def push_public_key():
 return jsonify(publicKey=_vapid_public_key())

@app.route('/api/push/subscribe', methods=['POST'])
@admin_req
def push_subscribe():
 u=who(); payload=request.get_json(silent=True) or {}; sub=payload.get('subscription')
 if not isinstance(sub,dict) or not sub.get('endpoint') or not isinstance(sub.get('keys'),dict):
  return jsonify(error='اطلاعات اشتراک اعلان معتبر نیست.'),400
 with db() as c:
  c.execute("INSERT INTO push_subscriptions(user_id,endpoint,subscription_json) VALUES(?,?,?) ON CONFLICT(endpoint) DO UPDATE SET user_id=excluded.user_id,subscription_json=excluded.subscription_json",(u['id'],sub['endpoint'],json.dumps(sub,ensure_ascii=False,separators=(',',':'))))
 return jsonify(ok=True)

@app.route('/',methods=['GET','POST'])
def login():
 if who(): return redirect('/inventory')
 err=None
 if request.method=='POST':
  un=request.form.get('username','').strip(); pw=request.form.get('password','')
  with db() as c: u=c.execute('SELECT * FROM users WHERE username=?',(un,)).fetchone()
  if u and check_password_hash(u['password_hash'],pw):
   session.clear(); session['user_id']=u['id']; session.permanent=True
   nxt=request.args.get('next','/inventory'); return redirect(nxt if nxt.startswith('/') and not nxt.startswith('//') else '/inventory')
  err='نام کاربری یا رمز عبور اشتباه است.'
 return render_template('login.html',error=err)
@app.route('/inventory')
@login_req
def inventory():
 u=who(); html=(D/'templates/inventory.html').read_text(encoding='utf-8')
 with db() as c:
  rows={r['key']:r['value'] for r in c.execute("SELECT key,value FROM app_state WHERE key IN ('products','outgoing')").fetchall()}
 state={}
 for key in ('products','outgoing'):
  try: state[key]=json.loads(rows[key]) if key in rows else None
  except Exception: state[key]=None
 state_json=json.dumps(state,ensure_ascii=False,separators=(',',':')).replace('<','\\u003c')
 role_script='window.__SHOE_ROLE__='+json.dumps(u['role'])+';window.__SHOE_NAME__='+json.dumps(u['display_name'],ensure_ascii=False)+';window.__SHOE_USERNAME__='+json.dumps(u['username'],ensure_ascii=False)+';window.__SHOE_SERVER_STATE__='+state_json+';'
 js=('<script>'+role_script+"(function(){const p='shoe_shop_user_%s_';const g=Storage.prototype.getItem,s=Storage.prototype.setItem,r=Storage.prototype.removeItem;Storage.prototype.getItem=function(k){return g.call(this,p+k)};Storage.prototype.setItem=function(k,v){return s.call(this,p+k,v)};Storage.prototype.removeItem=function(k){return r.call(this,p+k)};})();</script>" % u['id'])
 html=html.replace('</head>',js+'</head>',1)
 bar='<div class="accountbar">ورود: '+str(u['display_name'])+(' · مدیر' if u['role']=='admin' else ' · فروشنده')+' | <a href="/logout">خروج</a>'+(' | <a href="/admin">مدیریت فروشنده‌ها</a> | <a href="/database">مدیریت دیتابیس</a>' if u['role']=='admin' else '')+'</div>'
 import re
 def add_accountbar(m):
  tag=m.group(1)
  role_class='role-'+str(u['role'])
  cm=re.search(r'\bclass=(["\'])(.*?)\1',tag,re.I)
  if cm:
   classes=cm.group(2).split()
   if role_class not in classes: classes.append(role_class)
   tag=tag[:cm.start(2)]+' '.join(classes)+tag[cm.end(2):]
  else:
   tag=tag[:-1]+' class="'+role_class+'">'
  return tag+bar
 html=re.sub(r'(<body\b[^>]*>)',add_accountbar,html,count=1,flags=re.I)
 return Response(html,mimetype='text/html')

@app.route('/api/state', methods=['GET'])
@login_req
def get_state():
 with db() as c:
  rows={r['key']:r['value'] for r in c.execute("SELECT key,value FROM app_state WHERE key IN ('products','outgoing')").fetchall()}
 result={}
 for key in ('products','outgoing'):
  try: result[key]=json.loads(rows[key]) if key in rows else []
  except Exception: result[key]=[]
 return jsonify(result)

@app.route('/api/requests', methods=['GET','POST'])
@login_req
def seller_requests():
 u=who()
 if request.method=='POST':
  if u['role']!='seller': return jsonify(error='فقط فروشنده می‌تواند درخواست ثبت کند.'),403
  payload=request.get_json(silent=True) or {}; barcode=str(payload.get('barcode') or '').strip()
  try: qty=int(payload.get('qty',1))
  except (TypeError,ValueError): qty=1
  qty=max(1,min(qty,20))
  # Commit request before push delivery; push must not delay the seller response.
  with db() as c:
   c.execute('BEGIN IMMEDIATE')
   row=c.execute("SELECT value FROM app_state WHERE key='products'").fetchone()
   try: products=json.loads(row['value']) if row else []
   except Exception: products=[]
   product=next((p for p in products if str(p.get('barcode','')).strip()==barcode and int(p.get('qty') or 0)>0),None)
   if not product: return jsonify(error='این کفش دیگر موجود نیست؛ صفحه را تازه کن.'),409
   if int(product.get('qty') or 0)<qty: return jsonify(error='تعداد درخواستی از موجودی بیشتر است.'),409
   model=str(product.get('model') or 'مدل نامشخص').strip()
   size=str(product.get('size') or '').strip()
   duplicate=c.execute("SELECT id FROM seller_requests WHERE seller_id=? AND status='pending' AND lower(trim(model))=lower(trim(?)) AND lower(trim(COALESCE(color,'')))=lower(trim(?)) AND lower(trim(COALESCE(size,'')))=lower(trim(?)) LIMIT 1",(u['id'],model,str(product.get('color') or '').strip(),size)).fetchone()
   if duplicate: return jsonify(error='برای این مدل، رنگ و سایز قبلاً درخواست در انتظار بررسی ثبت کرده‌ای.'),409
   c.execute('INSERT INTO seller_requests(seller_id,barcode,model,color,size,qty) VALUES(?,?,?,?,?,?)',(u['id'],barcode,model,str(product.get('color') or ''),size,qty))
   rid=c.execute('SELECT last_insert_rowid()').fetchone()[0]
  push_title=f"درخواست جدید: {model}"
  push_body=f"فروشنده: {u['display_name']} | مدل: {model} | رنگ: {product.get('color') or '—'} | سایز: {size or '—'}"
  # Deliver push in the background so the seller receives an immediate success response.
  Thread(target=_send_admin_push,args=(push_title,push_body),daemon=True).start()
  return jsonify(ok=True,id=rid),201
 with db() as c:
  if u['role']=='admin':
   rows=c.execute("SELECT r.*,u.display_name AS seller_name,u.username AS seller_username FROM seller_requests r JOIN users u ON u.id=r.seller_id ORDER BY CASE r.status WHEN 'pending' THEN 0 ELSE 1 END,r.id DESC LIMIT 500").fetchall()
   state=c.execute("SELECT value FROM app_state WHERE key='manager_checked'").fetchone()
   try: checked=set(int(x) for x in json.loads(state['value'])) if state else set()
   except Exception: checked=set()
  else:
   rows=c.execute("SELECT r.* FROM seller_requests r WHERE r.seller_id=? AND r.status!='kept' ORDER BY r.id DESC LIMIT 100",(u['id'],)).fetchall()
   checked=set()
 result=[]
 for row in rows:
  item=dict(row)
  try:
   dt=datetime.strptime(item['created_at'],'%Y-%m-%d %H:%M:%S').replace(tzinfo=timezone.utc).astimezone(ZoneInfo('Asia/Tehran'))
   item['created_at_display']=dt.strftime('%Y/%m/%d %H:%M')
  except Exception: item['created_at_display']=item.get('created_at','')
  item['manager_checked']=int(item['id']) in checked
  result.append(item)
 return jsonify(requests=result)
@app.route('/api/requests/<int:rid>/type', methods=['POST'])
@login_req
def set_request_type(rid):
 u=who()
 payload=request.get_json(silent=True) or {}
 request_type=str(payload.get('request_type') or '').strip()
 if request_type not in ('چپ','جفت','صندوق'):
  return jsonify(error='نوع درخواست نامعتبر است.'),400
 with db() as c:
  row=c.execute('SELECT id,seller_id,status FROM seller_requests WHERE id=?',(rid,)).fetchone()
  if not row: return jsonify(error='درخواست پیدا نشد.'),404
  if u['role']!='admin' and int(row['seller_id'])!=int(u['id']):
   return jsonify(error='دسترسی به این درخواست ندارید.'),403
  if row['status']!='pending':
   return jsonify(error='فقط درخواست‌های در انتظار مدیر قابل تغییر هستند.'),409
  c.execute('UPDATE seller_requests SET request_type=? WHERE id=?',(request_type,rid))
 return jsonify(ok=True,request_type=request_type)

@app.route('/api/requests/<int:rid>/check', methods=['POST'])
@admin_req
def toggle_request_checked(rid):
 with db() as c:
  if not c.execute('SELECT id FROM seller_requests WHERE id=?',(rid,)).fetchone(): return jsonify(error='درخواست پیدا نشد.'),404
  row=c.execute("SELECT value FROM app_state WHERE key='manager_checked'").fetchone()
  try: checked=set(int(x) for x in json.loads(row['value'])) if row else set()
  except Exception: checked=set()
  checked.discard(rid) if rid in checked else checked.add(rid)
  c.execute("INSERT INTO app_state(key,value,updated_at) VALUES('manager_checked',?,CURRENT_TIMESTAMP) ON CONFLICT(key) DO UPDATE SET value=excluded.value,updated_at=CURRENT_TIMESTAMP",(json.dumps(sorted(checked)),))
 return jsonify(ok=True,checked=rid in checked)

@app.route('/api/requests/<int:rid>/resolve', methods=['POST'])
@admin_req
def resolve_seller_request(rid):
 u=who()
 payload=request.get_json(silent=True) or {}; decision=payload.get('decision')
 if decision not in ('sold','kept'): return jsonify(error='تصمیم نامعتبر است.'),400
 with db() as c:
  c.execute('BEGIN IMMEDIATE')
  req=c.execute('SELECT * FROM seller_requests WHERE id=?',(rid,)).fetchone()
  if not req: return jsonify(error='درخواست پیدا نشد.'),404
  if req['status']!='pending': return jsonify(error='این درخواست قبلاً تعیین تکلیف شده است.'),409
  if decision=='sold':
   state=c.execute("SELECT value FROM app_state WHERE key='products'").fetchone()
   try: products=json.loads(state['value']) if state else []
   except Exception: products=[]
   product=next((p for p in products if str(p.get('barcode','')).strip()==req['barcode']),None)
   if not product or int(product.get('qty') or 0)<int(req['qty']): return jsonify(error='موجودی کافی نیست؛ ابتدا موجودی انبار را بررسی کن.'),409
   product['qty']=int(product.get('qty') or 0)-int(req['qty'])
   packed=json.dumps(products,ensure_ascii=False,separators=(',',':'))
   c.execute("INSERT INTO app_state(key,value,updated_at) VALUES('products',?,CURRENT_TIMESTAMP) ON CONFLICT(key) DO UPDATE SET value=excluded.value,updated_at=CURRENT_TIMESTAMP",(packed,))
   outrow=c.execute("SELECT value FROM app_state WHERE key='outgoing'").fetchone()
   try: outgoing=json.loads(outrow['value']) if outrow else []
   except Exception: outgoing=[]
   outgoing.insert(0,{'date':__import__('time').time()*1000,'request_id':int(rid),'barcode':req['barcode'],'model':req['model'],'color':req['color'] or '','size':req['size'] or '','qty':int(req['qty']),'seller':req['seller_id'],'sellerName':c.execute('SELECT display_name FROM users WHERE id=?',(req['seller_id'],)).fetchone()[0],'managerName':u['display_name'],'managerUsername':u['username']})
   c.execute("INSERT INTO app_state(key,value,updated_at) VALUES('outgoing',?,CURRENT_TIMESTAMP) ON CONFLICT(key) DO UPDATE SET value=excluded.value,updated_at=CURRENT_TIMESTAMP",(json.dumps(outgoing,ensure_ascii=False,separators=(',',':')),))
   c.execute('INSERT INTO sales(request_id,seller_id,barcode,model,color,size,qty) VALUES(?,?,?,?,?,?,?)',(rid,req['seller_id'],req['barcode'],req['model'],req['color'],req['size'],req['qty']))
  c.execute("UPDATE seller_requests SET status=?,resolved_at=CURRENT_TIMESTAMP WHERE id=? AND status='pending'",(decision,rid))
 return jsonify(ok=True,status=decision)

@app.route('/api/requests/<int:rid>/delete', methods=['POST'])
@admin_req
def delete_kept_request(rid):
 with db() as c:
  req=c.execute('SELECT status FROM seller_requests WHERE id=?',(rid,)).fetchone()
  if not req: return jsonify(error='درخواست پیدا نشد.'),404
  if req['status']!='kept': return jsonify(error='فقط درخواست‌های «مانده در انبار» قابل پاک‌کردن هستند.'),409
  c.execute('DELETE FROM seller_requests WHERE id=?',(rid,))
 return jsonify(ok=True)

@app.route('/api/requests/delete-kept', methods=['POST'])
@admin_req
def delete_all_kept_requests():
 with db() as c:
  cur=c.execute("DELETE FROM seller_requests WHERE status='kept'")
  count=cur.rowcount
 return jsonify(ok=True,deleted=count)

@app.route('/api/requests/<int:rid>/cancel', methods=['POST'])
@admin_req
def cancel_seller_sale(rid):
 with db() as c:
  c.execute('BEGIN IMMEDIATE')
  req=c.execute("SELECT * FROM seller_requests WHERE id=?",(rid,)).fetchone()
  if not req: return jsonify(error='درخواست پیدا نشد.'),404
  if req['status']!='sold': return jsonify(error='فقط خروج‌های ثبت‌شده قابل لغو هستند.'),409
  sale=c.execute('SELECT * FROM sales WHERE request_id=?',(rid,)).fetchone()
  if not sale: return jsonify(error='رکورد فروش پیدا نشد؛ موجودی تغییر نکرد.'),409
  state=c.execute("SELECT value FROM app_state WHERE key='products'").fetchone()
  try: products=json.loads(state['value']) if state else []
  except Exception: products=[]
  product=next((p for p in products if str(p.get('barcode','')).strip()==req['barcode']),None)
  if not product:
   product={'barcode':req['barcode'],'model':req['model'],'color':req['color'] or '','size':req['size'] or '','qty':0,'created':__import__('time').time()*1000}
   products.append(product)
  product['qty']=int(product.get('qty') or 0)+int(req['qty'])
  c.execute("INSERT INTO app_state(key,value,updated_at) VALUES('products',?,CURRENT_TIMESTAMP) ON CONFLICT(key) DO UPDATE SET value=excluded.value,updated_at=CURRENT_TIMESTAMP",(json.dumps(products,ensure_ascii=False,separators=(',',':')),))
  outrow=c.execute("SELECT value FROM app_state WHERE key='outgoing'").fetchone()
  try: outgoing=json.loads(outrow['value']) if outrow else []
  except Exception: outgoing=[]
  idx=next((i for i,x in enumerate(outgoing) if str(x.get('request_id',''))==str(rid)),None)
  if idx is not None:
   outgoing.pop(idx)
  else:
   # Legacy records do not have request_id; remove one matching outgoing entry if available.
   idx=next((i for i,x in enumerate(outgoing) if str(x.get('barcode','')).strip()==req['barcode'] and str(x.get('seller',''))==str(req['seller_id']) and int(x.get('qty') or 0)==int(req['qty'])),None)
   if idx is not None: outgoing.pop(idx)
  c.execute("INSERT INTO app_state(key,value,updated_at) VALUES('outgoing',?,CURRENT_TIMESTAMP) ON CONFLICT(key) DO UPDATE SET value=excluded.value,updated_at=CURRENT_TIMESTAMP",(json.dumps(outgoing,ensure_ascii=False,separators=(',',':')),))
  c.execute('DELETE FROM sales WHERE request_id=?',(rid,))
  c.execute("UPDATE seller_requests SET status='pending',resolved_at=NULL WHERE id=?",(rid,))
 return jsonify(ok=True,status='pending',restored=int(req['qty']))

@app.route('/api/my-sales', methods=['GET'])
@login_req
def my_sales():
 u=who()
 if u['role']!='seller': return jsonify(pairs_sold=0)
 with db() as c:
  row=c.execute('SELECT COALESCE(SUM(qty),0) AS pairs_sold,COUNT(*) AS sale_count FROM sales WHERE seller_id=?',(u['id'],)).fetchone()
  sales=c.execute('SELECT model,color,size,qty,sold_at FROM sales WHERE seller_id=? ORDER BY id DESC LIMIT 300',(u['id'],)).fetchall()
 today=datetime.now(ZoneInfo('Asia/Tehran')).date()
 today_sales=[]
 for sale in sales:
  item=dict(sale); raw=str(item.get('sold_at') or '')
  try:
   dt=datetime.fromisoformat(raw.replace('Z','+00:00'))
   if dt.tzinfo is None: dt=dt.replace(tzinfo=timezone.utc)
   local=dt.astimezone(ZoneInfo('Asia/Tehran'))
   if local.date()==today:
    item['sold_at_display']=local.strftime('%H:%M'); today_sales.append(item)
  except Exception: pass
 payload=dict(row); payload.update(today_pairs=sum(int(x.get('qty') or 0) for x in today_sales),today_sales=today_sales)
 return jsonify(payload)

@app.route('/api/league', methods=['GET'])
@login_req
def seller_league():
 u=who()
 with db() as c:
  state=c.execute("SELECT value FROM app_state WHERE key='league_start'").fetchone()
  try: league_start=json.loads(state['value']) if state else None
  except Exception: league_start=None
  start_date=(league_start or {}).get('gregorian')
  if start_date:
   rows=c.execute("""SELECT u.id,u.display_name,u.username,
       COALESCE(SUM(CASE WHEN date(s.sold_at)>=date(?) THEN s.qty ELSE 0 END),0) AS pairs_sold,
       COALESCE(SUM(CASE WHEN date(s.sold_at)>=date(?) THEN 1 ELSE 0 END),0) AS sale_count
       FROM users u LEFT JOIN sales s ON s.seller_id=u.id
       WHERE u.role='seller'
       GROUP BY u.id
       ORDER BY pairs_sold DESC, sale_count DESC, u.display_name COLLATE NOCASE ASC""",(start_date,start_date)).fetchall()
  else:
   rows=c.execute("""SELECT u.id,u.display_name,u.username,
       COALESCE(SUM(s.qty),0) AS pairs_sold,
       COUNT(s.id) AS sale_count
       FROM users u LEFT JOIN sales s ON s.seller_id=u.id
       WHERE u.role='seller'
       GROUP BY u.id
       ORDER BY pairs_sold DESC, sale_count DESC, u.display_name COLLATE NOCASE ASC""").fetchall()
 data=[dict(r) for r in rows]
 for i,item in enumerate(data,1): item['rank']=i
 mine=next((x for x in data if x['id']==u['id']),None)
 return jsonify(league=data, mine=mine, total_sellers=len(data), start_date=start_date, start_label=(league_start or {}).get('jalali') if league_start else None, started=bool(start_date))

@app.route('/api/league/start', methods=['POST'])
@admin_req
def start_sales_league():
 payload=request.get_json(silent=True) or {}
 raw=str(payload.get('jalali_date') or '').strip().translate(str.maketrans('۰۱۲۳۴۵۶۷۸۹٠١٢٣٤٥٦٧٨٩','01234567890123456789'))
 match=re.fullmatch(r'(\d{4})\s*[/.-]\s*(\d{1,2})\s*[/.-]\s*(\d{1,2})',raw)
 if not match:
  return jsonify(error='تاریخ را به شکل سال/ماه/روز شمسی وارد کن؛ مثال ۱۴۰۵/۰۸/۱۰'),400
 jy,jm,jd=map(int,match.groups())
 if jm<1 or jm>12 or jd<1 or jd>(31 if jm<=6 else 30):
  return jsonify(error='تاریخ شمسی معتبر نیست.'),400
 try:
  greg=jalali_to_gregorian(jy,jm,jd).isoformat()
 except Exception:
  return jsonify(error='تاریخ واردشده معتبر نیست.'),400
 if not (1300 <= jy <= 1500):
  return jsonify(error='سال شمسی معتبر نیست.'),400
 with db() as c:
  c.execute("INSERT INTO app_state(key,value,updated_at) VALUES('league_start',?,CURRENT_TIMESTAMP) ON CONFLICT(key) DO UPDATE SET value=excluded.value,updated_at=CURRENT_TIMESTAMP",(json.dumps({'gregorian':greg,'jalali':f'{jy:04d}/{jm:02d}/{jd:02d}'},ensure_ascii=False),))
 return jsonify(ok=True,start_date=greg,start_label=f'{jy:04d}/{jm:02d}/{jd:02d}')

@app.route('/api/sales', methods=['GET'])
@admin_req
def sales_report():
 with db() as c:
  sellers=[dict(r) for r in c.execute("SELECT u.id,u.display_name,u.username,COALESCE(SUM(s.qty),0) AS pairs_sold,COUNT(s.id) AS sale_count FROM users u LEFT JOIN sales s ON s.seller_id=u.id WHERE u.role='seller' GROUP BY u.id ORDER BY pairs_sold DESC,u.display_name").fetchall()]
  recent=[dict(r) for r in c.execute("SELECT s.*,u.display_name AS seller_name FROM sales s JOIN users u ON u.id=s.seller_id ORDER BY s.id DESC LIMIT 100").fetchall()]
 return jsonify(sellers=sellers,recent=recent)


@app.route('/api/admin/sellers/<int:seller_id>', methods=['GET'])
@admin_req
def admin_seller_detail(seller_id):
 with db() as c:
  seller=c.execute("SELECT id,username,display_name,created_at FROM users WHERE id=? AND role='seller'",(seller_id,)).fetchone()
  if not seller: return jsonify(error='فروشنده پیدا نشد.'),404
  sales=c.execute("SELECT id,model,color,size,qty,sold_at FROM sales WHERE seller_id=? ORDER BY id DESC LIMIT 200",(seller_id,)).fetchall()
 today=datetime.now(ZoneInfo('Asia/Tehran')).date().isoformat()
 today_sales=[]
 for row in sales:
  item=dict(row)
  raw=str(item.get('sold_at') or '')
  try:
   dt=datetime.fromisoformat(raw.replace('Z','+00:00'))
   if dt.tzinfo is None: dt=dt.replace(tzinfo=timezone.utc)
   local=dt.astimezone(ZoneInfo('Asia/Tehran'))
   item['sold_at_display']=local.strftime('%Y/%m/%d %H:%M')
   if local.date().isoformat()==today: today_sales.append(item)
  except Exception: item['sold_at_display']=raw
 return jsonify(seller=dict(seller),today_sales=today_sales,recent_sales=[dict(x) for x in sales],today_pairs=sum(int(x.get('qty') or 0) for x in today_sales))

@app.route('/api/admin/sellers/<int:seller_id>/password', methods=['POST'])
@admin_req
def admin_seller_password(seller_id):
 payload=request.get_json(silent=True) or {}
 password=str(payload.get('password') or '')
 if len(password)<10: return jsonify(error='رمز عبور باید حداقل ۱۰ کاراکتر باشد.'),400
 with db() as c:
  cur=c.execute("UPDATE users SET password_hash=? WHERE id=? AND role='seller'",(generate_password_hash(password),seller_id))
  if not cur.rowcount: return jsonify(error='فروشنده پیدا نشد.'),404
 return jsonify(ok=True)

@app.route('/api/outgoing/manual', methods=['POST'])
@admin_req
def manual_outgoing():
 u=who()
 payload=request.get_json(silent=True) or {}
 barcode=str(payload.get('barcode') or '').strip()
 try: qty=int(payload.get('qty',1))
 except (TypeError,ValueError): return jsonify(error='تعداد نامعتبر است.'),400
 if not barcode: return jsonify(error='کالا انتخاب نشده است.'),400
 if qty<1 or qty>10000: return jsonify(error='تعداد باید بین ۱ تا ۱۰۰۰۰ جفت باشد.'),400
 with db() as c:
  c.execute('BEGIN IMMEDIATE')
  state=c.execute("SELECT value FROM app_state WHERE key='products'").fetchone()
  try: products=json.loads(state['value']) if state else []
  except Exception: products=[]
  product=next((p for p in products if str(p.get('barcode','')).strip()==barcode),None)
  if not product: return jsonify(error='کالای انتخاب‌شده پیدا نشد؛ صفحه را تازه کن.'),404
  stock=int(product.get('qty') or 0)
  if stock<qty: return jsonify(error=f'موجودی کافی نیست؛ موجودی فعلی {stock} جفت است.'),409
  product['qty']=stock-qty
  packed=json.dumps(products,ensure_ascii=False,separators=(',',':'))
  c.execute("INSERT INTO app_state(key,value,updated_at) VALUES('products',?,CURRENT_TIMESTAMP) ON CONFLICT(key) DO UPDATE SET value=excluded.value,updated_at=CURRENT_TIMESTAMP",(packed,))
  outrow=c.execute("SELECT value FROM app_state WHERE key='outgoing'").fetchone()
  try: outgoing=json.loads(outrow['value']) if outrow else []
  except Exception: outgoing=[]
  now=__import__('time').time()*1000
  outgoing.insert(0,{'date':now,'manual':True,'barcode':barcode,'model':str(product.get('model') or ''),'color':str(product.get('color') or ''),'size':str(product.get('size') or ''),'qty':qty,'managerName':u['display_name'],'managerUsername':u['username'],'description':'خروج دستی توسط '+u['display_name']})
  c.execute("INSERT INTO app_state(key,value,updated_at) VALUES('outgoing',?,CURRENT_TIMESTAMP) ON CONFLICT(key) DO UPDATE SET value=excluded.value,updated_at=CURRENT_TIMESTAMP",(json.dumps(outgoing,ensure_ascii=False,separators=(',',':')),))
 return jsonify(ok=True,product=product,outgoing=outgoing[0],products=products)


@app.route('/api/admin/products/edit-model', methods=['POST'])
@admin_req
def admin_edit_model():
 payload=request.get_json(silent=True) or {}
 old_model=str(payload.get('old_model') or '').strip()
 new_model=str(payload.get('model') or '').strip()
 variants=payload.get('variants')
 if not old_model or not new_model or not isinstance(variants,list):
  return jsonify(error='اطلاعات ویرایش ناقص است.'),400
 with db() as c:
  row=c.execute("SELECT value FROM app_state WHERE key='products'").fetchone()
  try: products=json.loads(row['value']) if row else []
  except Exception: products=[]
  by_code={str(p.get('barcode') or ''):p for p in products}
  # Rename every variant in the model, including zero-stock rows.
  for p in products:
   if str(p.get('model') or '').strip()==old_model: p['model']=new_model
  for v in variants:
   code=str(v.get('barcode') or '')
   p=by_code.get(code)
   if not p: continue
   p['model']=new_model
   p['color']=str(v.get('color') or '').strip()
   p['size']=str(v.get('size') or '').strip()
   try: p['qty']=max(0,int(v.get('qty') or 0))
   except (TypeError,ValueError): return jsonify(error='تعداد باید عدد صحیح و غیرمنفی باشد.'),400
  packed=json.dumps(products,ensure_ascii=False,separators=(',',':'))
  c.execute("INSERT INTO app_state(key,value,updated_at) VALUES('products',?,CURRENT_TIMESTAMP) ON CONFLICT(key) DO UPDATE SET value=excluded.value,updated_at=CURRENT_TIMESTAMP",(packed,))
 return jsonify(ok=True)

@app.route('/api/state', methods=['PUT'])
@login_req
def save_state():
 payload=request.get_json(silent=True) or {}
 with db() as c:
  for key in ('products','outgoing'):
   value=payload.get(key, None)
   if isinstance(value,list):
    packed=json.dumps(value,ensure_ascii=False,separators=(',',':'))
    c.execute("INSERT INTO app_state(key,value,updated_at) VALUES(?,?,CURRENT_TIMESTAMP) ON CONFLICT(key) DO UPDATE SET value=excluded.value,updated_at=CURRENT_TIMESTAMP",(key,packed))
 return jsonify(ok=True)

@app.route('/admin/import-inventory', methods=['POST'])
@admin_req
def import_inventory():
 f=request.files.get('file')
 if not f or not f.filename:
  return jsonify(error='فایل اکسل انتخاب نشده است.'),400
 if not f.filename.lower().endswith(('.xlsx','.xlsm')):
  return jsonify(error='فقط فایل Excel با پسوند xlsx یا xlsm پشتیبانی می‌شود.'),400
 try:
  from openpyxl import load_workbook
  wb=load_workbook(f,read_only=True,data_only=True)
  ws=wb.active
  rows=ws.iter_rows(values_only=True)
  headers=next(rows,None)
  if not headers: return jsonify(error='فایل اکسل خالی است.'),400
  def norm(x):
   s=str(x or '').strip().lower().translate(str.maketrans('۰۱۲۳۴۵۶۷۸۹٠١٢٣٤٥٦٧٨٩','01234567890123456789'))
   s=s.replace('ي','ی').replace('ى','ی').replace('ك','ک').replace('ة','ه')
   return re.sub(r'[\s_\-()\[\]/]+','',s)
  aliases={
   'barcode':{'barcode','بارکد','کدبارکد','کالا','کدکالا','کد'},
   'model':{'model','name','product','productname','مدل','نام','نامکالا','ناممحصول','نامکفش','مدلکفش','ناممدل','ناممدلکالا'},
   'color':{'color','colour','رنگ','رنگبندی'},
   'size':{'size','سایز','اندازه'},
   'subcategory':{'subcategory','subname','variant','نامزیرمجموعه','نامزيرمجموعه','زیرمجموعه','زيرمجموعه'},
   'qty':{'qty','quantity','stock','pairs','count','تعداد','تعدادجفت','موجودی','موجودیجفت','جفت'}
  }
  mapped={}
  for i,h in enumerate(headers):
   hn=norm(h)
   for field,opts in aliases.items():
    if hn in {norm(x) for x in opts} and field not in mapped: mapped[field]=i
  if not all(k in mapped for k in ('model','qty')) or not (('color' in mapped and 'size' in mapped) or 'subcategory' in mapped):
   return jsonify(error='ستون‌های نام کالا، تعداد و نام زیرمجموعه (مثل رنگ / سایز) یا ستون‌های جداگانه رنگ و سایز لازم است.'),400
  def val(row,key):
   i=mapped.get(key)
   return row[i] if i is not None and i<len(row) else None
  def clean_num(v):
   s=str(v if v is not None else '').strip().translate(str.maketrans('۰۱۲۳۴۵۶۷۸۹٠١٢٣٤٥٦٧٨٩','01234567890123456789')).replace(',','')
   try: return max(0,int(float(s)))
   except: return None
  imported=[]
  for idx,row in enumerate(rows, start=2):
   model=str(val(row,'model') or '').strip()
   qty=clean_num(val(row,'qty'))
   if not model: continue
   if qty is None: continue
   if 'color' in mapped and 'size' in mapped:
    color=str(val(row,'color') or '').strip(); size=str(val(row,'size') or '').strip()
   else:
    sub=str(val(row,'subcategory') or '').strip()
    # نمونه فایل: «berberis / 42»؛ بخش قبل از آخرین / رنگ و بعد از آن سایز است.
    if '/' in sub:
     color,size=[x.strip() for x in sub.rsplit('/',1)]
    else:
     color,size=sub,''
   if not color and not size: continue
   raw_barcode=str(val(row,'barcode') or '').strip()
   composite='|'.join((model,color,size))
   barcode=raw_barcode or ('EXCEL-'+hashlib.sha1(composite.encode('utf-8')).hexdigest()[:14])
   imported.append({'barcode':barcode,'model':model,'color':color,'size':size,'qty':qty,'created':0,'sourceExcel':True})
  wb.close()
  if not imported: return jsonify(error='ردیف قابل استفاده‌ای پیدا نشد؛ عنوان ستون‌ها و اطلاعات فایل را بررسی کن.'),400
  if request.form.get('preview_only') == '1':
   grouped={}
   for item in imported:
    key=(item['model'],item['color'],item['size'])
    if key not in grouped:
     grouped[key]={**item,'qty':0}
    grouped[key]['qty']+=item['qty']
   with db() as c:
    state=c.execute("SELECT value FROM app_state WHERE key='products'").fetchone()
    try: current=json.loads(state['value']) if state and state['value'] else []
    except Exception: current=[]
   preview=[]
   for item in grouped.values():
    existing=next((p for p in current if isinstance(p,dict) and str(p.get('model','')).strip()==item['model'] and str(p.get('color','')).strip()==item['color'] and str(p.get('size','')).strip()==item['size']),None)
    preview.append({**item,'current_qty':int(existing.get('qty') or 0) if existing else 0,'exists':bool(existing)})
   preview.sort(key=lambda x:(x['model'],x['color'],x['size']))
   return jsonify(ok=True,preview=True,count=len(imported),unique_count=len(preview),added_pairs=sum(x['qty'] for x in preview),rows=preview)
  with db() as c:
   state=c.execute("SELECT value FROM app_state WHERE key='products'").fetchone()
   try: products=json.loads(state['value']) if state and state['value'] else []
   except Exception: products=[]
   if not isinstance(products,list): products=[]
   added_pairs=0; merged_rows=0; new_rows=0
   for incoming in imported:
    # مدل/رنگ/سایز شناسه ترکیب کفش است؛ رکورد موجود حفظ می‌شود و فقط تعداد زیاد می‌شود.
    same=next((p for p in products if isinstance(p,dict) and str(p.get('model','')).strip()==incoming['model'] and str(p.get('color','')).strip()==incoming['color'] and str(p.get('size','')).strip()==incoming['size']),None)
    if same is not None:
     same['qty']=max(0,int(same.get('qty') or 0))+incoming['qty']
     same.setdefault('sourceExcel',True)
     merged_rows+=1
    else:
     barcode=incoming['barcode']
     used={str(p.get('barcode','')) for p in products if isinstance(p,dict)}
     if barcode in used:
      barcode='EXCEL-'+hashlib.sha1((incoming['model']+'|'+incoming['color']+'|'+incoming['size']+'|'+str(len(products))).encode('utf-8')).hexdigest()[:14]
     incoming['barcode']=barcode
     products.append(incoming)
     new_rows+=1
    added_pairs+=incoming['qty']
   packed=json.dumps(products,ensure_ascii=False,separators=(',',':'))
   c.execute("INSERT INTO app_state(key,value,updated_at) VALUES('products',?,CURRENT_TIMESTAMP) ON CONFLICT(key) DO UPDATE SET value=excluded.value,updated_at=CURRENT_TIMESTAMP",(packed,))
  return jsonify(ok=True,count=len(imported),new_count=new_rows,merged_count=merged_rows,added_pairs=added_pairs,products=products)
 except Exception as e:
  app.logger.exception('Excel import failed')
  return jsonify(error='خواندن فایل اکسل ناموفق بود. مطمئن شو فایل xlsx سالم است.'),400

@app.route('/admin/confirm-import-inventory', methods=['POST'])
@admin_req
def confirm_import_inventory():
 try:
  payload=request.get_json(silent=True) or {}
  rows=payload.get('rows')
  if not isinstance(rows,list) or not rows:
   return jsonify(error='پیش‌نمایش معتبر نیست؛ فایل را دوباره بارگذاری کن.'),400
  cleaned=[]
  for item in rows:
   if not isinstance(item,dict): continue
   model=str(item.get('model') or '').strip()
   color=str(item.get('color') or '').strip()
   size=str(item.get('size') or '').strip()
   try: qty=int(item.get('qty') or 0)
   except Exception: qty=0
   if model and (color or size) and qty>0:
    cleaned.append({'barcode':str(item.get('barcode') or ''),'model':model,'color':color,'size':size,'qty':qty,'created':0,'sourceExcel':True})
  if not cleaned:
   return jsonify(error='ردیف قابل اضافه‌کردن در پیش‌نمایش وجود ندارد.'),400
  with db() as c:
   state=c.execute("SELECT value FROM app_state WHERE key='products'").fetchone()
   try: products=json.loads(state['value']) if state and state['value'] else []
   except Exception: products=[]
   if not isinstance(products,list): products=[]
   new_rows=0; merged_rows=0; added_pairs=0
   for incoming in cleaned:
    same=next((p for p in products if isinstance(p,dict) and str(p.get('model','')).strip()==incoming['model'] and str(p.get('color','')).strip()==incoming['color'] and str(p.get('size','')).strip()==incoming['size']),None)
    if same is not None:
     same['qty']=max(0,int(same.get('qty') or 0))+incoming['qty']
     same.setdefault('sourceExcel',True)
     merged_rows+=1
    else:
     barcode=incoming['barcode'] or ('EXCEL-'+hashlib.sha1((incoming['model']+'|'+incoming['color']+'|'+incoming['size']).encode('utf-8')).hexdigest()[:14])
     used={str(p.get('barcode','')) for p in products if isinstance(p,dict)}
     if barcode in used:
      barcode='EXCEL-'+hashlib.sha1((incoming['model']+'|'+incoming['color']+'|'+incoming['size']+'|'+str(len(products))).encode('utf-8')).hexdigest()[:14]
     incoming['barcode']=barcode
     products.append(incoming)
     new_rows+=1
    added_pairs+=incoming['qty']
   c.execute("INSERT INTO app_state(key,value,updated_at) VALUES('products',?,CURRENT_TIMESTAMP) ON CONFLICT(key) DO UPDATE SET value=excluded.value,updated_at=CURRENT_TIMESTAMP",(json.dumps(products,ensure_ascii=False,separators=(',',':')),))
  return jsonify(ok=True,count=len(cleaned),new_count=new_rows,merged_count=merged_rows,added_pairs=added_pairs,products=products)
 except Exception:
  app.logger.exception('Excel import confirmation failed')
  return jsonify(error='تأیید ورود موجودی ناموفق بود؛ موجودی را بررسی کن.'),400

@app.route('/admin',methods=['GET','POST'])
@admin_req
def admin():
 if request.method=='POST':
  dn=request.form.get('display_name','').strip(); un=request.form.get('username','').strip(); pw=request.form.get('password',''); role=request.form.get('role','seller').strip()
  if role not in ('seller','admin'): role='seller'
  if not dn or len(un)<3 or len(pw)<10: flash('نام لازم است؛ نام کاربری حداقل ۳ و رمز حداقل ۱۰ کاراکتر.','error')
  else:
   try:
    with db() as c: c.execute('INSERT INTO users(username,display_name,password_hash,role) VALUES(?,?,?,?)',(un,dn,generate_password_hash(pw),role))
    flash('اکانت مدیر ساخته شد.' if role=='admin' else 'اکانت فروشنده ساخته شد.','ok')
   except sqlite3.IntegrityError: flash('این نام کاربری قبلاً وجود دارد.','error')
  return redirect('/admin')
 with db() as c: users=c.execute('SELECT id,username,display_name,role,created_at FROM users ORDER BY id').fetchall()
 return render_template('admin.html',users=users)
@app.route('/admin/promote/<int:uid>', methods=['POST'])
@admin_req
def promote(uid):
 with db() as c:
  target=c.execute('SELECT id,role FROM users WHERE id=?',(uid,)).fetchone()
  if not target:
   flash('اکانت پیدا نشد.','error')
  elif target['role']=='admin':
   flash('این حساب از قبل مدیر است.','error')
  else:
   c.execute("UPDATE users SET role='admin' WHERE id=? AND role='seller'",(uid,))
   flash('حساب فروشنده با موفقیت به مدیر ارتقا یافت.','ok')
 return redirect('/admin')

@app.route('/admin/delete/<int:uid>',methods=['POST'])
@admin_req
def delete(uid):
 with db() as c:
  u=c.execute('SELECT role FROM users WHERE id=?',(uid,)).fetchone()
  if u and u['role']=='seller': c.execute('DELETE FROM users WHERE id=?',(uid,))
 flash('اکانت فروشنده حذف شد.','ok'); return redirect('/admin')


# -------------------- Admin database manager --------------------
def _db_tables(conn):
 return [r['name'] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name").fetchall()]
def _db_ident(name):
 return '"'+str(name).replace('"','""')+'"'

@app.route('/database', methods=['GET'])
@admin_req
def database_manager():
 table=request.args.get('table','').strip(); search=request.args.get('q','').strip()
 try: page=max(1,int(request.args.get('page','1')))
 except Exception: page=1
 page_size=50
 with db() as c:
  tables=_db_tables(c)
  if table not in tables: table=tables[0] if tables else ''
  info=[]; rows=[]; total=0
  if table:
   info=[dict(r) for r in c.execute('PRAGMA table_info('+_db_ident(table)+')').fetchall()]
   cols=[r['name'] for r in info]
   where=''; params=[]
   if search:
    terms=[]
    for col in cols:
     terms.append('CAST('+_db_ident(col)+' AS TEXT) LIKE ?'); params.append('%'+search+'%')
    where=' WHERE '+' OR '.join(terms)
   total=c.execute('SELECT COUNT(*) FROM '+_db_ident(table)+where,params).fetchone()[0]
   rows=[dict(r) for r in c.execute('SELECT * FROM '+_db_ident(table)+where+' LIMIT ? OFFSET ?',params+[page_size,(page-1)*page_size]).fetchall()]
  schema=[]
  for t in tables:
   schema.append({'name':t,'count':c.execute('SELECT COUNT(*) FROM '+_db_ident(t)).fetchone()[0]})
 return render_template('database.html',tables=schema,table=table,columns=info,rows=rows,total=total,page=page,page_size=page_size,q=search)

@app.route('/database/export/sql')
@admin_req
def database_export_sql():
 with db() as c: payload='\n'.join(c.iterdump()).encode('utf-8')
 return send_file(__import__('io').BytesIO(payload),mimetype='application/sql',as_attachment=True,download_name='queli-shop-database.sql')

@app.route('/database/export/sqlite')
@admin_req
def database_export_sqlite():
 with db() as c: payload=c.serialize()
 return send_file(__import__('io').BytesIO(payload),mimetype='application/vnd.sqlite3',as_attachment=True,download_name='queli-shop-database.sqlite3')

@app.route('/database/import',methods=['POST'])
@admin_req
def database_import():
 uploaded=request.files.get('database_file')
 if request.form.get('confirm_replace')!='yes':
  flash('برای جایگزینی دیتابیس باید تأیید را فعال کنی.','error'); return redirect('/database')
 if not uploaded or not uploaded.filename:
  flash('فایل دیتابیس انتخاب نشده است.','error'); return redirect('/database')
 filename=uploaded.filename.lower()
 if not filename.endswith(('.sqlite','.sqlite3','.db')):
  flash('فقط فایل دیتابیس SQLite با پسوند db، sqlite یا sqlite3 قابل ورود است.','error'); return redirect('/database')
 import tempfile
 tmp_path=None
 try:
  fd,tmp_path=tempfile.mkstemp(prefix='queli-import-',suffix='.sqlite',dir=str(Path(DB).resolve().parent))
  os.close(fd); uploaded.save(tmp_path)
  if os.path.getsize(tmp_path)>100*1024*1024: raise ValueError('حجم فایل بیشتر از ۱۰۰ مگابایت است.')
  src=sqlite3.connect('file:'+tmp_path+'?mode=ro',uri=True,timeout=30)
  try:
   check=src.execute('PRAGMA integrity_check').fetchone()
   if not check or check[0]!='ok': raise ValueError('ساختار فایل دیتابیس سالم نیست.')
   names={r[0] for r in src.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()}
   if not {'users','app_state','seller_requests','sales'}.issubset(names):
    raise ValueError('این فایل دیتابیس کوئلی شاپ نیست یا جدول‌های اصلی را ندارد.')
   admins=src.execute("SELECT COUNT(*) FROM users WHERE role='admin'").fetchone()[0]
   if not admins: raise ValueError('فایل انتخاب‌شده هیچ حساب مدیری ندارد؛ برای جلوگیری از قفل‌شدن پنل وارد نشد.')
   dest=sqlite3.connect(DB,timeout=60)
   try:
    dest.execute('PRAGMA busy_timeout=60000')
    src.backup(dest)
    dest.commit()
   finally: dest.close()
  finally: src.close()
  flash('دیتابیس با موفقیت جایگزین شد. برای بارگذاری کامل اطلاعات، یک‌بار از حساب خارج شو و دوباره وارد شو.','success')
 except Exception as e:
  flash('ورود دیتابیس ناموفق بود: '+str(e),'error')
 finally:
  if tmp_path and os.path.exists(tmp_path):
   try: os.remove(tmp_path)
   except OSError: pass
 return redirect('/database')


@app.route('/logout')
def logout(): session.clear(); return redirect('/')
@app.errorhandler(403)
def forbidden(e): return 'دسترسی غیرمجاز',403
init()
