import {test} from 'node:test';
import assert from 'node:assert/strict';
import {DatabaseSync} from 'node:sqlite';
import {Store,schema,now} from '../src/store.mjs';
import {Flow,App,settings,bookingText,parseTime} from '../src/core.mjs';

export const env={ADMIN_IDS:'1888165622,1092851573',OPERATOR_NAME:'by:Chaildy',PRIVACY_CONTACT:'@spider_013',MASTER_TELEGRAM:'spider_013',PRIVACY_READY:'true',TIMEZONE:'Europe/Moscow'};
function database(){
  const db=new DatabaseSync(':memory:');db.exec('PRAGMA foreign_keys=ON');
  const sql={exec(query,...args){if(query===schema){db.exec(query);return [];}return db.prepare(query).all(...args);}};
  const store=new Store(sql,fn=>{db.exec('BEGIN');try{const result=fn();db.exec('COMMIT');return result;}catch(e){db.exec('ROLLBACK');throw e;}});
  return {db,store};
}
const cfg=await settings(env);
function click(flow,store,user,action){const s=store.session(user);return flow.action(user,`f:${s.nonce}:${s.step}:${action}`,'sample_user');}
function complete(store,user=7,submit=true,config=cfg){const flow=new Flow(store,config);flow.begin(user);click(flow,store,user,'accept');click(flow,store,user,'adult');for(const answer of ['Пример','Веточка','Предплечье','8 см','Выходные'])flow.answer(user,answer);click(flow,store,user,'skip');click(flow,store,user,'next');if(submit){click(flow,store,user,'submit');return store.active(user);}return flow;}
function message(user,text,chatType='private'){return {update_id:1,message:{from:{id:user},chat:{id:user,type:chatType},text}};}
function replies(store){return store.rows('SELECT * FROM replies ORDER BY id').map(row=>({...row,body:JSON.parse(row.body)}));}

