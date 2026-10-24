const {chromium}=require('playwright'),{spawn}=require('child_process'),path=require('path'),assert=require('assert/strict');
const child=spawn(process.env.SECRET_TEST_PYTHON||'python',['-u','tests/extensions_server.py'],{cwd:path.resolve(__dirname,'..'),stdio:['ignore','pipe','pipe']});
child.stderr.on('data',b=>process.stderr.write(b));
const ready=new Promise((resolve,reject)=>{let raw='';const t=setTimeout(()=>reject(Error('Fixture timeout')),20000);child.stdout.on('data',b=>{raw+=b;try{const info=JSON.parse(raw.split('\n')[0]);clearTimeout(t);resolve(info);}catch{}});child.once('exit',c=>{clearTimeout(t);reject(Error('Fixture exited '+c));});});
(async()=>{let browser;try{
 const info=await ready,origin='http://127.0.0.1:'+info.port;browser=await chromium.launch({headless:true});
 for(const viewport of [{width:390,height:844},{width:1440,height:1000}]){
  const context=await browser.newContext({viewport,reducedMotion:'reduce'});await context.route('https://**/*',r=>r.fulfill({body:''}));
  await context.addInitScript(({initData})=>{window.Telegram={WebApp:{initData,initDataUnsafe:{user:{id:1,language_code:'ru'}},ready(){},expand(){},onEvent(){},isVersionAtLeast(){return true;},setHeaderColor(){},setBackgroundColor(){},setBottomBarColor(){},disableVerticalSwipes(){},BackButton:{show(){},hide(){},onClick(){}}}};},{initData:info.initData});
  const page=await context.newPage(),errors=[];page.on('pageerror',e=>errors.push(e.message));
  await page.goto(origin+'/app');await page.waitForFunction(()=>bootReady);await page.waitForSelector('#splash',{state:'detached'});
  await page.locator('#home-groups [data-group]').click();await page.waitForSelector('#g-guard-toggle');
  assert.equal(await page.locator('#g-guard-toggle').getAttribute('aria-pressed'),'false');
  await page.locator('#g-guard-toggle').click();await page.waitForFunction(()=>document.getElementById('g-guard-toggle')?.getAttribute('aria-pressed')==='true');
  await page.reload();await page.waitForFunction(()=>bootReady);await page.waitForSelector('#splash',{state:'detached'});await page.locator('#home-groups [data-group]').click();
  await page.waitForSelector('#g-guard-toggle');assert.equal(await page.locator('#g-guard-toggle').getAttribute('aria-pressed'),'true','state persists across reload');
  const denied=await context.request.post(origin+'/api/groups/guard',{headers:{'X-Telegram-Init-Data':info.otherInitData},data:{chat_id:-1001234567890,enabled:false,user_id:1}});
  assert.equal(denied.status(),403,'ordinary member cannot forge administrator identity');
  await page.locator('#g-guard-refresh').click();await page.waitForSelector('#g-guard-toggle:not([disabled])');assert.equal(await page.locator('#g-guard-toggle').getAttribute('aria-pressed'),'true');
  await page.locator('#g-guard-toggle').click();await page.waitForFunction(()=>document.getElementById('g-guard-toggle')?.getAttribute('aria-pressed')==='false');
  assert.equal(await page.evaluate(()=>document.documentElement.scrollWidth>innerWidth+1),false);assert.deepEqual(errors,[]);await context.close();
 }
 console.log('Group restoration buttons passed: administrator enable/disable, durable settings, member denied (real HTTP/SQLite).');
}finally{await browser?.close();child.kill();}})().catch(e=>{console.error(e);child.kill();process.exitCode=1;});
