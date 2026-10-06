import {test} from 'node:test';
import assert from 'node:assert/strict';
import {Miniflare,NoOpLog,convertV4MiniflareOptions} from 'miniflare';
import {mkdtemp,rm} from 'node:fs/promises';
import {tmpdir} from 'node:os';
import {join,resolve} from 'node:path';

test('real Worker runtime: authenticated webhook, durable form, owner access, duplicate delivery and deletion',async()=>{
  const sent=[],directory=await mkdtemp(join(tmpdir(),'chaildy-worker-test-'));
  let counter=0,messageId=100;
  const bindings={BOT_TOKEN:'123456:FAKE_TEST_TOKEN',WEBHOOK_SECRET:'w'.repeat(48),DEPLOY_SECRET:'d'.repeat(48),ADMIN_IDS:'1888165622,1092851573',OPERATOR_NAME:'by:Chaildy',PRIVACY_CONTACT:'@spider_013',MASTER_TELEGRAM:'spider_013',PRIVACY_READY:'true'};
  const mf=new Miniflare(convertV4MiniflareOptions({modules:true,scriptPath:resolve('dist/worker.js'),compatibilityDate:'2026-10-04',log:new NoOpLog(),
    bindings,durableObjects:{BOOKING:{className:'BookingOffice',useSQLite:true}},durableObjectsPersist:directory,
    serviceBindings:{TELEGRAM_TEST:async request=>{const body=await request.json();const method=new URL(request.url).pathname.split('/').at(-1);sent.push({method,...body});return Response.json({ok:true,result:['deleteMessage','answerCallbackQuery'].includes(method)?true:{message_id:++messageId,chat:{id:body.chat_id,type:'private'}}});}}}));
  const admin=(path,method='GET',body)=>mf.dispatchFetch('https://bot.test/_admin/'+path,{method,headers:{'X-Deployment-Secret':bindings.DEPLOY_SECRET,'Content-Type':'application/json'},...(body?{body:JSON.stringify(body)}:{})});
  const webhook=update=>mf.dispatchFetch('https://bot.test/webhook',{method:'POST',headers:{'X-Telegram-Bot-Api-Secret-Token':bindings.WEBHOOK_SECRET,'Content-Type':'application/json'},body:JSON.stringify(update)});
  const message=(text,user=7)=>({update_id:++counter,message:{from:{id:user,is_bot:false},chat:{id:user,type:'private'},text}});
  const callback=(data,user=7)=>({update_id:++counter,callback_query:{id:String(counter),from:{id:user,is_bot:false,username:'example_user'},data,message:{chat:{id:user,type:'private'}}}});
  async function waitFor(predicate){const end=Date.now()+6000;while(Date.now()<end){const found=sent.findLast(predicate);if(found)return found;await new Promise(r=>setTimeout(r,50));}throw Error('Durable delivery timed out');}
  async function button(action){const last=await waitFor(s=>s.chat_id===7&&s.reply_markup?.inline_keyboard?.flat().some(b=>b.callback_data.endsWith(':'+action)));return last.reply_markup.inline_keyboard.flat().find(b=>b.callback_data.endsWith(':'+action)).callback_data;}
  try{
    assert.equal((await mf.dispatchFetch('https://bot.test/webhook',{method:'POST',body:'{}'})).status,403);
    assert.equal((await mf.dispatchFetch('https://bot.test/_admin/status')).status,403);
    assert.equal((await webhook(message('/book'))).status,503);
    const snapshot={sessions:[],bookings:[],outbox:[],sent_messages:[]};
    assert.equal((await admin('import','POST',snapshot)).status,200);
    assert.equal((await admin('import','POST',snapshot)).status,409);
    assert.equal((await admin('activate','POST')).status,200);
    const first=message('/book');assert.equal((await webhook(first)).status,200);assert.equal((await webhook(first)).status,200);
    await waitFor(s=>s.chat_id===7&&s.text?.includes('Согласие на обработку'));
    assert.equal(sent.filter(s=>s.chat_id===7&&s.text?.includes('Согласие на обработку')).length,1);
    await webhook(callback(await button('accept')));await waitFor(s=>s.text?.includes('исполнилось 18'));
    await webhook(callback(await button('adult')));await waitFor(s=>s.text?.includes('1 / 5'));
    for(const [index,text] of ['Аня','Веточка','Предплечье','8 см','Выходные'].entries()){
      await webhook(message(text));await waitFor(s=>s.chat_id===7&&(index<4?s.text?.includes(`${index+2} / 5`):s.text?.includes('ориентир по бюджету')));
    }
    await webhook(callback(await button('skip')));await waitFor(s=>s.text?.includes('до 3 фото'));
    await webhook(callback(await button('next')));await waitFor(s=>s.text?.includes('Проверь заявку'));
    const submitted=callback(await button('submit'));await webhook(submitted);await webhook(submitted);
    const notice=await waitFor(s=>s.chat_id===1092851573&&s.text?.includes('Написать клиенту'));
    const key=notice.reply_markup.inline_keyboard[0][0].callback_data.slice(11);
    assert.equal((await (await admin('status')).json()).counts.bookings,1);
    assert.equal(sent.filter(s=>s.chat_id===1092851573&&s.text?.includes('Написать клиенту')).length,1);
    await webhook(message('/view '+key,8));const rejected=await waitFor(s=>s.chat_id===8);assert.match(rejected.text,/доступна мастеру/);assert.doesNotMatch(rejected.text,/Аня/);
    await webhook(message('/bookings',1092851573));await waitFor(s=>s.chat_id===1092851573&&s.text?.includes('На рассмотрении')&&!s.text.includes('Написать клиенту'));
    await webhook(callback('admin_view:'+key,1092851573));await waitFor(s=>s.chat_id===1092851573&&s.text?.includes('Имя:</b> Аня'));
    await webhook(callback('erase:yes'));await waitFor(s=>s.chat_id===7&&s.text?.includes('Данные удалены'));
    assert.equal((await (await admin('status')).json()).counts.bookings,0);
    assert.ok(sent.some(s=>s.method==='deleteMessage'));
    assert.equal((await admin('deactivate','POST')).status,200);
    assert.equal((await webhook(message('/book'))).status,503);
  }finally{await mf.dispose();await rm(directory,{recursive:true,force:true});}
});
