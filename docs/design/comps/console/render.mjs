import { createRequire } from 'module';
const require = createRequire('/home/tnunamak/applications/azizam/web/package.json');
const { chromium } = require('@playwright/test');
const dir = process.env.HOME + '/applications/azizam-wt-design/docs/design/comps/console';
const jobs = [['home',390,844],['status',1440,1000],['library',1440,1000]];
const browser = await chromium.launch();
for (const [name,w,h] of jobs) for (const theme of ['light','dark']) {
  const ctx = await browser.newContext({ viewport:{width:w,height:h}, colorScheme:theme, deviceScaleFactor:1 });
  const p = await ctx.newPage();
  await p.goto(`file://${dir}/${name}.html`, {waitUntil:'domcontentloaded'}); await p.waitForLoadState('load',{timeout:15000}).catch(()=>console.log('load timeout',name));
  await p.evaluate(() => document.fonts.ready);
  await p.waitForTimeout(300);
  const sw = await p.evaluate(() => [document.documentElement.scrollWidth, document.documentElement.clientWidth]);
  if (sw[0] > sw[1]) console.log('HORIZONTAL OVERFLOW', name, theme, sw);
  const fonts = await p.evaluate(() => [...document.fonts].filter(f=>f.status==='loaded').map(f=>f.family+' '+f.weight).join(', '));
  if (theme==='dark') console.log(name, 'fonts:', fonts);
  await p.screenshot({ path:`${dir}/${name}-${theme}-${w}.png` });
  await p.evaluate(() => document.body.classList.add('capture-full'));
  await p.screenshot({ path:`${dir}/${name}-${theme}-${w}-full.png`, fullPage:true });
  await ctx.close();
}
await browser.close();
