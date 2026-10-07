// Private browser only. No persistent profile, credentials, shared MCP, or login.
const { chromium } = require('/home/tnunamak/.npm/_npx/e41f203b7505f1fb/node_modules/playwright');
const fs = require('fs');
const path = require('path');
const root=__dirname;
const targets = [
 ['home-assistant','https://demo.home-assistant.io/'],
 ['grafana','https://play.grafana.org/'],
 ['github-status','https://www.githubstatus.com/'],
 ['openai-status','https://status.openai.com/'],
 ['vercel-status','https://www.vercel-status.com/'],
 ['tailscale-status','https://status.tailscale.com/'],
 ['mcp','https://modelcontextprotocol.io/docs/getting-started/intro'],
 ['linear','https://linear.app/'],
 ['tailscale','https://tailscale.com/'],
 ['stripe','https://stripe.com/payments'],
 ['apple-photos','https://www.apple.com/ios/photos/'],
 ['plex','https://www.plex.tv/'],
 ['immich','https://demo.immich.app/'],
 ['open-webui','https://demo.openwebui.com/'],
 ['librechat','https://demo.librechat.ai/'],
 ['raycast','https://www.raycast.com/'],
 ['1password','https://1password.com/'],
 ['plaid','https://plaid.com/']
];
(async()=>{
 const browser=await chromium.launch({headless:true,executablePath:'/home/tnunamak/.cache/ms-playwright/chromium-1243/chrome-linux64/chrome',args:['--disable-dev-shm-usage']});
 const logs=[];
 for (const [id,url] of targets) {
  const ctx=await browser.newContext({viewport:{width:1440,height:900},deviceScaleFactor:1});
  const page=await ctx.newPage(); const record={id,url,date:new Date().toISOString().slice(0,10)};
  try {
   const robots=await ctx.request.get(new URL('/robots.txt',url).href,{timeout:15000});
   record.robots={status:robots.status(),text:(await robots.text()).slice(0,16000)};
   // Conservatively refuse a blanket disallow for generic or Playwright agents.
   const groups=record.robots.text.split(/\n\s*\n/);
   if(robots.ok() && groups.some(g=>/User-agent:\s*(\*|Playwright)\s*\n/i.test(g)&&/^Disallow:\s*\/\s*$/im.test(g))) {record.skipped='robots blanket disallow'; logs.push(record); await ctx.close(); continue;}
   const response=await page.goto(url,{waitUntil:'domcontentloaded',timeout:30000});
   record.status=response?.status(); await page.waitForTimeout(4000);
   record.finalUrl=page.url(); record.title=await page.title();
   record.text=(await page.locator('body').innerText()).slice(0,12000);
   record.images=await page.locator('img').evaluateAll(imgs=>imgs.map(i=>({alt:i.alt,src:i.currentSrc,rect:{x:i.getBoundingClientRect().x,y:i.getBoundingClientRect().y,width:i.getBoundingClientRect().width,height:i.getBoundingClientRect().height}})).filter(i=>i.rect.width>250&&i.rect.height>120));
   await page.screenshot({path:path.join(root,'public',id+'-desktop.png')});
   await page.setViewportSize({width:390,height:844}); await page.waitForTimeout(1000);
   await page.screenshot({path:path.join(root,'public',id+'-mobile.png')});
   console.log(id,record.status,record.title,record.finalUrl);
  } catch(e) {record.error=e.message;console.log(id,'FAILED',e.message.slice(0,150));}
  logs.push(record); fs.writeFileSync(path.join(root,'capture-log.json'),JSON.stringify(logs,null,2)); await ctx.close();
 }
 await browser.close();
})();
