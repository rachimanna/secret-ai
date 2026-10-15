const { chromium } = require('/tmp/claude-0/shot/node_modules/playwright');
const fs = require('fs');
const D = process.argv[2], only = process.argv[3];  // only: "preview" renders a few stills
(async () => {
  const tl = JSON.parse(fs.readFileSync(D + '/timeline.json', 'utf8'));
  const b = await chromium.launch({ executablePath: '/opt/pw-browsers/chromium-1194/chrome-linux/chrome' });
  const p = await b.newPage({ viewport: { width: 1080, height: 1920 } });
  p.on('pageerror', e => console.log('PAGEERROR', e.message));
  await p.goto('file://' + D + '/promo.html'); await p.waitForTimeout(500);
  await p.evaluate(tl => setup(tl), tl);
  fs.mkdirSync(D + '/frames', { recursive: true });
  const fps = 30, n = Math.round(tl.total * fps);
  const list = only ? tl.scenes.map(s => Math.round((s.start + Math.min(1.6, (s.end - s.start) * .7)) * fps)) : [...Array(n).keys()];
  for (const f of list) {
    await p.evaluate(t => render(t), f / fps);
    if (!only) await p.evaluate(() => Promise.all([...document.images].filter(i => !i.complete).map(i => new Promise(r => { i.onload = i.onerror = r; }))));
    await p.screenshot({ path: `${D}/${only ? 'prev' : 'frames'}/${String(f).padStart(5, '0')}.jpg`, type: 'jpeg', quality: 90 });
  }
  await b.close();
  console.log('frames', list.length);
})();
