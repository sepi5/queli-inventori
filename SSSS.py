import os, sqlite3, secrets, json, hashlib, re
from datetime import datetime, timezone
from zoneinfo import ZoneInfo
from pathlib import Path
from functools import wraps
from flask import Flask, request, redirect, url_for, session, render_template, flash, abort, Response, jsonify
from werkzeug.security import generate_password_hash, check_password_hash
D=Path(__file__).resolve().parent
DB=os.environ.get('SHOESHOP_DB',str(D/'users.db'))
app=Flask(__name__); app.secret_key=os.environ.get('SECRET_KEY') or secrets.token_hex(32)
app.config.update(SESSION_COOKIE_HTTPONLY=True,SESSION_COOKIE_SAMESITE='Lax',SESSION_COOKIE_SECURE=os.environ.get('COOKIE_SECURE','0')=='1',PERMANENT_SESSION_LIFETIME=43200)
def db():
 c=sqlite3.connect(DB); c.row_factory=sqlite3.Row; return c
def init():
 with db() as c:
  c.execute("CREATE TABLE IF NOT EXISTS users(id INTEGER PRIMARY KEY,username TEXT UNIQUE NOT NULL,display_name TEXT NOT NULL,password_hash TEXT NOT NULL,role TEXT NOT NULL CHECK(role IN ('admin','seller')),created_at TEXT DEFAULT CURRENT_TIMESTAMP)")
  c.execute("CREATE TABLE IF NOT EXISTS app_state(key TEXT PRIMARY KEY, value TEXT NOT NULL, updated_at TEXT DEFAULT CURRENT_TIMESTAMP)")
  c.execute("CREATE TABLE IF NOT EXISTS seller_requests(id INTEGER PRIMARY KEY AUTOINCREMENT, seller_id INTEGER NOT NULL, barcode TEXT NOT NULL, model TEXT NOT NULL, color TEXT, size TEXT, qty INTEGER NOT NULL DEFAULT 1, status TEXT NOT NULL DEFAULT 'pending' CHECK(status IN ('pending','sold','kept')), created_at TEXT DEFAULT CURRENT_TIMESTAMP, resolved_at TEXT, FOREIGN KEY(seller_id) REFERENCES users(id))")
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
 bar='<div class="accountbar">ورود: '+str(u['display_name'])+(' · مدیر' if u['role']=='admin' else ' · فروشنده')+' | <a href="/logout">خروج</a>'+(' | <a href="/admin">مدیریت فروشنده‌ها</a>' if u['role']=='admin' else '')+'</div>'
 import re
 html=re.sub(r'(<body\b[^>]*>)',lambda m:m.group(1)+bar,html,count=1,flags=re.I)
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
  with db() as c:
   row=c.execute("SELECT value FROM app_state WHERE key='products'").fetchone()
   try: products=json.loads(row['value']) if row else []
   except Exception: products=[]
   product=next((p for p in products if str(p.get('barcode','')).strip()==barcode and int(p.get('qty') or 0)>0),None)
   if not product: return jsonify(error='این کفش دیگر موجود نیست؛ صفحه را تازه کن.'),409
   if int(product.get('qty') or 0)<qty: return jsonify(error='تعداد درخواستی از موجودی بیشتر است.'),409
   c.execute('INSERT INTO seller_requests(seller_id,barcode,model,color,size,qty) VALUES(?,?,?,?,?,?)',(u['id'],barcode,str(product.get('model') or 'مدل نامشخص'),str(product.get('color') or ''),str(product.get('size') or ''),qty))
   rid=c.execute('SELECT last_insert_rowid()').fetchone()[0]
  return jsonify(ok=True,id=rid)
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
 return jsonify(dict(row))

@app.route('/api/sales', methods=['GET'])
@admin_req
def sales_report():
 with db() as c:
  sellers=[dict(r) for r in c.execute("SELECT u.id,u.display_name,u.username,COALESCE(SUM(s.qty),0) AS pairs_sold,COUNT(s.id) AS sale_count FROM users u LEFT JOIN sales s ON s.seller_id=u.id WHERE u.role='seller' GROUP BY u.id ORDER BY pairs_sold DESC,u.display_name").fetchall()]
  recent=[dict(r) for r in c.execute("SELECT s.*,u.display_name AS seller_name FROM sales s JOIN users u ON u.id=s.seller_id ORDER BY s.id DESC LIMIT 100").fetchall()]
 return jsonify(sellers=sellers,recent=recent)

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
@app.route('/logout')
def logout(): session.clear(); return redirect('/')
@app.errorhandler(403)
def forbidden(e): return 'دسترسی غیرمجاز',403
init()
