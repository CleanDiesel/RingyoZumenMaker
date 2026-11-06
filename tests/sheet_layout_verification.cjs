// Exercise the generated HTML in a real browser, including print-to-PDF.
const fs = require('fs');
const path = require('path');
const {pathToFileURL} = require('url');
const {execFileSync} = require('child_process');
const {chromium} = require(process.env.PLAYWRIGHT_MODULE);
const root = path.resolve(__dirname, '..');
const output = process.env.RINGYO_PDF_QA_ROOT || fs.readFileSync(path.join(root, 'test/pdf-geometry-latest.txt'), 'utf8').trim();
function check(condition, message) { if (!condition) throw Error(message); }
function pdfInfo(file) { return execFileSync(process.env.PDFINFO_EXE, [file], {encoding:'utf8'}); }
async function ready(page, slug) {
  const folder = path.join(output, slug);
  for (const name of ['sheet.js','index.css']) fs.copyFileSync(path.join(root,'html_shinsoku/asset',name),path.join(folder,'asset',name));
  await page.goto(pathToFileURL(path.join(folder,'index.html')).href);
  await page.waitForFunction(() => window.ringyoSheetReady && document.fonts.status === 'loaded' &&
    [...document.querySelectorAll('.main_map')].every(image => image.complete && image.naturalWidth));
  await page.waitForTimeout(150);
}
function printPages() {
  window.dispatchEvent(new Event('beforeprint'));
  const kinds = [...document.querySelectorAll('#print_pages > .print-page')].map(page => page.dataset.pageType);
  const count = document.querySelectorAll('#print_pages #calc').length;
  const tables = [...document.querySelectorAll('#print_pages [data-xy-table]:not([hidden])')].map(table => table.id);
  const rows = [...document.querySelectorAll('#print_pages [data-xy-table]:not([hidden]) tbody')]
    .reduce((total, body) => total + body.rows.length, 0);
  window.dispatchEvent(new Event('afterprint'));
  return {kinds,count,tables,rows};
}
(async () => {
  const browser = await chromium.launch({executablePath:process.env.EDGE_EXE,headless:true,args:['--allow-file-access-from-files']});
  const page = await browser.newPage({viewport:{width:1400,height:1100},deviceScaleFactor:1});
  const errors = [];
  page.on('pageerror', error => errors.push(error.message));
  await ready(page,'donut');
  let state = await page.evaluate(() => ({mode:document.getElementById('calc').dataset.pageMode,
    count:+document.getElementById('main_container').dataset.pageCount}));
  check(state.mode === 'separate' && state.count === 2, 'A large map must move calculations to page two: '+JSON.stringify(state));
  await page.screenshot({path:path.join(output,'drawing-page.png')});
  await page.locator('#xy_next').click();
  check(await page.locator('#main_container').evaluate(element=>element.hidden), 'Page two must show the calculation sheet');
  check(await page.locator('.calculation-sheet').evaluate(element=>!element.hidden), 'The calculation sheet must be visible');
  await page.screenshot({path:path.join(output,'calculation-page.png')});
  const printed = await page.evaluate(printPages);
  check(printed.kinds.join(',') === 'drawing,calculation' && printed.count === 1,'Print must include calculations exactly once');
  const layoutPdf=path.join(output,'layout-donut-A4.pdf');
  await page.pdf({path:layoutPdf,preferCSSPageSize:true,printBackground:true});
  check(/Pages:\s+2/.test(pdfInfo(layoutPdf)), 'The actual printed PDF must contain exactly two pages');
  console.log('A4 COLLISION DETECTION, PAGE NAVIGATION AND TWO-PAGE PDF OK');

  await ready(page,'narrow-polygon');
  state = await page.evaluate(() => ({mode:document.getElementById('calc').dataset.pageMode,
    count:+document.getElementById('main_container').dataset.pageCount}));
  check(state.mode === 'inline' && state.count === 1, 'A small map must keep calculations on page one: '+JSON.stringify(state));
  const inlinePdf=path.join(output,'layout-inline-A4.pdf');
  await page.pdf({path:inlinePdf,preferCSSPageSize:true,printBackground:true});
  check(/Pages:\s+1/.test(pdfInfo(inlinePdf)), 'An inline calculation must print as one page');
  console.log('NON-OVERLAPPING CALCULATION REMAINS ON PAGE ONE OK');

  await ready(page,'circle-96-edges');
  const circlePages = await page.evaluate(printPages);
  check(circlePages.kinds[0] === 'drawing' && circlePages.kinds.filter(kind=>kind==='coordinates').length===3,
    'All coordinate continuation pages must survive pagination');
  check(circlePages.count===1,'Calculation must not repeat on coordinate continuation pages');
  console.log('COORDINATE CONTINUATIONS AND SINGLE CALCULATION PRINT OK');

  // Move calculations while keeping all 96 coordinates. Page two must share
  // its left-hand coordinate continuation with right-hand calculations.
  await page.evaluate(() => {
    document.querySelector('.main_map').dataset.contentBounds='[3,3,147,147]';
    window.dispatchEvent(new Event('resize'));
  });
  await page.waitForTimeout(150);
  const shared = await page.evaluate(printPages);
  check(shared.kinds.join(',')==='drawing,coordinates-calculation,coordinates,coordinates',
    'Calculations must share the next coordinate page instead of adding a fifth page');
  check(shared.count===1 && shared.rows===96 && new Set(shared.tables).size===4,
    'All coordinates must appear once, and calculation exactly once');
  await page.locator('#xy_next').click();
  const sharedBounds=await page.locator('.coordinates-calculation-sheet').evaluate(sheet=>({
    visible:!sheet.hidden, rows:sheet.querySelector('[data-xy-table]:not([hidden]) tbody').rows.length,
    left:sheet.querySelector('#xy_container').getBoundingClientRect().right,
    right:sheet.querySelector('.calculation-content').getBoundingClientRect().left,
    map:!!sheet.querySelector('.main_map'),
    fit:sheet.querySelector('#calc').getBoundingClientRect().height<=sheet.querySelector('.calculation-content').getBoundingClientRect().height+1
  }));
  check(sharedBounds.visible && sharedBounds.rows===24 && sharedBounds.right>sharedBounds.left && !sharedBounds.map && sharedBounds.fit,
    'Screen page two must show XY left/calculation right without a map or overlap');
  await page.screenshot({path:path.join(output,'shared-xy-calculation.png')});
  const sharedPdf=path.join(output,'layout-shared-XY-A4.pdf');
  await page.pdf({path:sharedPdf,preferCSSPageSize:true,printBackground:true});
  check(/Pages:\s+4/.test(pdfInfo(sharedPdf)),'96 points plus moved calculations must still print as four pages');
  console.log('XY CONTINUATION AND MOVED CALCULATION SHARE PAGE TWO WITHOUT DUPLICATION OK');

  // Simulate 25 coordinate rows: 24 on page one and a single remaining row.
  await page.evaluate(() => {
    const tables=[...document.querySelectorAll('#main_container [data-xy-table]')];
    tables.slice(2).forEach(table=>{table.dataset.optionPanel='unused-test-panel';});
    [...tables[1].tBodies[0].rows].slice(1).forEach(row=>row.remove());
    window.dispatchEvent(new Event('resize'));
  });
  await page.waitForTimeout(100);
  const remainder=await page.evaluate(printPages);
  check(remainder.kinds.join(',')==='drawing,coordinates-calculation' && remainder.rows===25 && remainder.count===1,
    'One remaining XY row and calculations must fit together on page two');
  const remainderPdf=path.join(output,'layout-25-XY-A4.pdf');
  await page.pdf({path:remainderPdf,preferCSSPageSize:true,printBackground:true});
  check(/Pages:\s+2/.test(pdfInfo(remainderPdf)),'25 coordinates must not create a separate third calculation page');
  await page.evaluate(() => {
    document.body.dataset.paper='A3';
    document.getElementById('main_container').style.setProperty('--paper-factor',420/297);
    const style=document.createElement('style');style.textContent='@page { size: A3 landscape; margin: 0; }';document.head.append(style);
    window.dispatchEvent(new Event('resize'));
  });
  await page.waitForTimeout(100);
  const sharedA3Pdf=path.join(output,'layout-25-XY-A3.pdf');
  await page.pdf({path:sharedA3Pdf,preferCSSPageSize:true,printBackground:true});
  check(/Pages:\s+2/.test(pdfInfo(sharedA3Pdf)),'A3 must also share XY remainder/calculation on page two');
  console.log('SINGLE XY REMAINDER ROW SHARES CALCULATION PAGE IN A4 AND A3 OK');

  await ready(page,'donut');
  await page.evaluate(() => {
    document.body.dataset.paper='A3';
    document.getElementById('main_container').style.setProperty('--paper-factor',Math.SQRT2);
    const style=document.createElement('style');style.textContent='@page { size: A3 landscape; margin: 0; }';document.head.append(style);
    window.dispatchEvent(new Event('resize'));
  });
  await page.waitForTimeout(100);
  const a3Pdf=path.join(output,'layout-donut-A3.pdf');
  await page.pdf({path:a3Pdf,preferCSSPageSize:true,printBackground:true});
  const a3Info=pdfInfo(a3Pdf);
  check(/Pages:\s+2/.test(a3Info),'A3 calculation must also start on page two');
  const size=a3Info.match(/Page size:\s+([\d.]+) x ([\d.]+)/);
  check(size && +size[1]>1190 && +size[2]>840,'A3 dimensions must be retained');
  console.log('A3 PAGE SIZE AND CALCULATION BREAK OK');

  await ready(page,'donut');
  await page.evaluate(() => {
    const column=document.getElementById('calc_area');
    const row=column.firstElementChild.cloneNode(true);
    for(let i=0;i<80;i++) column.append(row.cloneNode(true));
    window.dispatchEvent(new Event('resize'));
  });
  await page.waitForTimeout(100);
  const large = await page.evaluate(() => [...document.querySelectorAll('.calculation-sheet')].map(sheet=>({
    oversized:sheet.dataset.oversizedFormula, height:sheet.querySelector('#calc').getBoundingClientRect().height,
    available:sheet.querySelector('.calculation-content').getBoundingClientRect().height})));
  check(large.length>1 && large.every(item=>!item.oversized && item.height<=item.available+1),'Long calculations must split across continuation sheets');
  console.log('LONG CALCULATIONS SPLIT WITHOUT CLIPPING OK');
  await ready(page,'donut');
  await page.evaluate(() => {
    const column=document.getElementById('calc_area');
    const row=column.firstElementChild.cloneNode(true);
    const flow=row.querySelector('.calc-formula');
    flow.replaceChildren();
    for(let i=0;i<400;i++) {
      const part=document.createElement('span');part.className='latex-part';part.textContent=`${10000+i}m² +`;
      flow.append(part);
    }
    column.replaceChildren(row);
    window.dispatchEvent(new Event('resize'));
  });
  await page.waitForTimeout(150);
  const formulaPages=await page.evaluate(() => ({sheets:document.querySelectorAll('.calculation-sheet').length,
    terms:[...document.querySelectorAll('.calculation-sheet .latex-part')].length,
    fit:[...document.querySelectorAll('.calculation-sheet')].every(sheet=>!sheet.dataset.oversizedFormula &&
      sheet.querySelector('#calc').getBoundingClientRect().height <= sheet.querySelector('.calculation-content').getBoundingClientRect().height+1)}));
  check(formulaPages.sheets>1 && formulaPages.terms===400 && formulaPages.fit,'One long formula must continue without losing terms');
  console.log('SINGLE LONG FORMULA SPLITS WITHOUT LOST TERMS OR CLIPPING OK');
  // Long calculations use coordinate continuation pages first, then standalone
  // calculation pages once all coordinate tables have been consumed.
  await ready(page,'circle-96-edges');
  await page.evaluate(() => {
    document.querySelector('.main_map').dataset.contentBounds='[3,3,147,147]';
    const column=document.getElementById('calc_area');
    const row=column.firstElementChild.cloneNode(true);
    for(let i=0;i<120;i++) column.append(row.cloneNode(true));
    window.dispatchEvent(new Event('resize'));
  });
  await page.waitForTimeout(150);
  const longShared=await page.evaluate(printPages);
  const sharedFits=await page.evaluate(()=>[...document.querySelectorAll('.calculation-sheet')].every(sheet=>
    !sheet.dataset.oversizedFormula && sheet.querySelector('#calc').getBoundingClientRect().height<=sheet.querySelector('.calculation-content').getBoundingClientRect().height+1));
  check(longShared.kinds.slice(1,4).every(kind=>kind==='coordinates-calculation') &&
    longShared.kinds.slice(4).every(kind=>kind==='calculation') && longShared.rows===96 && sharedFits,
    'Long calculations must pair all XY continuation tables first without lost rows or clipping');
  console.log('LONG CALCULATION CONTINUATIONS SHARE XY PAGES FIRST AND KEEP ALL ROWS OK');
  check(!errors.length,'Browser errors: '+errors.join('; '));
  fs.writeFileSync(path.join(output,'layout-verification.json'),JSON.stringify({status:'passed',a4:layoutPdf,a3:a3Pdf,inline:inlinePdf,
    coordinate_pages:circlePages.kinds,shared_xy:sharedPdf,shared_remainder:remainderPdf,shared_a3:sharedA3Pdf,
    shared_pages:shared.kinds,calculation_continuations:large.length,formula_continuations:formulaPages.sheets},null,2));
  await browser.close();
  console.log('PASS');
})().catch(error=>{console.error(error);process.exit(1);});
