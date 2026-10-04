const { chromium } = require('playwright');
const assert = require('node:assert/strict');
const fs = require('node:fs/promises');
const path = require('node:path');

(async () => {
  const url = process.argv[2] || 'http://127.0.0.1:8765/';
  const output = path.resolve('artifacts');
  await fs.mkdir(output, {recursive:true});
  const browser = await chromium.launch({headless:true, ...(process.platform === 'win32' ? {channel:'msedge'} : {})});
  const results = [];
  try {
    for (const width of [320, 390, 768, 1280, 1920]) {
      const context = await browser.newContext({viewport:{width,height:900}, reducedMotion:'reduce'});
      const page = await context.newPage();
      const errors = [];
      const failed = [];
      const requests = [];
      page.on('pageerror', error => errors.push(error.message));
      page.on('console', message => {if(message.type()==='error') errors.push(message.text());});
      page.on('response', response => {if(response.status()>=400) failed.push(`${response.status()} ${response.url()}`);});
      page.on('request', request => requests.push(request.url()));
      await page.goto(url, {waitUntil:'networkidle'});
      await page.evaluate(() => document.fonts.ready);
      assert.equal(await page.locator('h1').count(), 1);
      assert.equal(await page.locator('#gallery-empty').isVisible(), true);
      assert.equal(await page.locator('#gallery').isVisible(), false);
      const layout = await page.evaluate(() => ({
        viewport:innerWidth, scroll:document.documentElement.scrollWidth,
        body:getComputedStyle(document.body).backgroundColor,
        candles:(() => {const r=document.querySelector('.ritual-art img').getBoundingClientRect();return {width:r.width,height:r.height};})(),
        fonts:document.fonts.status,
        localStorage:localStorage.length, cookies:document.cookie
      }));
      assert.equal(layout.scroll, width, `Horizontal overflow at ${width}`);
      assert.equal(layout.body, 'rgb(16, 13, 25)');
      assert.ok(Math.abs(layout.candles.width/layout.candles.height-600/560)<0.01);
      assert.equal(layout.fonts,'loaded');
      assert.equal(layout.localStorage,0);
      assert.equal(layout.cookies,'');
      assert.ok(requests.every(request => new URL(request).origin === new URL(url).origin), 'External resource request');
      await page.screenshot({path:path.join(output,`site-${width}.png`),fullPage:true});
      await page.getByRole('button',{name:'Давай обсудим',exact:true}).click();
      await assert.equal(await page.locator('dialog').isVisible(),true);
      for (let index=0;index<8;index++) await page.locator('#demo-next').click();
      assert.equal(await page.locator('.chat-bubble').count(),15);
      assert.equal(await page.locator('#demo-next').isDisabled(),true);
      await page.keyboard.press('Escape');
      assert.equal(await page.locator('dialog').isVisible(),false);
      assert.equal(await page.evaluate(() => document.activeElement.textContent.includes('Давай обсудим')),true);
      await page.locator('summary').first().click();
      assert.equal(await page.locator('details').first().getAttribute('open'),'');
      await page.getByRole('link',{name:'Персональные данные',exact:true}).click();
      await page.waitForLoadState('networkidle');
      assert.equal(await page.getByRole('heading',{name:'Персональные данные',exact:true}).isVisible(),true);
      await page.getByRole('link',{name:'← На главную',exact:true}).click();
      await page.waitForLoadState('networkidle');
      assert.equal(await page.locator('h1').isVisible(),true);
      assert.deepEqual(errors,[]);
      assert.deepEqual(failed,[]);
      results.push({width,layout,errors,failed,checks:'layout, SVG aspect ratio, empty gallery, local fonts, all demo steps, Escape, focus, FAQ, privacy navigation'});
      await context.close();
    }
    console.log(JSON.stringify({url,results},null,2));
    await fs.writeFile(path.join(output,'site-checks.json'),JSON.stringify({url,results},null,2));
  } finally {await browser.close();}
})().catch(error => {console.error(error);process.exitCode=1;});
