// The same SQLite records as the Python bot, plus durable webhook delivery jobs.
export const schema = `
CREATE TABLE IF NOT EXISTS sessions(user_id INTEGER PRIMARY KEY, nonce TEXT NOT NULL, step TEXT NOT NULL, data TEXT NOT NULL, updated REAL NOT NULL);
CREATE TABLE IF NOT EXISTS bookings(id TEXT PRIMARY KEY,user_id INTEGER NOT NULL,data TEXT NOT NULL,status TEXT NOT NULL,created REAL NOT NULL,scheduled REAL,duration INTEGER NOT NULL DEFAULT 120,version INTEGER NOT NULL DEFAULT 1);
CREATE UNIQUE INDEX IF NOT EXISTS active_booking ON bookings(user_id) WHERE status IN ('pending','confirmed');
CREATE TABLE IF NOT EXISTS outbox(id INTEGER PRIMARY KEY AUTOINCREMENT,booking_id TEXT NOT NULL REFERENCES bookings(id) ON DELETE CASCADE,recipient INTEGER NOT NULL,kind TEXT NOT NULL,version INTEGER NOT NULL,due REAL NOT NULL,attempts INTEGER NOT NULL DEFAULT 0,UNIQUE(booking_id,recipient,kind,version));
CREATE TABLE IF NOT EXISTS sent_messages(user_id INTEGER NOT NULL,chat_id INTEGER NOT NULL,message_id INTEGER NOT NULL,booking_id TEXT NOT NULL REFERENCES bookings(id) ON DELETE CASCADE,PRIMARY KEY(chat_id,message_id));
CREATE TABLE IF NOT EXISTS replies(id INTEGER PRIMARY KEY AUTOINCREMENT,user_id INTEGER NOT NULL,recipient INTEGER NOT NULL,method TEXT NOT NULL,body TEXT NOT NULL,booking_id TEXT REFERENCES bookings(id) ON DELETE CASCADE,due REAL NOT NULL,attempts INTEGER NOT NULL DEFAULT 0,created REAL NOT NULL);
CREATE TABLE IF NOT EXISTS received_updates(update_id INTEGER PRIMARY KEY,created REAL NOT NULL);
CREATE TABLE IF NOT EXISTS settings(key TEXT PRIMARY KEY,value TEXT NOT NULL);
`;
export const now = () => Date.now()/1000;
const decode = row => row ? {...row,data:JSON.parse(row.data)} : null;
const id = length => crypto.randomUUID().replaceAll('-', '').slice(0,length);
export class Store {
  constructor(sql, transaction = fn => fn()) {
    this.sql=sql; this.depth=0;
    this.transaction=fn=> {
      if(this.depth)return fn();
      return transaction(()=>{this.depth++;try{return fn();}finally{this.depth--;}});
    };
    sql.exec(schema);
  }
  rows(query,...args) {return Array.from(this.sql.exec(query,...args));}
  one(query,...args) {return this.rows(query,...args)[0] ?? null;}
  setting(key,fallback=null) {return this.one('SELECT value FROM settings WHERE key=?',key)?.value ?? fallback;}
  setSetting(key,value) {this.sql.exec('INSERT OR REPLACE INTO settings VALUES(?,?)',key,String(value));}
  session(user) {return decode(this.one('SELECT * FROM sessions WHERE user_id=?',user));}
  saveSession(user,step,data,nonce=null) {
    nonce ||= this.session(user)?.nonce || id(8);
    this.sql.exec('INSERT OR REPLACE INTO sessions VALUES(?,?,?,?,?)',user,nonce,step,JSON.stringify(data),now());
    return this.session(user);
  }
  dropSession(user) {this.sql.exec('DELETE FROM sessions WHERE user_id=?',user);}
  booking(key) {return decode(this.one('SELECT * FROM bookings WHERE id=?',key));}
  active(user) {const row=this.one("SELECT id FROM bookings WHERE user_id=? AND status IN ('pending','confirmed')",user);return row?this.booking(row.id):null;}
  list() {return this.rows("SELECT * FROM bookings WHERE status IN ('pending','confirmed') ORDER BY created DESC LIMIT 20").map(decode);}
  enqueue(key,recipient,kind,version,due) {
    this.sql.exec('INSERT OR IGNORE INTO outbox(booking_id,recipient,kind,version,due) VALUES(?,?,?,?,?)',key,recipient,kind,version,due);
  }
  submit(user,admins) {
    const s=this.session(user);
    if(!s || s.step!=='review' || !s.data.consent || !s.data.adult || ['name','idea','placement','size','availability'].some(k=>!s.data[k])) throw Error('Анкета не заполнена или нет согласия.');
    const current=this.active(user); if(current)return current;
    const key=id(10);
    this.transaction(()=>{
      this.sql.exec('INSERT INTO bookings(id,user_id,data,status,created) VALUES(?,?,?,?,?)',key,user,JSON.stringify(s.data),'pending',now());
      for(const admin of admins)this.enqueue(key,admin,'new',1,now());
      this.dropSession(user);
    });
    return this.booking(key);
  }
  transition(key,status,admins,scheduled=null,duration=120) {
    const b=this.booking(key);
    if(!b || !['pending','confirmed'].includes(b.status))throw Error('Заявка уже закрыта или не найдена.');
    if(!['confirmed','declined','cancelled','completed'].includes(status))throw Error('Неизвестный статус.');
    if(status==='confirmed') {
      if(!Number.isFinite(scheduled)||scheduled<=now()||!Number.isInteger(duration)||duration<30||duration>720)throw Error('Нужна будущая дата и длительность от 30 до 720 минут.');
      const clash=this.one("SELECT id FROM bookings WHERE status='confirmed' AND id<>? AND scheduled<? AND scheduled+duration*60>?",key,scheduled+duration*60,scheduled);
      if(clash)throw Error('Время пересекается с записью '+clash.id+'.');
    }
    if(status==='completed'&&b.status!=='confirmed')throw Error('Сначала подтвердите сеанс.');
    const version=b.version+1;
    this.transaction(()=>{
      this.sql.exec('UPDATE bookings SET status=?,scheduled=?,duration=?,version=? WHERE id=?',status,status==='confirmed'?scheduled:b.scheduled,duration,version,key);
      this.sql.exec('DELETE FROM outbox WHERE booking_id=?',key);
      this.enqueue(key,b.user_id,'status',version,now());
      for(const admin of admins)this.enqueue(key,admin,'admin_status',version,now());
      if(status==='confirmed')for(const hours of [24,2]) {
        const due=scheduled-hours*3600;
        if(due>now())this.enqueue(key,b.user_id,'reminder'+hours,version,due);
      }
    });
    return this.booking(key);
  }
  reply(user,recipient,method,body,bookingId=null) {
    this.sql.exec('INSERT INTO replies(user_id,recipient,method,body,booking_id,due,created) VALUES(?,?,?,?,?,?,?)',user,recipient,method,JSON.stringify(body),bookingId,now(),now());
  }
  track(user,chat,message,key) {this.sql.exec('INSERT OR IGNORE INTO sent_messages VALUES(?,?,?,?)',user,chat,message,key);}
  eraseBooking(key) {
    const copies=this.rows('SELECT * FROM sent_messages WHERE booking_id=?',key);
    this.sql.exec('DELETE FROM outbox WHERE booking_id=?',key);
    this.sql.exec('DELETE FROM replies WHERE booking_id=?',key);
    this.sql.exec('DELETE FROM sent_messages WHERE booking_id=?',key);
    this.sql.exec('DELETE FROM bookings WHERE id=?',key);
    return copies;
  }
  erase(user) {
    let copies=[];
    this.transaction(()=>{
      for(const b of this.rows('SELECT id FROM bookings WHERE user_id=?',user))copies.push(...this.eraseBooking(b.id));
      this.dropSession(user);
      this.sql.exec('DELETE FROM replies WHERE user_id=?',user);
    });
    return copies;
  }
  due(table) {if(!['outbox','replies'].includes(table))throw Error('Invalid queue');return this.rows(`SELECT * FROM ${table} WHERE due<=? ORDER BY id LIMIT 12`,now());}
  delivered(table,key) {if(!['outbox','replies'].includes(table))throw Error('Invalid queue');this.sql.exec(`DELETE FROM ${table} WHERE id=?`,key);}
  retry(table,row,seconds=null) {
    const attempts=row.attempts+1;
    const delay=seconds??Math.min(3600,30*2**Math.min(attempts,7));
    this.sql.exec(`UPDATE ${table} SET attempts=?,due=? WHERE id=?`,attempts,now()+delay,row.id);
  }
  nextDue() {
    return this.one('SELECT MIN(due) AS due FROM (SELECT due FROM outbox UNION ALL SELECT due FROM replies)')?.due ?? null;
  }
  cleanup(days) {
    this.sql.exec('DELETE FROM sessions WHERE updated<?',now()-7*86400);
    this.sql.exec('DELETE FROM received_updates WHERE created<?',now()-2*86400);
    this.sql.exec('DELETE FROM replies WHERE booking_id IS NULL AND created<?',now()-7*86400);
    const copies=[];
    for(const b of this.rows('SELECT id FROM bookings WHERE COALESCE(scheduled,created)<?',now()-days*86400))copies.push(...this.eraseBooking(b.id));
    return copies;
  }
  importSnapshot(snapshot) {
    if(this.setting('imported')||this.setting('active')==='true'||this.one('SELECT COUNT(*) AS n FROM bookings').n||this.one('SELECT COUNT(*) AS n FROM sessions').n)throw Error('Import requires a new inactive database');
    const columns={sessions:['user_id','nonce','step','data','updated'],bookings:['id','user_id','data','status','created','scheduled','duration','version'],outbox:['id','booking_id','recipient','kind','version','due','attempts'],sent_messages:['user_id','chat_id','message_id','booking_id']};
    this.transaction(()=>{
      for(const [table,fields] of Object.entries(columns)) {
        const rows=snapshot[table];
        if(!Array.isArray(rows)||rows.length>10000)throw Error('Invalid snapshot');
        for(const row of rows)this.sql.exec(`INSERT INTO ${table}(${fields.join(',')}) VALUES(${fields.map(()=>'?').join(',')})`,...fields.map(k=>row[k]??null));
      }
      this.setSetting('imported','true');
    });
    return this.counts();
  }
  counts() {return Object.fromEntries(['sessions','bookings','outbox','sent_messages','replies'].map(t=>[t,this.one(`SELECT COUNT(*) AS n FROM ${t}`).n]));}
}
