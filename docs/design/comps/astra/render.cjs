const { chromium } = require('/home/tnunamak/applications/azizam/web/node_modules/@playwright/test');
const fs=require('node:fs');
const path=require('node:path');
const {pathToFileURL}=require('node:url');
const out=__dirname;
process.env.TMPDIR=path.join(out,'.browser-tmp');fs.mkdirSync(process.env.TMPDIR,{recursive:true});
(async()=>{
 const browser=await chromium.launch({headless:true});
 const report=[];
 for(const surface of ['home','status','library']){
  for(const theme of ['light','dark']){
   const width=surface==='home'?390:1440,height=surface==='home'?844:1000;
   const page=await browser.newPage({viewport:{width,height},deviceScaleFactor:1,colorScheme:theme});
   const errors=[];page.on('pageerror',e=>errors.push(e.message));
   await page.goto(pathToFileURL(path.join(out,surface+'.html')).href+'?theme='+theme);
   await page.evaluate(()=>document.fonts.ready);
   await page.locator('img').evaluateAll(imgs=>Promise.all(imgs.filter(i=>i.getAttribute('src')).map(i=>i.decode())));
   await page.waitForFunction(()=>[...document.querySelectorAll('video')].every(v=>v.preload==='none'||v.readyState>=2),{},{timeout:15000});
   await page.screenshot({path:path.join(out,`${surface}-${theme}-${width}.png`)});
   await page.evaluate(()=>document.documentElement.classList.add('full-capture'));
   await page.screenshot({path:path.join(out,`${surface}-${theme}-${width}-full.png`),fullPage:true});
   const audit=await page.evaluate(()=>{
    function rgb(str){return (str.match(/[\d.]+/g)||[]).map(Number)}
    function lum(rgb){const c=rgb.slice(0,3).map(v=>{v/=255;return v<=.04045?v/12.92:((v+.055)/1.055)**2.4});return .2126*c[0]+.7152*c[1]+.0722*c[2]}
    const rows=[];
    const walker=document.createTreeWalker(document.body,NodeFilter.SHOW_TEXT);
    let node;
    while(node=walker.nextNode()){
     if(!node.textContent.trim())continue;
     const el=node.parentElement;if(['SCRIPT','STYLE','OPTION'].includes(el.tagName)||el.closest('svg'))continue;
     const box=el.getBoundingClientRect();if(!box.width||!box.height||box.y<0)continue;
     if(el.closest('details:not([open])')&&el.tagName!=='SUMMARY')continue;
     const s=getComputedStyle(el);if(s.visibility==='hidden')continue;
     let bg=[255,255,255];for(let a=el;a;a=a.parentElement){const c=rgb(getComputedStyle(a).backgroundColor);if(c.length>=3&&(c.length<4||c[3]===1)){bg=c;break}}
     const fg=rgb(s.color);const f=lum(fg),b=lum(bg),ratio=(Math.max(f,b)+.05)/(Math.min(f,b)+.05);
     const size=parseFloat(s.fontSize),large=size>=24||(size>=18.66&&parseInt(s.fontWeight)>=700);
     rows.push({text:node.textContent.trim().slice(0,85),color:s.color,background:bg.slice(0,3),fontSize:size,ratio:+ratio.toFixed(2),required:large?3:4.5,pass:ratio>=(large?3:4.5)});
    }
    return {text:rows,overflow:document.documentElement.scrollWidth>innerWidth,height:document.documentElement.scrollHeight,images:[...document.images].filter(i=>i.getAttribute('src')).every(i=>i.complete&&i.naturalWidth>0),squareImages:[...document.querySelectorAll('.creation:not(.video-item) img')].every(i=>Math.abs(i.width-i.height)<=1)};
   });
   const interactions={};
   await page.locator('.theme').click();interactions.theme=await page.evaluate(t=>document.documentElement.dataset.theme!==t,theme);
   if(surface==='library'){
    await page.locator('#search').fill('tomatoes');interactions.search=await page.locator('[data-kind]:visible').count()===2;
    await page.locator('#search').fill('');await page.locator('#kind').selectOption('audio');interactions.audioFilter=await page.locator('[data-kind]:visible').count()===5;
    await page.locator('#kind').selectOption('all');await page.locator('.creation>a').first().click();interactions.viewer=await page.locator('#viewer').evaluate(d=>d.open);await page.locator('#viewer button').click();
   }
   report.push({surface,theme,width,height,...audit,errors,interactions});
   console.log(`${surface} ${theme}: height ${audit.height}, ${audit.text.length} text samples, contrast failures ${audit.text.filter(r=>!r.pass).length}, overflow ${audit.overflow}`);
   await page.close();
  }
 }
 await browser.close();fs.writeFileSync(path.join(out,'verification.json'),JSON.stringify(report,null,2));
 if(report.some(r=>r.overflow||!r.images||!r.squareImages||r.errors.length||r.text.some(t=>!t.pass)||Object.values(r.interactions).some(v=>!v)))process.exitCode=1;
})().catch(e=>{console.error(e);process.exitCode=1});
