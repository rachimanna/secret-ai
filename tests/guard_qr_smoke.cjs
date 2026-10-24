const {chromium}=require('playwright'),{spawn}=require('child_process'),path=require('path'),assert=require('assert/strict');
const child=spawn(process.env.SECRET_TEST_PYTHON||'python',['-u','tests/guard_login_server.py','--qr'],{cwd:path.resolve(__dirname,'..'),stdio:['ignore','pipe','pipe']});
child.stderr.on('data',b=>process.stderr.write(b));
const ready=new Promise((resolve,reject)=>{let raw='';const t=setTimeout(()=>reject(Error('Fixture timeout')),20000);child.stdout.on('data',b=>{raw+=b;try{const info=JSON.parse(raw.split('\n')[0]);clearTimeout(t);resolve(info);}catch{}});child.once('exit',c=>{clearTimeout(t);reject(Error('Fixture exited '+c));});});
(async()=>{let browser;try{
 const {url}=await ready;browser=await chromium.launch({headless:true});const page=await browser.newPage(),errors=[];page.on('pageerror',e=>errors.push(e.message));
 await page.goto(url);assert.equal(await page.locator('#phone').isVisible(),false);
 await page.locator('#api-id').fill('123');await page.locator('#api-hash').fill('a'.repeat(32));await page.locator('#secret').fill('fixture-key');
 await page.locator('#setup button[type=submit]').click();await page.locator('#qr-image svg').waitFor({state:'visible'});
 assert((await page.locator('#qr-panel').textContent()).includes('iPhone'));
 assert.equal(await page.locator('#prompt').isVisible(),false,'QR login must not request a phone code');
 await page.waitForFunction(()=>document.getElementById('status').textContent.includes('Готово!'));
 assert.equal(await page.locator('#qr-image svg').count(),0,'QR capability removed after success');assert.deepEqual(errors,[]);
 console.log('QR browser flow passed: no phone/SMS, SVG render, simulated acceptance, encrypted output.');
}finally{await browser?.close();child.kill();}})().catch(e=>{console.error(e);child.kill();process.exitCode=1;});
