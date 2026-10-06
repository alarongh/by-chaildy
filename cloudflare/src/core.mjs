import content from './content.json' with {type:'json'};
import {now} from './store.mjs';
export const escape = value => String(value).replaceAll('&','&amp;').replaceAll('<','&lt;').replaceAll('>','&gt;').replaceAll('"','&quot;').replaceAll("'",'&#x27;');
const fields=['name','idea','placement','size','availability','budget','refs'];
export const labels={pending:'На рассмотрении',confirmed:'Сеанс подтверждён',declined:'Мастер не принял заявку',cancelled:'Заявка отменена',completed:'Сеанс завершён'};
export async function settings(env) {
  const cfg={admins:String(env.ADMIN_IDS||'').split(',').filter(Boolean).map(Number),master:String(env.MASTER_TELEGRAM||'').replace(/^@/,''),
    operator:env.OPERATOR_NAME||'',address:env.OPERATOR_ADDRESS||'',contact:env.PRIVACY_CONTACT||'',city:env.CITY||'',timezone:env.TIMEZONE||'Europe/Moscow',
    days:Number(env.RETENTION_DAYS||90),privacyReady:env.PRIVACY_READY==='true',testMode:env.TEST_MODE==='true'};
  if(cfg.admins.some(x=>!Number.isSafeInteger(x)||x<=0)||!Number.isInteger(cfg.days)||cfg.days<1||cfg.days>365)throw Error('Invalid configuration');
  new Intl.DateTimeFormat('ru-RU',{timeZone:cfg.timezone});
  cfg.canCollect=!cfg.testMode&&cfg.privacyReady&&cfg.admins.length>0&&!!cfg.operator.trim()&&!!cfg.contact.trim();
  cfg.allowed=user=>cfg.testMode?cfg.admins.includes(user):cfg.canCollect;
  cfg.consent=content.consent;
  if(!cfg.address.trim())cfg.consent=cfg.consent.replace('Адрес для обращений: {{OPERATOR_ADDRESS}}\n','');
  for(const [key,value] of Object.entries({OPERATOR_NAME:cfg.operator,OPERATOR_ADDRESS:cfg.address,PRIVACY_CONTACT:cfg.contact,RETENTION_DAYS:cfg.days}))cfg.consent=cfg.consent.replaceAll('{{'+key+'}}',String(value));
  if(cfg.testMode)cfg.consent='Тест анкеты by:Chaildy. Доступ открыт только администраторам. Вводи вымышленные ответы. Telegram ID, ответы и нажатие согласия сохраняются в Cloudflare и передаются тестовым администраторам через Telegram. Реального сеанса этот тест не создаёт. Удаление: /delete. Соглашаюсь на обработку данных для проверки бота.';
  cfg.consentHash=Array.from(new Uint8Array(await crypto.subtle.digest('SHA-256',new TextEncoder().encode(cfg.consent)))).map(b=>b.toString(16).padStart(2,'0')).join('');
  return cfg;
}
export function summary(data) {
  return [['Имя','name'],['Идея','idea'],['Место','placement'],['Размер','size'],['Удобные дни','availability'],['Бюджет','budget']]
    .map(([label,key])=>`<b>${label}:</b> ${escape(data[key]??'Не указан')}`).join('\n')+`\n<b>Референсы:</b> ${(data.refs||[]).length} фото`;
}
export function markup(buttons) {return buttons?.length?{inline_keyboard:buttons.map(([text,callback_data])=>[{text,callback_data}])}:undefined;}
export function bookingText(b,cfg,admin=false) {
  let text=`<b>Заявка #${b.id}</b>\n${labels[b.status]}\n`;
  if(b.data.test)text='<b>ТЕСТ · реальной записи нет</b>\n\n'+text;
  if(b.scheduled)text+='\nДата: '+new Intl.DateTimeFormat('ru-RU',{timeZone:cfg.timezone,dateStyle:'short',timeStyle:'short'}).format(new Date(b.scheduled*1000))+` (${escape(cfg.timezone)})\nПродолжительность: ${b.duration} мин.\n`;
  if(admin) {
    text+=`\n<a href="tg://user?id=${b.user_id}">Написать клиенту</a> · ID ${b.user_id}\n`;
    if(/^[A-Za-z0-9_]{5,32}$/.test(b.data.username||''))text+='@'+escape(b.data.username)+'\n';
  }
  text+='\n'+summary(b.data);
  if(b.status==='pending')text+='\n\nДата пока не подтверждена.';
  return text;
}
export class Flow {
  constructor(store,cfg){this.store=store;this.cfg=cfg;}
  begin(user) {
    if(!this.cfg.allowed(user))return {text:'Запись скоро откроется. Пока анкету заполнить нельзя.'};
    if(this.store.active(user))return {text:'У тебя уже есть активная заявка. Посмотреть её: /my. Если нужна новая, сначала отмени текущую.'};
    return this.prompt(this.store.session(user)||this.store.saveSession(user,'consent',{}));
  }
  prompt(s) {
    const btn=(label,action)=>[label,`f:${s.nonce}:${s.step}:${action}`];
    if(s.step==='consent')return {text:escape(this.cfg.consent),buttons:[btn('Согласен / согласна','accept'),btn('Не согласен / не согласна','refuse')]};
    if(s.step==='age')return {text:'Запись здесь доступна с 18 лет. Тебе уже исполнилось 18?',buttons:[btn('Да, мне 18+','adult'),btn('Мне нет 18','minor')]};
    if(s.step==='review')return {text:'<b>Проверь заявку</b>\n\n'+summary(s.data)+'\n\nМастер обсудит детали и подтвердит время. Отправка анкеты не бронирует сеанс.',buttons:[btn('Отправить мастеру','submit'),
      ...[['имя','name'],['идею','idea'],['место','placement'],['размер','size'],['дни','availability'],['бюджет','budget'],['фото','refs']].map(([label,key])=>btn('Изменить: '+label,key)),btn('Удалить черновик','abort')]};
    return {text:content.prompts[s.step],buttons:[...(s.step==='refs'?[btn('Готово / пропустить','next')]:s.step==='budget'?[btn('Пропустить','skip')]:[]),btn('Назад','back'),btn('Удалить черновик','abort')]};
  }
  advance(step,data){if(data.editing){delete data.editing;return 'review';}return step==='refs'?'review':fields[fields.indexOf(step)+1];}
  action(user,callback,username=null) {
    if(!this.cfg.allowed(user))return {text:'Анкеты временно недоступны. Данные можно удалить командой /delete.'};
    const s=this.store.session(user),parts=callback.split(':');
    if(!s||parts.length!==4||parts[1]!==s.nonce||parts[2]!==s.step)return {text:'Эта кнопка уже устарела. Открой текущий шаг: /book.'};
    let {step,data}=s;const action=parts[3];
    if(['abort','refuse','minor'].includes(action)){this.store.dropSession(user);return {text:action==='minor'?'В этом боте запись только с 18 лет. Черновик удалён.':'Черновик удалён. Запись не создана.'};}
    if(step==='consent'&&action==='accept') {
      data.consent={version:content.consentVersion,accepted_at:now(),text:this.cfg.consent,sha256:this.cfg.consentHash};data.username=username;data.test=this.cfg.testMode;step='age';
    } else if(step==='age'&&action==='adult'){data.adult=true;step='name';}
    else if(step==='review'&&action==='submit') {
      const b=this.store.submit(user,this.cfg.admins);
      return {text:`Заявка <b>#${b.id}</b> сохранена. Мастер получит её и свяжется с тобой здесь.\n\nДата ещё не подтверждена. Статус и отмена: /my.`};
    } else if(step==='review'&&fields.includes(action)){data.editing=true;if(action==='refs')data.refs=[];step=action;}
    else if(action==='back'&&fields.includes(step)){if(data.editing){delete data.editing;step='review';}else step=fields[Math.max(0,fields.indexOf(step)-1)];}
    else if((step==='budget'&&action==='skip')||(step==='refs'&&action==='next')){if(step==='budget')data.budget='Не указан';step=this.advance(step,data);}
    else return {text:'Открой текущий шаг: /book.'};
    return this.prompt(this.store.saveSession(user,step,data));
  }
  answer(user,text=null,photo=null) {
    if(!this.cfg.allowed(user))return {text:'Анкеты временно недоступны. Данные можно удалить командой /delete.'};
    const s=this.store.session(user);
    if(!s)return {text:'Чтобы оставить заявку, нажми /book. Вопросы мастеру: /contact.'};
    const {step,data}=s;
    if(['consent','age','review'].includes(step))return this.prompt(s);
    if(step==='refs') {
      if(!photo)return {...this.prompt(s),text:'Пришли фото или нажми «Готово / пропустить».'};
      const refs=data.refs||=[];
      if(refs.length>=3)return {...this.prompt(s),text:'Уже добавлено 3 фото. Нажми «Готово / пропустить».'};
      if(!refs.includes(photo))refs.push(photo);
      const result=this.prompt(this.store.saveSession(user,step,data));
      return {...result,text:`Добавлено фото: ${refs.length} / 3.`};
    }
    if(!text?.trim())return {...this.prompt(s),text:'Ответь текстом, пожалуйста.'};
    text=text.trim();const limit=step==='name'?60:step==='idea'?900:240;
    if(Array.from(text).length>limit)return {...this.prompt(s),text:`Нужно чуть короче: до ${limit} символов.`};
    data[step]=text;
    return this.prompt(this.store.saveSession(user,this.advance(step,data),data));
  }
}
export const adminHelp='<b>Управление заявками</b>\n/bookings — последние 20 активных заявок\n/view ID — анкета и фото\n/confirm ID ГГГГ-ММ-ДД ЧЧ:ММ МИНУТЫ — подтвердить или перенести сеанс\n/decline ID — отклонить\n/cancel_booking ID — отменить\n/done ID — завершить\n\nПеред подтверждением согласуй с клиентом дату, стоимость и условия.';
export function parseTime(date,time,timezone) {
  if(!/^\d{4}-\d{2}-\d{2}$/.test(date)||!/^\d{2}:\d{2}$/.test(time))throw Error('Формат: /confirm ID ГГГГ-ММ-ДД ЧЧ:ММ МИНУТЫ');
  const target=Date.parse(date+'T'+time+':00Z');
  if(!Number.isFinite(target))throw Error('Неверная дата.');
  const parts=ms=>Object.fromEntries(new Intl.DateTimeFormat('en-CA',{timeZone:timezone,year:'numeric',month:'2-digit',day:'2-digit',hour:'2-digit',minute:'2-digit',second:'2-digit',hourCycle:'h23'}).formatToParts(new Date(ms)).map(x=>[x.type,x.value]));
  let result=target;
  for(let i=0;i<3;i++){const p=parts(result);const wall=Date.parse(`${p.year}-${p.month}-${p.day}T${p.hour}:${p.minute}:${p.second}Z`);result+=target-wall;}
  const p=parts(result);
  if(`${p.year}-${p.month}-${p.day}`!==date||`${p.hour}:${p.minute}`!==time)throw Error('Неверная дата или время.');
  return result/1000;
}
export class App {
  constructor(store,cfg){this.store=store;this.cfg=cfg;this.flow=new Flow(store,cfg);}
  send(user,text,buttons=null,recipient=user,key=null){this.store.reply(user,recipient,'sendMessage',{chat_id:recipient,text,parse_mode:'HTML',...(markup(buttons)?{reply_markup:markup(buttons)}:{})},key);}
  reply(user,reply){this.send(user,reply.text,reply.buttons);}
  my(user){const b=this.store.active(user);if(!b)return this.send(user,'Активной заявки пока нет. Начать: /book.');this.send(user,bookingText(b,this.cfg),[['Отменить заявку','cancel:'+b.id]],user,b.id);}
  view(user,key){const b=this.store.booking(key);if(!b)return this.send(user,'Заявка не найдена.');this.send(b.user_id,bookingText(b,this.cfg,true),null,user,b.id);for(const photo of b.data.refs||[])this.store.reply(b.user_id,user,'sendPhoto',{chat_id:user,photo},b.id);}
  cleanupCopies(copies){for(const c of copies)this.store.reply(0,c.chat_id,'deleteMessage',{chat_id:c.chat_id,message_id:c.message_id});}
  handle(update) {
    const m=update.message,q=update.callback_query,actor=q?.from||m?.from,chat=q?.message?.chat||m?.chat;
    if(!actor||actor.is_bot||chat?.type!=='private'||!Number.isSafeInteger(actor.id))return;
    const user=actor.id,isAdmin=this.cfg.admins.includes(user);
    if(q){this.store.reply(user,user,'answerCallbackQuery',{callback_query_id:q.id});return this.callback(user,q.data||'',actor.username,isAdmin);}
    const command=(m.text||'').split(/\s/)[0].split('@')[0];
    if(command==='/start') {
      if(this.cfg.testMode)this.send(user,'Бот проходит тестирование. Анкета доступна только администраторам. Вводи вымышленные ответы.');
      this.send(user,'<b>by:Chaildy</b>\n\nОставь короткую заявку, чтобы обсудить тату с мастером. Дата и условия сеанса согласуются отдельно.\n\n/book — оставить заявку\n/my — статус и отмена\n/faq — вопросы\n/contact — мастер\n/privacy — обработка данных\n/delete — удалить данные',[['Оставить заявку','book'],['Моя заявка','my'],['Вопросы и ответы','faq']]);
      if(isAdmin)this.send(user,adminHelp);return;
    }
    if(command==='/id')return this.send(user,`Твой Telegram ID: <code>${user}</code>`);
    if(command==='/book')return this.reply(user,this.flow.begin(user));
    if(command==='/my')return this.my(user);
    if(command==='/cancel'){this.store.dropSession(user);return this.send(user,'Черновик удалён. Отмена отправленной заявки: /my.');}
    if(command==='/privacy')return this.send(user,escape(this.cfg.consent));
    if(command==='/faq')return this.send(user,escape(content.faq));
    if(command==='/prepare')return this.send(user,escape(content.prepare));
    if(command==='/contact')return this.send(user,escape((this.cfg.master?'Мастер: @'+this.cfg.master:'Контакт ещё не настроен.')+(this.cfg.city?'\nГород: '+this.cfg.city:'')));
    if(command==='/help')return this.send(user,'Начать: /book · Заявка: /my · Удаление: /delete · Вопросы: /faq · Мастер: /contact');
    if(command==='/delete')return this.send(user,'Удалить данные из базы и отозвать согласие? Активная запись будет отменена. Бот попробует удалить свои уведомления мастеру; старые сообщения и ручные копии могут потребовать обращения к мастеру.',[['Да, удалить и отменить запись','erase:yes'],['Оставить данные','erase:no']]);
    if(['/admin','/bookings','/view','/confirm','/decline','/cancel_booking','/done'].includes(command)) {
      if(!isAdmin)return this.send(user,'Эта команда доступна мастеру.');
      const parts=m.text.split(/\s+/);
      if(command==='/admin')return this.send(user,adminHelp);
      if(command==='/bookings'){const rows=this.store.list();return this.send(user,rows.length?rows.map(b=>`<code>${b.id}</code> · ${labels[b.status]} · ${escape(b.data.name)}`).join('\n'):'Активных заявок нет.',rows.map(b=>['Открыть #'+b.id,'admin_view:'+b.id]));}
      if(parts.length<2)return this.send(user,adminHelp);
      if(command==='/view')return this.view(user,parts[1]);
      try{
        if(command==='/confirm'){if(parts.length!==5)throw Error('Формат: /confirm ID ГГГГ-ММ-ДД ЧЧ:ММ МИНУТЫ');this.store.transition(parts[1],'confirmed',this.cfg.admins,parseTime(parts[2],parts[3],this.cfg.timezone),Number(parts[4]));}
        else this.store.transition(parts[1],{'/decline':'declined','/cancel_booking':'cancelled','/done':'completed'}[command],this.cfg.admins);
        return this.send(user,'Изменение сохранено. Уведомления поставлены в очередь.');
      }catch(error){return this.send(user,escape(error.message));}
    }
    return this.reply(user,this.flow.answer(user,m.text,m.photo?.at(-1)?.file_id));
  }
  callback(user,data,username,isAdmin) {
    if(data.startsWith('admin_view:'))return isAdmin?this.view(user,data.slice(11)):this.send(user,'Эта команда доступна мастеру.');
    if(data==='book')return this.reply(user,this.flow.begin(user));
    if(data==='my')return this.my(user);
    if(data==='faq')return this.send(user,escape(content.faq));
    if(data==='erase:no')return this.send(user,'Данные оставлены.');
    if(data==='erase:yes') {
      const active=this.store.active(user);this.cleanupCopies(this.store.erase(user));
      if(active)for(const admin of this.cfg.admins)this.send(0,`Заявка #${active.id} отменена: клиент отозвал согласие и удалил данные. Удали ручные копии анкеты, если сохранял их.`,null,admin);
      return this.send(user,'Данные удалены из базы, согласие отозвано, активная запись отменена. Для удаления ручных копий обратись к мастеру. Историю чата можно удалить в Telegram.');
    }
    if(data.startsWith('cancel:')||data.startsWith('cancel_yes:')) {
      const b=this.store.booking(data.split(':')[1]);
      if(!b||b.user_id!==user||!['pending','confirmed'].includes(b.status))return this.send(user,'Активная заявка не найдена.');
      if(data.startsWith('cancel_yes:')){this.store.transition(b.id,'cancelled',this.cfg.admins);return this.send(user,'Заявка отменена. Мастер получит уведомление. Удаление данных: /delete.');}
      return this.send(user,'Точно отменить заявку и согласованный сеанс?',[['Да, отменить','cancel_yes:'+b.id],['Оставить заявку','my']]);
    }
    if(data.startsWith('f:'))return this.reply(user,this.flow.action(user,data,username));
  }
}
