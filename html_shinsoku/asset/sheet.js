(() => {
  document.querySelectorAll('[data-latex]').forEach(element => {
    window.katex?.render(element.dataset.latex, element, {
      throwOnError: false, strict: 'ignore', output: 'htmlAndMathml'
    });
  });
  const main = document.getElementById('main_container');
  const select = document.getElementById('zumen_type');
  if (!main || !select) return;
  const toolbar = document.getElementById('zumen_select');
  document.body.insertBefore(toolbar, main);
  const panels = [...main.querySelectorAll('[data-option-panel]')];
  const tables = [...main.querySelectorAll('[data-xy-table]')];
  const mapContainer = document.getElementById('map_container');
  const mapToggle = document.getElementById('map_visibility_toggle');
  const calc = document.getElementById('calc');
  const pageControls = document.getElementById('xy_updown');
  const previousPage = document.getElementById('xy_prev');
  const nextPage = document.getElementById('xy_next');
  const locationRows = [...main.querySelectorAll('#location .value1, #location .value2, .application-number-row')];
  const applicationNumberControl = document.getElementById('application_number_control');
  const applicationNumberInput = document.getElementById('application_number_input');
  const editableApplicationNumbers = new Map([...main.querySelectorAll('#application_number [data-option-panel]')]
    .filter(value => !value.textContent.trim()).map(value => [value.dataset.optionPanel, value]));
  const enteredApplicationNumbers = new Map();
  const applicationNumberKey = panel => `ringyo:application-number:${location.pathname}:${panel}`;
  const pageStatus = document.createElement('span');
  if (pageControls) {
    toolbar.append(pageControls);
    previousPage.textContent = '前のページ';
    nextPage.textContent = '次のページ';
    pageControls.insertBefore(pageStatus, nextPage);
  }
  const baseTitle = document.title;
  const siteTitle = baseTitle.replace(/(?:^|\s*-\s*)実測図\s*$/, '');
  const drawingIndices = [...select.options].map((option, i) => option.value ? i : -1).filter(i => i >= 0);
  let pageIndex = 0;
  let calculationSheets = [];
  const panelValues = element => (element.dataset.optionPanel || '').split(/\s+/).filter(Boolean);
  const activeTables = () => tables.filter(table => panelValues(table).includes(select.value));
  const hasCalculation = () => calc && calc.dataset.isJochiKeisan === '1' && panelValues(calc).includes(select.value);
  const pages = () => {
    const count = Math.max(1, activeTables().length);
    const result = [{type: 'drawing', table: 0}];
    calculationSheets.forEach((sheet, index) => result.push({
      type: sheet.dataset.coordinateTable ? 'coordinates-calculation' : 'calculation', index
    }));
    const sharedTables = calculationSheets.filter(sheet => sheet.dataset.coordinateTable).length;
    for (let index = 1 + sharedTables; index < count; index++) result.push({type: 'coordinates', table: index});
    return result;
  };
  function updatePanels() {
    const options = new Set([...select.options].map(option => option.value));
    panels.forEach(panel => {
      const values = panelValues(panel);
      panel.hidden = !values.some(value => options.has(value)) || !values.includes(select.value);
      if (panel === calc && !hasCalculation()) panel.hidden = true;
    });
    const number = editableApplicationNumbers.get(select.value);
    if (applicationNumberControl) applicationNumberControl.hidden = !number;
    if (number) {
      if (!enteredApplicationNumbers.has(select.value)) {
        let saved = '';
        try { saved = localStorage.getItem(applicationNumberKey(select.value)) || ''; } catch {}
        enteredApplicationNumbers.set(select.value, saved);
      }
      const value = enteredApplicationNumbers.get(select.value);
      number.textContent = value.trim();
      number.title = number.textContent;
      if (applicationNumberInput.value !== value) applicationNumberInput.value = value;
    }
    locationRows.forEach(row => {
      const value = row.querySelector('#application_number') || row;
      const activeValue = value.querySelector('[data-option-panel]') ?
        value.querySelector('[data-option-panel]:not([hidden])') : value;
      row.hidden = !activeValue?.textContent.trim();
    });
  }
  function updateMapVisibility() {
    if (mapContainer && mapToggle) mapContainer.dataset.mapVisible = mapToggle.checked ? '1' : '0';
  }
  function showPage() {
    const choices = pages();
    pageIndex = Math.max(0, Math.min(pageIndex, choices.length - 1));
    const selected = choices[pageIndex];
    const calculationPage = selected.index !== undefined;
    tables.forEach(table => { table.hidden = true; });
    const active = activeTables();
    if (!calculationPage && active[selected.table]) active[selected.table].hidden = false;
    main.hidden = calculationPage;
    calculationSheets.forEach((sheet, index) => { sheet.hidden = !calculationPage || selected.index !== index; });
    if (mapToggle) mapToggle.checked = selected.type === 'drawing';
    updateMapVisibility();
    if (pageControls) {
      pageControls.hidden = choices.length <= 1;
      previousPage.disabled = pageIndex === 0;
      nextPage.disabled = pageIndex === choices.length - 1;
      pageStatus.textContent = `${pageIndex + 1} / ${choices.length}ページ`;
    }
    main.dataset.pageCount = String(choices.length);
  }
  function fitHeaders() {
    main.querySelectorAll('#info_container .value, #info_container .value1, #info_container .value2, #location .value3, .application-number-row').forEach(element => {
      if (!element.getClientRects().length) return;
      element.style.fontSize = '';
      for (let size = 3; size > 1.5 && (element.scrollHeight > element.clientHeight + 1 || element.scrollWidth > element.clientWidth + 1); size -= .1) {
        element.style.fontSize = `${size}mm`;
      }
    });
  }
  function overlapsMap() {
    if (!hasCalculation()) return false;
    const image = [...main.querySelectorAll('.main_map')].find(item => panelValues(item).includes(select.value));
    if (!image) return false;
    const imageRect = image.getBoundingClientRect();
    const formula = calc.getBoundingClientRect();
    const frame = mapContainer.getBoundingClientRect();
    const mm = imageRect.width / 150;
    const bounds = JSON.parse(image.dataset.contentBounds || '[0,0,150,150]');
    const ink = {left: imageRect.left + bounds[0] * mm, top: imageRect.top + bounds[1] * mm,
      right: imageRect.left + bounds[2] * mm, bottom: imageRect.top + bounds[3] * mm};
    const gap = 2 * mm;
    return (formula.left < ink.right + gap && formula.right > ink.left - gap &&
      formula.top < ink.bottom + gap && formula.bottom > ink.top - gap) ||
      formula.bottom > frame.bottom - gap || formula.left < frame.left || formula.right > frame.right;
  }
  function createCalculationSheet(number) {
    const sheet = main.cloneNode(true);
    sheet.removeAttribute('id');
    sheet.className = 'calculation-sheet';
    sheet.hidden = false;
    const coordinateTable = activeTables()[number + 1];
    sheet.querySelector('h1').textContent = '実測図';
    const content = document.createElement('div');
    content.className = 'calculation-content';
    const formula = calc.cloneNode(false);
    formula.removeAttribute('hidden');
    delete formula.dataset.pageMode;
    content.append(formula);
    if (coordinateTable) {
      sheet.dataset.coordinateTable = String(number + 1);
      sheet.classList.add('coordinates-calculation-sheet');
      sheet.querySelectorAll('[data-xy-table]').forEach(table => { table.hidden = table.id !== coordinateTable.id; });
      sheet.querySelector('#map_container').replaceWith(content);
    } else {
      sheet.querySelector('#sub_container').replaceWith(content);
    }
    document.body.append(sheet);
    return sheet;
  }
  function splitCalculations() {
    const sections = [...calc.children];
    const pending = sections.map(section => [...section.children].map(row => row.cloneNode(true)));
    let number = 0;
    do {
      const sheet = createCalculationSheet(number++);
      const content = sheet.querySelector('.calculation-content');
      const formula = sheet.querySelector('#calc');
      sections.forEach((section, index) => {
        const column = section.cloneNode(false);
        formula.append(column);
        let added = 0;
        while (pending[index].length) {
          column.append(pending[index][0]);
          if (formula.getBoundingClientRect().height > content.getBoundingClientRect().height + 1 && added) {
            column.lastElementChild.remove();
            break;
          }
          if (formula.getBoundingClientRect().height > content.getBoundingClientRect().height + 1) {
            const row = pending[index][0];
            const flow = row.querySelector('.calc-formula');
            const parts = flow ? [...flow.children] : [];
            if (parts.length > 1) {
              const tail = row.cloneNode(true);
              let low = 1, high = parts.length - 1, fit = 0;
              while (low <= high) {
                const middle = Math.floor((low + high) / 2);
                flow.replaceChildren(...parts.slice(0, middle));
                if (formula.getBoundingClientRect().height <= content.getBoundingClientRect().height + 1) {
                  fit = middle; low = middle + 1;
                } else high = middle - 1;
              }
              if (fit) {
                flow.replaceChildren(...parts.slice(0, fit));
                tail.querySelector('.calc-formula').replaceChildren(...parts.slice(fit));
                if (!tail.dataset.continued) {
                  const label = tail.querySelector('.calc-label');
                  if (label) label.append(document.createTextNode('（続き）'));
                  tail.dataset.continued = '1';
                }
                pending[index].splice(0, 1, tail);
                added++;
                break;
              }
              flow.replaceChildren(...parts);
            }
          }
          pending[index].shift();
          added++;
          if (formula.getBoundingClientRect().height > content.getBoundingClientRect().height + 1) {
            sheet.dataset.oversizedFormula = '1';
            break;
          }
        }
      });
      calculationSheets.push(sheet);
    } while (pending.some(rows => rows.length));
  }
  function reflow() {
    calculationSheets.forEach(sheet => sheet.remove());
    calculationSheets = [];
    main.hidden = false;
    updatePanels();
    tables.forEach(table => { table.hidden = true; });
    if (activeTables()[0]) activeTables()[0].hidden = false;
    if (mapContainer) mapContainer.dataset.mapVisible = '1';
    fitHeaders();
    if (calc) delete calc.dataset.pageMode;
    const separate = overlapsMap();
    if (calc) calc.dataset.pageMode = separate ? 'separate' : 'inline';
    if (separate) splitCalculations();
    showPage();
    window.ringyoSheetReady = true;
  }
  const previousDrawing = document.getElementById('drawing_previous');
  const nextDrawing = document.getElementById('drawing_next');
  function drawingChanged() {
    pageIndex = 0;
    previousDrawing.disabled = select.selectedIndex === drawingIndices[0];
    nextDrawing.disabled = select.selectedIndex === drawingIndices[drawingIndices.length - 1];
    const option = select.options[select.selectedIndex];
    document.title = option?.value ? [siteTitle, option.textContent.trim(), '実測図'].filter(Boolean).join(' - ') : baseTitle;
    reflow();
  }
  previousPage?.addEventListener('click', () => { pageIndex--; showPage(); });
  nextPage?.addEventListener('click', () => { pageIndex++; showPage(); });
  for (const [button, step] of [[previousDrawing, -1], [nextDrawing, 1]]) {
    button.addEventListener('click', () => {
      const index = drawingIndices.indexOf(select.selectedIndex) + step;
      if (index >= 0 && index < drawingIndices.length) {
        select.selectedIndex = drawingIndices[index];
        drawingChanged();
      }
    });
  }
  select.addEventListener('change', drawingChanged);
  applicationNumberInput?.addEventListener('input', () => {
    const value = applicationNumberInput.value;
    enteredApplicationNumbers.set(select.value, value);
    try { localStorage.setItem(applicationNumberKey(select.value), value); } catch {}
    reflow();
  });
  mapToggle?.addEventListener('change', updateMapVisibility);
  const note = document.createElement('span');
  note.textContent = '印刷：指定用紙・横向き／倍率100%／余白なし／ヘッダー・フッターなし';
  toolbar.append(note);
  window.addEventListener('beforeprint', () => {
    document.getElementById('print_pages')?.remove();
    const all = document.getElementById('print_all').checked;
    const saved = [select.selectedIndex, pageIndex, mapToggle?.checked, document.title];
    const container = document.createElement('div');
    container.id = 'print_pages';
    for (const index of all ? drawingIndices : [select.selectedIndex]) {
      select.selectedIndex = index;
      pageIndex = 0;
      reflow();
      pages().forEach((descriptor, index) => {
        pageIndex = index;
        showPage();
        const sheet = (descriptor.index !== undefined ? calculationSheets[descriptor.index] : main).cloneNode(true);
        sheet.hidden = false;
        sheet.querySelector('#zumen_select')?.remove();
        if (descriptor.type === 'coordinates' || (descriptor.type === 'drawing' && calculationSheets.length)) {
          sheet.querySelector('#calc')?.remove();
        }
        const wrapper = document.createElement('div');
        wrapper.className = 'print-page';
        wrapper.dataset.pageType = descriptor.type;
        wrapper.append(sheet);
        container.append(wrapper);
      });
    }
    document.body.append(container);
    [select.selectedIndex, pageIndex] = saved;
    reflow();
    if (mapToggle) mapToggle.checked = saved[2];
    updateMapVisibility();
    document.title = saved[3];
    document.body.dataset.printAll = '1';
  });
  window.addEventListener('afterprint', () => {
    document.getElementById('print_pages')?.remove();
    delete document.body.dataset.printAll;
  });
  let resizeFrame;
  window.addEventListener('resize', () => {
    cancelAnimationFrame(resizeFrame);
    resizeFrame = requestAnimationFrame(reflow);
  });
  if (!select.value && drawingIndices.length) select.selectedIndex = drawingIndices[0];
  drawingChanged();
  document.fonts.ready.then(reflow);
  main.querySelectorAll('.main_map').forEach(image => image.addEventListener('load', reflow));
})();
