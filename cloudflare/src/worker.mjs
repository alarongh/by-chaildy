import {DurableObject} from 'cloudflare:workers';
import {Store,now} from './store.mjs';
import {App,settings,bookingText,markup} from './core.mjs';
const json=(value,status=200)=>Response.json(value,{status,headers:{'Cache-Control':'no-store'}});
function secretEquals(a,b) {
  if(typeof b!=='string'||b.length<32||typeof a!=='string')return false;
  let diff=a.length^b.length;
  for(let i=0;i<b.length;i++)diff|=(a.charCodeAt(i)||0)^b.charCodeAt(i);
  return diff===0;
}
export default {
  async fetch(request,env) {
    const path=new URL(request.url).pathname;
    if(path==='/webhook') {
      if(request.method!=='POST')return json({error:'method'},405);
      if(!secretEquals(request.headers.get('X-Telegram-Bot-Api-Secret-Token'),env.WEBHOOK_SECRET))return json({error:'unauthorized'},403);
    } else if(path.startsWith('/_admin/')) {
      if(!secretEquals(request.headers.get('X-Deployment-Secret'),env.DEPLOY_SECRET))return json({error:'unauthorized'},403);
      if(!['/_admin/status','/_admin/import','/_admin/activate','/_admin/deactivate'].includes(path))return json({error:'not_found'},404);
      if(request.method!==(path==='/_admin/status'?'GET':'POST'))return json({error:'method'},405);
    } else if(path!=='/health'||request.method!=='GET')return json({error:'not_found'},404);
    try {
      return await env.BOOKING.get(env.BOOKING.idFromName('by-chaildy-main')).fetch(request);
    } catch {return json({error:'temporarily_unavailable'},503);}
  }
};
export class BookingOffice extends DurableObject {
  constructor(ctx,env) {
    super(ctx,env);
    this.store=new Store(ctx.storage.sql,fn=>ctx.storage.transactionSync(fn));
    this.cfg=settings(env);
    this.queue=Promise.resolve();
  }
  serial(fn) {
    const result=this.queue.then(fn);
    this.queue=result.catch(()=>{});
    return result;
  }
  async fetch(request) {
    const path=new URL(request.url).pathname;
    let input;
    if(request.method==='POST'&&!['/_admin/activate','/_admin/deactivate'].includes(path)) {
      const limit=path==='/_admin/import'?5*1024*1024:64*1024;
      if(Number(request.headers.get('content-length')||0)>limit)return json({error:'too_large'},413);
      const text=await request.text();
      if(new TextEncoder().encode(text).length>limit)return json({error:'too_large'},413);
      try{input=JSON.parse(text);}catch{return json({error:'invalid_json'},400);}
    }
    return this.serial(async()=>{
      const cfg=await this.cfg;
      const active=this.store.setting('active')==='true';
      if(path==='/health')return json({ok:true,service:'by-chaildy-bot',active:active&&!!cfg.canCollect});
      if(path==='/_admin/status')return json({active,configured:!!cfg.canCollect,imported:this.store.setting('imported')==='true',counts:this.store.counts()});
      if(path==='/_admin/import') {
        try{return json({ok:true,counts:this.store.importSnapshot(input)});}catch{return json({error:'import_rejected'},409);}
      }
      if(path==='/_admin/activate') {
        if(!cfg.canCollect||!this.env.BOT_TOKEN||this.store.setting('imported')!=='true')return json({error:'configuration_incomplete'},409);
        this.store.setSetting('active','true');await this.planAlarm();return json({ok:true,active:true});
      }
      if(path==='/_admin/deactivate'){this.store.setSetting('active','false');await this.ctx.storage.deleteAlarm();return json({ok:true,active:false});}
      if(!active)return json({error:'not_activated'},503);
      if(!input||!Number.isSafeInteger(input.update_id)||input.update_id<0||(!input.message&&!input.callback_query))return json({error:'invalid_update'},400);
      this.store.transaction(()=>{
        if(this.store.one('SELECT update_id FROM received_updates WHERE update_id=?',input.update_id))return;
        new App(this.store,cfg).handle(input);
        this.store.sql.exec('INSERT INTO received_updates VALUES(?,?)',input.update_id,now());
      });
      await this.planAlarm();
      return json({ok:true});
    });
  }
  async planAlarm() {
    if(this.store.setting('active')!=='true')return;
    const due=this.store.nextDue();
    const cleanup=Number(this.store.setting('last_cleanup',0))+3600;
    const at=Math.max(Date.now()+100,Math.min(due??cleanup,cleanup)*1000);
    const current=await this.ctx.storage.getAlarm();
    if(current===null||current>at)await this.ctx.storage.setAlarm(at);
  }
  async telegram(method,body) {
    try {
      const request=new Request(`https://api.telegram.org/bot${this.env.BOT_TOKEN}/${method}`,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body),signal:AbortSignal.timeout(5000)});
      const response=this.env.TELEGRAM_TEST?await this.env.TELEGRAM_TEST.fetch(request):await fetch(request);
      const result=await response.json();
      if(result.ok)return {ok:true,result:result.result};
      return {ok:false,code:result.error_code||500,retry:result.parameters?.retry_after};
    } catch {return {ok:false,code:503};}
  }
  async alarm() {
    return this.serial(async()=>{
      if(this.store.setting('active')!=='true')return;
      const cfg=await this.cfg,app=new App(this.store,cfg),started=Date.now();
      if(now()-Number(this.store.setting('last_cleanup',0))>=3600) {
        this.store.transaction(()=>{app.cleanupCopies(this.store.cleanup(cfg.days));this.store.setSetting('last_cleanup',now());});
      }
      // Every state mutation and send is serialized, so deletion cannot race a view/notification.
      for(const row of this.store.due('replies')) {
        if(Date.now()-started>15000)break;
        const result=await this.telegram(row.method,JSON.parse(row.body));
        if(result.ok) {
          if(row.booking_id&&result.result?.message_id)this.store.track(row.user_id,row.recipient,result.result.message_id,row.booking_id);
          this.store.delivered('replies',row.id);
        } else if(row.method==='answerCallbackQuery'&&result.code===400)this.store.delivered('replies',row.id);
        else if(row.method==='deleteMessage'&&[400,403].includes(result.code)) {
          if(cfg.admins.includes(row.recipient))app.send(0,'Клиент отозвал согласие. Часть старых сообщений Telegram не разрешил удалить. Удали доступные ручные копии анкеты.',null,row.recipient);
          this.store.delivered('replies',row.id);
        } else this.store.retry('replies',row,result.retry);
      }
      for(const event of this.store.due('outbox')) {
        if(Date.now()-started>15000)break;
        const b=this.store.booking(event.booking_id);
        if(!b||b.version!==event.version||(event.kind.startsWith('reminder')&&(b.status!=='confirmed'||b.scheduled<=now()))) {this.store.delivered('outbox',event.id);continue;}
        const admin=['new','admin_status'].includes(event.kind);
        let text=bookingText(b,cfg,admin);
        if(event.kind.startsWith('reminder'))text='<b>Напоминание о сеансе</b>\n\n'+text+'\n\nЕсли планы изменились: /my. Подготовка: /prepare.';
        const result=await this.telegram('sendMessage',{chat_id:event.recipient,text,parse_mode:'HTML',...(admin?{reply_markup:markup([['Открыть анкету','admin_view:'+b.id]])}:{})});
        if(result.ok){this.store.track(b.user_id,event.recipient,result.result.message_id,b.id);this.store.delivered('outbox',event.id);}
        else this.store.retry('outbox',event,result.retry);
      }
      await this.planAlarm();
    });
  }
}
