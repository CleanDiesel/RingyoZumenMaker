// Render an exported drawing in Chromium; check fixed header boxes on screen and paper.
// Usage: node tests/header_layout.cjs <exported index.html> <QA directory>
const fs = require('node:fs');
const path = require('node:path');
const {pathToFileURL} = require('node:url');
const assert = require('node:assert/strict');
const {chromium} = require('playwright');

(async () => {
  const destination = path.resolve(process.argv[3]);
  fs.mkdirSync(destination, {recursive: true});
  const browser = await chromium.launch({headless: true,
    executablePath: process.env.CHROMIUM_PATH || 'C:/Program Files (x86)/Microsoft/Edge/Application/msedge.exe'});
  try {
    const page = await browser.newPage({viewport: {width: 1700, height: 1200}});
    await page.goto(pathToFileURL(path.resolve(process.argv[2])).href);
    await page.evaluate(() => document.fonts.ready);
    const keys = ['seizubi', 'seizujigyosha', 'seizusha', 'sanrinshoyusha', 'drawing_name', 'shinseino'];
    for (const paper of ['A4', 'A3']) {
      const factor = paper === 'A3' ? 420 / 297 : 1;
      await page.evaluate(factor => document.documentElement.style.setProperty('--paper-factor', factor), factor);
      for (const mode of ['short', 'multiple', 'many', 'long', 'blank']) {
        const data = mode === 'short' ? ['2026年10月05日', '製図事業者', '製図者', '所有者', '12林班60小班', '第123号']
          : mode === 'multiple' ? ['2026年10月05日、2026年10月06日', '事業者A、事業者B', '製図者A、製図者B', '所有者A、所有者B', '12林班60小班、13林班61小班', '第123号、第456号']
          : mode === 'many' ? keys.map((_, column) => Array.from({length: 50}, (_, n) => column === 0 ? `2026年10月${String(n % 28 + 1).padStart(2, '0')}日` : column === 4 ? `${n + 1}林班${n + 60}小班` : `名称${n + 1}`).join('、'))
          : mode === 'long' ? keys.map(() => '非常に長い名称とABCDEFGHIJKLMNOPQRSTUVWXYZ'.repeat(30))
          : keys.map(() => '');
        await page.evaluate(({keys, data}) => {
          const sheet = document.querySelector('.drawing-sheet.active');
          keys.forEach((key, index) => {
            const cell = sheet.querySelector('.' + key);
            cell.title = data[index];
            cell.querySelector('.header-text').textContent = data[index];
          });
        }, {keys, data});
        for (const media of ['screen', 'print']) {
          await page.emulateMedia({media});
          const result = await page.evaluate(keys => {
            const sheet = document.querySelector('.drawing-sheet.active');
            const header = sheet.querySelector('.info_container');
            const box = header.getBoundingClientRect();
            return {width: box.width, height: box.height, cells: keys.map(key => {
              const cell = sheet.querySelector('.' + key);
              const text = cell.querySelector('.header-text');
              const rect = text.getBoundingClientRect();
              const parent = cell.getBoundingClientRect();
              const style = getComputedStyle(text);
              return {text: text.textContent, title: cell.title, height: rect.height,
                fits: rect.left >= parent.left - .1 && rect.right <= parent.right + .1 && rect.top >= parent.top - .1 && rect.bottom <= parent.bottom + .1,
                clipped: text.scrollHeight > text.clientHeight + 1,
                clamp: Number(style.webkitLineClamp), overflow: style.overflow};
            })};
          }, keys);
          assert(Math.abs(result.width - 240 * factor * 96 / 25.4) < .1, `${paper}/${mode}: width changed`);
          assert(Math.abs(result.height - 17 * factor * 96 / 25.4) < .1, `${paper}/${mode}: height changed`);
          result.cells.forEach((cell, index) => {
            assert(cell.fits, `${paper}/${mode}/${media}/${keys[index]} exceeds its box`);
            assert.equal(cell.text, data[index], 'Full source value must remain intact');
            assert.equal(cell.title, data[index]);
            assert.equal(cell.overflow, 'hidden');
            assert(cell.clamp >= 1 && cell.clamp <= 2);
            if (mode === 'many' || mode === 'long') assert(cell.clipped, 'Excess text should be clamped');
            if (mode === 'short' || mode === 'multiple') assert(!cell.clipped, `${paper}/${mode}/${keys[index]} unnecessarily truncated`);
          });
        }
        await page.emulateMedia({media: 'screen'});
        if (mode === 'multiple' || mode === 'many') {
          await page.locator('.drawing-sheet.active .info_container').screenshot({path: path.join(destination, `${paper}-${mode}.png`)});
        }
      }
    }
    console.log('A4/A3: short, multiple, 50 values, long unbroken values and blanks; fixed boxes, no overflow, full values retained; screen/print OK');
  } finally { await browser.close(); }
})().catch(error => { console.error(error); process.exitCode = 1; });
