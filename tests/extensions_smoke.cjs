const {chromium}=require('playwright');
const {spawn}=require('child_process');
const path=require('path'),fs=require('fs'),assert=require('assert/strict');
const root=path.resolve(__dirname,'..');
const output=process.env.SECRET_EXT_QA || path.join(root,'..','extensions-qa');
fs.mkdirSync(output,{recursive:true});
const child=spawn(process.env.SECRET_TEST_PYTHON || 'python',['-u','tests/extensions_server.py'],{cwd:root,stdio:['ignore','pipe','pipe']});
child.stderr.on('data',chunk=>{const text=chunk.toString();if(!/^127\.0\.0\.1/.test(text))process.stderr.write(text);});
const ready=new Promise((resolve,reject)=>{
 let buffer='';const timer=setTimeout(()=>reject(new Error('Fixture server startup timeout')),30000);
 child.stdout.on('data',chunk=>{buffer+=chunk;const line=buffer.split('\n')[0];try{const info=JSON.parse(line);clearTimeout(timer);resolve(info);}catch{}});
 child.once('exit',code=>{clearTimeout(timer);reject(new Error('Fixture server exited: '+code));});
});
(async()=>{
 let browser;
 try{
  const info=await ready,origin='http://127.0.0.1:'+info.port;
  browser=await chromium.launch({headless:true});
  for(const viewport of [{width:390,height:844},{width:1440,height:1000}]){
   const context=await browser.newContext({viewport,reducedMotion:'reduce'});
   await context.route('https://**/*',r=>r.fulfill({body:''}));
   await context.addInitScript(({initData})=>{window.Telegram={WebApp:{initData,initDataUnsafe:{user:{id:1,language_code:'ru'}},ready(){},expand(){},onEvent(){},isVersionAtLeast(){return true;},setHeaderColor(){},setBackgroundColor(){},setBottomBarColor(){},disableVerticalSwipes(){},BackButton:{show(){},hide(){},onClick(){}}}};},{initData:info.initData});
   const page=await context.newPage(),errors=[];page.on('pageerror',e=>errors.push(e.message));
   await page.goto(origin+'/app');await page.waitForFunction(()=>typeof bootReady!=='undefined'&&bootReady);await page.waitForSelector('#splash',{state:'detached'});
   await page.evaluate(()=>switchTab('apps'));await page.locator('#ext-library').click();await page.locator('#ext-create').click();
   await page.locator('#ext-name').fill('Редактор <script>');
   await page.locator('#ext-description').fill('Сократить вступление');
   await page.locator('#ext-instructions').fill('Сохрани факты. Удали повторы. <img src=x onerror=alert(1)>');
   await page.locator('#ext-save-preview').click();await page.waitForSelector('#ext-install');
   assert.equal(await page.locator('#sheet script, #sheet img[onerror]').count(),0,'plugin text must be escaped');
   await page.locator('#ext-install').click();await page.waitForSelector('#ext-toggle');
   await page.locator('#ext-toggle').click();await page.waitForFunction(()=>document.getElementById('ext-toggle')?.textContent==='Включить');
   await page.reload();await page.waitForFunction(()=>bootReady);await page.waitForSelector('#splash',{state:'detached'});
   await page.evaluate(()=>switchTab('apps'));await page.locator('#ext-library').click();
   const row=page.locator('[data-ext-id]').filter({hasText:'Редактор <script>'}).last();await row.click();
   assert.equal(await page.locator('#ext-toggle').textContent(),'Включить','disabled state persists on server');
   await page.locator('#ext-toggle').click();await page.waitForFunction(()=>document.getElementById('ext-toggle')?.textContent==='Выключить');
   await page.locator('#ext-edit').click();const source=JSON.parse(await page.locator('#ext-edit-source').inputValue());source.skills[0].instructions='Новая инструкция';
   await page.locator('#ext-edit-source').fill(JSON.stringify(source));await page.locator('#ext-edit-preview').click();await page.waitForSelector('#ext-install');
   assert.equal(await page.locator('#ext-install').textContent(),'Заменить установленный плагин');await page.locator('#ext-install').click();await page.waitForSelector('#ext-back');
   const download=page.waitForEvent('download');await page.locator('#ext-export').click();const file=await download;const exported=JSON.parse(fs.readFileSync(await file.path(),'utf8'));assert.equal(exported.skills[0].instructions,'Новая инструкция');
   const foreign=await context.request.post(origin+'/api/extensions/export',{headers:{'X-Telegram-Init-Data':info.otherInitData},data:{id:exported.id}});assert.equal(foreign.status(),400);
   await page.screenshot({path:path.join(output,'skills-'+viewport.width+'.png'),fullPage:true});
   page.once('dialog',d=>d.dismiss());await page.locator('#ext-delete').click();assert.equal(await page.locator('#ext-delete').count(),1);
   page.once('dialog',d=>d.accept());await page.locator('#ext-delete').click();await page.waitForSelector('#ext-import');
   await page.locator('#ext-import').click();await page.locator('#ext-file').setInputFiles({name:'SKILL.md',mimeType:'text/markdown',buffer:Buffer.from('---\nname: sample-'+viewport.width+'\ndescription: Test imported skill\n---\nUse clear language.')});
   await page.waitForSelector('#ext-install');await page.locator('#ext-install').click();await page.waitForSelector('#ext-back');
   await page.locator('#sheet .close').click();await page.locator('#ext-connectors').click();await page.locator('#ext-add-connector').click();
   await page.locator('#ext-label').fill('Docs'+viewport.width);await page.locator('#ext-url').fill('https://example.com/mcp');await page.locator('#ext-token').fill('fixture-secret');await page.locator('#ext-add').click();await page.waitForSelector('[data-ext-check]');
   const toggler=page.locator('[data-ext-toggle]').last();await toggler.click();await page.waitForFunction(()=>document.getElementById('sheet').textContent.includes('Выключен'));
   const apiState=await context.request.post(origin+'/api/state',{headers:{'X-Telegram-Init-Data':info.initData},data:{}});assert(!JSON.stringify(await apiState.json()).includes('fixture-secret'));
   await page.screenshot({path:path.join(output,'connectors-'+viewport.width+'.png'),fullPage:true});
   assert.equal(await page.evaluate(()=>document.documentElement.scrollWidth>innerWidth+1),false,'no horizontal overflow');assert.deepEqual(errors,[]);
   await context.close();
  }
  console.log('Extensions browser → real HTTP → encrypted SQLite flows passed (mobile and desktop)');
 }finally{await browser?.close();child.kill();}
})().catch(e=>{console.error(e);child.kill();process.exitCode=1;});
