const {chromium}=require('playwright'),{spawn}=require('child_process'),path=require('path'),assert=require('assert/strict');
const child=spawn(process.env.SECRET_TEST_PYTHON||'python',['-u','tests/guard_login_server.py'],{cwd:path.resolve(__dirname,'..'),stdio:['ignore','pipe','pipe']});
child.stderr.on('data',b=>process.stderr.write(b));
const ready=new Promise((resolve,reject)=>{let raw='';const timeout=setTimeout(()=>reject(Error('Server startup timeout')),20000);child.stdout.on('data',b=>{raw+=b;try{const r=JSON.parse(raw.split('\n')[0]);clearTimeout(timeout);resolve(r);}catch{}});child.once('exit',c=>{clearTimeout(timeout);reject(Error('Fixture exited '+c));});});
(async()=>{let browser;try{
 const {url}=await ready;browser=await chromium.launch({headless:true});const page=await browser.newPage({viewport:{width:1000,height:1000}}),errors=[];
 page.on('pageerror',e=>errors.push(e.message));
 // Fake clipboard: never read or overwrite the user's actual clipboard.
 await page.addInitScript(()=>Object.defineProperty(navigator,'clipboard',{value:{readText:async()=> 'a'.repeat(32)}}));
 await page.goto(url);await page.locator('#api-id').fill('123');await page.locator('[data-paste="api-hash"]').click();
 assert.equal(await page.locator('#api-hash').inputValue(),'a'.repeat(32));assert.notEqual(await page.locator('#api-hash').getAttribute('type'),'password');
 await page.locator('#phone').fill('+79991234567');await page.locator('#secret').fill('fixture-key');
 if(process.env.SECRET_GUARD_SCREENSHOT)await page.screenshot({path:process.env.SECRET_GUARD_SCREENSHOT});
 await page.locator('#setup button[type=submit]').click();await page.waitForFunction(()=>document.getElementById('question').textContent==='Код входа из Telegram');
 await page.locator('#answer').fill('11111');await page.locator('#prompt button[type=submit]').click();
 await page.waitForFunction(()=>document.getElementById('question').textContent==='Пароль двухэтапной проверки');
 await page.locator('#answer').fill('fixture-password');await page.locator('#prompt button[type=submit]').click();
 await page.waitForFunction(()=>document.getElementById('status').textContent.includes('Готово!'));
 assert.equal(await page.locator('#api-hash').inputValue(),'');assert.equal(await page.locator('#answer').inputValue(),'');assert.deepEqual(errors,[]);
 console.log('Visible login wizard passed: Paste button, visible API hash, code and password steps, encrypted output (fake Telegram).');
}finally{await browser?.close();child.kill();}})().catch(e=>{console.error(e);child.kill();process.exitCode=1;});