test('public client can submit; both owner and project admin receive durable notification',()=>{
  const {store}=database(),b=complete(store);assert.equal(b.status,'pending');assert.equal(b.scheduled,null);assert.equal(b.data.test,false);assert.ok(b.data.consent.sha256.match(/^[a-f0-9]{64}$/));assert.match(b.data.consent.text,/Cloudflare/);assert.equal(store.session(7),null);
  assert.deepEqual(new Set(store.due('outbox').map(x=>x.recipient)),new Set([1888165622,1092851573]));
});
test('no input retained before consent; refusal and underage erase draft',()=>{
  const {store}=database(),flow=new Flow(store,cfg);flow.begin(7);flow.answer(7,'Без согласия');assert.deepEqual(store.session(7).data,{});assert.throws(()=>store.submit(7,cfg.admins));click(flow,store,7,'refuse');assert.equal(store.session(7),null);flow.begin(7);click(flow,store,7,'accept');click(flow,store,7,'minor');assert.equal(store.session(7),null);
});
test('stale button cannot advance consent and duplicate submission creates one booking',()=>{
  const {store}=database(),flow=new Flow(store,cfg);flow.begin(7);assert.match(flow.action(7,'f:stale:review:submit').text,/устарела/);assert.equal(store.session(7).step,'consent');complete(store);assert.match(flow.begin(7).text,/уже/);assert.equal(store.list().length,1);
});
test('editing returns to review; arbitrary HTML in client answers is escaped',()=>{
  const {store}=database(),flow=complete(store,7,false);click(flow,store,7,'idea');flow.answer(7,'<script>alert(1)</script>');assert.equal(store.session(7).step,'review');const reply=flow.prompt(store.session(7));assert.match(reply.text,/&lt;script&gt;/);assert.doesNotMatch(reply.text,/<script>/);
});
test('photo references capped at three; an overlong name does not advance',()=>{
  const {store}=database(),flow=new Flow(store,cfg);flow.begin(7);click(flow,store,7,'accept');click(flow,store,7,'adult');flow.answer(7,'я'.repeat(61));assert.equal(store.session(7).step,'name');complete(store,7,false);click(flow,store,7,'refs');for(const p of ['a','b','c','d'])flow.answer(7,null,p);assert.equal(store.session(7).data.refs.length,3);
});
test('disabled and admin-only modes block public users and old callbacks',async()=>{
  const {store}=database(),flow=complete(store,7,false);const disabled=await settings({...env,PRIVACY_READY:'false'});assert.match(new Flow(store,disabled).begin(8).text,/скоро/);assert.match(new Flow(store,disabled).action(7,`f:${store.session(7).nonce}:review:submit`).text,/недоступны/);const testCfg=await settings({...env,TEST_MODE:'true'});assert.equal(testCfg.allowed(7),false);assert.equal(testCfg.allowed(1092851573),true);assert.match(new Flow(store,testCfg).begin(1092851573).text,/Тест/);
});
test('owner can view client and photos; outsider and group cannot read them',()=>{
  const {store}=database(),b=complete(store),app=new App(store,cfg);app.handle(message(9,'/view '+b.id));assert.match(replies(store).at(-1).body.text,/доступна мастеру/);const n=replies(store).length;app.handle(message(1092851573,'/view '+b.id,'group'));assert.equal(replies(store).length,n);app.handle(message(1092851573,'/view '+b.id));assert.match(replies(store).at(-1).body.text,/Пример/);assert.match(replies(store).at(-1).body.text,/tg:\/\/user\?id=7/);
});
test('admin callback checks actual clicking actor, not author of bot message',()=>{
  const {store}=database(),b=complete(store),app=new App(store,cfg);app.callback(9,'admin_view:'+b.id,null,false);assert.match(replies(store).at(-1).body.text,/доступна мастеру/);app.callback(1092851573,'admin_view:'+b.id,null,true);assert.match(replies(store).at(-1).body.text,/Пример/);
});
test('another client cannot cancel a booking',()=>{
  const {store}=database(),b=complete(store);new App(store,cfg).callback(8,'cancel_yes:'+b.id,null,false);assert.equal(store.booking(b.id).status,'pending');
});
test('confirmation validates future date, overlap and duration; reschedule replaces reminders',()=>{
  const {store}=database(),a=complete(store,7),b=complete(store,8),date=now()+3*86400;
  assert.throws(()=>store.transition(a.id,'confirmed',cfg.admins,now()-1,120));assert.throws(()=>store.transition(a.id,'confirmed',cfg.admins,date,10));store.transition(a.id,'confirmed',cfg.admins,date,120);assert.throws(()=>store.transition(b.id,'confirmed',cfg.admins,date+3600,60),/пересекается/);
  store.transition(a.id,'confirmed',cfg.admins,date+86400,60);const reminders=store.rows("SELECT * FROM outbox WHERE kind LIKE 'reminder%'");assert.equal(reminders.length,2);assert.ok(reminders.every(x=>x.version===3));store.transition(a.id,'cancelled',cfg.admins);assert.equal(store.rows("SELECT * FROM outbox WHERE kind LIKE 'reminder%'").length,0);assert.equal(store.active(7),null);
});
test('Moscow appointment time conversion and invalid calendar date',()=>{assert.equal(parseTime('2026-12-12','15:00','Europe/Moscow'),Date.parse('2026-12-12T12:00:00Z')/1000);assert.throws(()=>parseTime('2026-02-30','15:00','Europe/Moscow'));});
test('deletion clears forms, notifications, queued views and copies',()=>{
  const {store}=database(),b=complete(store),app=new App(store,cfg);app.view(1092851573,b.id);store.track(7,1092851573,20,b.id);const copies=store.erase(7);assert.equal(copies.length,1);assert.equal(store.booking(b.id),null);assert.equal(store.due('outbox').length,0);assert.equal(store.rows('SELECT * FROM replies WHERE user_id=7').length,0);assert.equal(store.rows('SELECT * FROM sent_messages').length,0);
});
test('retention purges old booking but preserves a newer draft',()=>{
  const {store}=database(),b=complete(store);store.transition(b.id,'declined',cfg.admins);store.sql.exec('UPDATE bookings SET created=? WHERE id=?',now()-91*86400,b.id);store.saveSession(7,'consent',{});store.cleanup(90);assert.equal(store.booking(b.id),null);assert.ok(store.session(7));store.sql.exec('UPDATE sessions SET updated=?',now()-8*86400);store.cleanup(90);assert.equal(store.session(7),null);
});
test('Python SQLite snapshot preserves consent, statuses, notification IDs and is one-shot',()=>{
  const original=database().store,b=complete(original);original.track(7,1092851573,90,b.id);const snapshot=Object.fromEntries(['sessions','bookings','outbox','sent_messages'].map(t=>[t,original.rows(`SELECT * FROM ${t}`)]));const {store}=database();store.importSnapshot(snapshot);assert.deepEqual(store.booking(b.id),original.booking(b.id));assert.equal(store.counts().sent_messages,1);assert.throws(()=>store.importSnapshot(snapshot));
});
test('failed nested transaction rolls back booking and durable response together',()=>{
  const {store}=database();complete(store,7,false);assert.throws(()=>store.transaction(()=>{store.submit(7,cfg.admins);store.reply(7,7,'sendMessage',{text:'x'});throw Error('rollback');}));assert.equal(store.active(7),null);assert.equal(store.rows('SELECT * FROM replies').length,0);assert.equal(store.session(7).step,'review');
});
test('retry backs off without losing queue and maximum form fits Telegram',()=>{
  const {store}=database(),b=complete(store);const row=store.due('outbox')[0];store.retry('outbox',row);assert.equal(store.one('SELECT attempts FROM outbox WHERE id=?',row.id).attempts,1);assert.ok(store.one('SELECT due FROM outbox WHERE id=?',row.id).due>now());b.data.name='Я'.repeat(60);b.data.idea='Я'.repeat(900);for(const k of ['placement','size','availability','budget'])b.data[k]='Я'.repeat(240);assert.ok(bookingText(b,cfg,true).length<4096);
});
