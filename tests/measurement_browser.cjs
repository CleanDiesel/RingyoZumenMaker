const {chromium} = require('playwright');
const {PDFDocument} = require('pdf-lib');
const fs = require('node:fs');
const path = require('node:path');
const assert = require('node:assert/strict');
const {pathToFileURL} = require('node:url');
(async () => {
  const root = path.resolve(process.argv[2]);
  const browser = await chromium.launch({headless:true, executablePath:'C:/Program Files (x86)/Microsoft/Edge/Application/msedge.exe'});
  try {
    const page = await browser.newPage({viewport:{width:1750,height:1250}});
    let errors=[]; page.on('pageerror', e=>errors.push(String(e)));
    for (const paper of ['A4','A3']) {
      await page.goto(pathToFileURL(path.join(root, paper==='A3'?'A3/index.html':'index.html')).href);
      await page.evaluate(()=>document.fonts.ready);
      assert.equal(await page.locator('#zumen_type option[value]:not([value=""])').count(), 3);
      assert.equal(await page.locator('#zumen_type').inputValue(), 'land');
      await page.screenshot({path:path.join(root,`${paper}-preview.png`), fullPage:true});
      const first = await page.locator('#zumen_type').inputValue();
      await page.click('#drawing_next');
      assert.notEqual(await page.locator('#zumen_type').inputValue(), first);
      await page.click('#drawing_previous');
      assert.equal(await page.locator('#zumen_type').inputValue(), first);
      const pdf = await PDFDocument.load(await page.pdf({preferCSSPageSize:true,printBackground:true}));
      assert.equal(pdf.getPageCount(),1);
      assert(Math.abs(pdf.getPage(0).getWidth() - (paper==='A3'?420:297)*72/25.4)<2);
      // A continuation table must become a separate printed page and must not
      // repeat the map. Existing interactive selection must survive printing.
      await page.evaluate(()=>{
        const table=document.querySelector('[data-xy-table][data-option-panel="land"]');
        const clone=table.cloneNode(true); clone.dataset.xyTable='2'; clone.hidden=true;
        table.after(clone);
      });
      // The closure enumerates source tables once on load, so reload the modified
      // exported document to exercise a genuine >24-point result.
      const modified = await page.content();
      const longFile=path.join(root, paper==='A3'?'A3':'', 'long-table.html');
      fs.writeFileSync(longFile, modified);
      await page.goto(pathToFileURL(longFile).href);
      await page.check('#print_all');
      const saved=await page.locator('#zumen_type').inputValue();
      const allBuffer=await page.pdf({preferCSSPageSize:true,printBackground:true});
      fs.writeFileSync(path.join(root, `${paper}-all.pdf`), allBuffer);
      const all=await PDFDocument.load(allBuffer);
      assert.equal(all.getPageCount(),4, `${paper}: diagrams + coordinate-table continuation`);
      assert.equal(await page.locator('#zumen_type').inputValue(),saved);
      assert.equal(errors.length,0, errors.join('\n'));
      console.log(paper, 'navigation, single-page size, 4-page batch print and restored state: PASS');
    }
  } finally {await browser.close();}
})().catch(e=>{console.error(e); process.exitCode=1;});
