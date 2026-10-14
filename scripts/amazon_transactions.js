// Amazon transactions extractor. Run in the browser console while logged in, on
// https://www.amazon.com/cpe/yourpayments/transactions
//
// Reads every card charge Amazon lists (date, instrument, amount, order number, merchant,
// pending/completed), following "Next Page" by posting the page's own form, and produces
// JSON for `pt import-amazon`. These rows are what tie an order to the exact charge(s)
// that appear on a card statement, including Amazon's per-shipment splits.
//
// Nothing is sent anywhere; it only reads pages you can already see.

const CONFIG = {
  MAX_PAGES: 30,            // 20 charges per page
  STOP_BEFORE: null,        // e.g. '2026-01-01': stop once charges are older than this
  DOWNLOAD: true,
  DELAY_MS: 400,
};

await (async () => {
  const norm = s => (s || '').replace(/\s+/g, ' ').trim();
  const money = s => {
    const m = (s || '').replace(/,/g, '').match(/(-?)\$\s*(\d+\.\d\d)/);
    return m ? (m[1] ? -1 : 1) * parseFloat(m[2]) : null;
  };
  const sleep = ms => new Promise(r => setTimeout(r, ms));
  const isoDate = s => { const d = new Date(s); return isNaN(d) ? s : d.toISOString().slice(0, 10); };

  const parsePage = d => {
    const rows = [];
    let date = null;
    for (const el of d.querySelectorAll('.apx-transaction-date-container, .apx-transactions-line-item-component-container')) {
      if (el.classList.contains('apx-transaction-date-container')) { date = norm(el.textContent); continue; }
      // Blocks vary by row (the status block only exists while pending), so classify by content.
      const texts = [...el.querySelectorAll('span, a')].map(e => norm(e.textContent)).filter(Boolean);
      const amountText = texts.find(s => /\$/.test(s)) || '';
      const orderText  = texts.find(s => /Order #/i.test(s)) || '';
      const statusText = texts.find(s => /^(pending|completed|declined|refunded)$/i.test(s));
      const rest = texts.filter(s => s !== amountText && s !== orderText && s !== statusText);
      rows.push({
        date: isoDate(date),
        instrument: rest[0] || null,          // first non-amount text: 'Discover ****1234'
        amount: money(amountText),
        status: statusText || 'Completed',
        order_ref: ((orderText.match(/Order #\s*([\w-]+)/i) || [])[1]) || null,
        merchant: rest[1] || null,            // 'AMZN Mktp US', 'Amazon.com', 'Amazon Tips'
      });
    }
    return rows;
  };

  // "Next Page" is handled by Amazon's payments widget as an in-place update (an XHR to
  // continueWidget), not a form navigation, so we drive the real button and wait for the
  // row list to change. The page never reloads, so state here survives across pages.
  const nextPageInput = () => [...document.querySelectorAll('input[type="submit"]')]
    .find(i => /nextPageKey/.test(i.name || '') && !i.disabled);
  const signature = () => [...document.querySelectorAll('.apx-transactions-line-item-component-container')]
    .map(e => norm(e.textContent)).join('|');
  const waitForChange = async (before, timeoutMs = 20000) => {
    const t0 = Date.now();
    while (Date.now() - t0 < timeoutMs) {
      await sleep(250);
      if (signature() !== before && !document.querySelector('[class*="spinner-overlay"]:not([style*="display: none"])')) return true;
    }
    return false;
  };

  const all = [];
  for (let page = 0; page < CONFIG.MAX_PAGES; page++) {
    const rows = parsePage(document);
    all.push(...rows);
    window.__ptProgress = `page ${page + 1}: ${all.length} charges, oldest ${rows[rows.length - 1]?.date}`;
    console.log(window.__ptProgress);
    if (CONFIG.STOP_BEFORE && rows.length && rows[rows.length - 1].date < CONFIG.STOP_BEFORE) break;
    const next = nextPageInput();
    if (!next) break;
    const before = signature();
    await sleep(CONFIG.DELAY_MS);
    next.click();
    if (!(await waitForChange(before))) { console.warn('Next Page did not load; stopping'); break; }
  }
  window.__ptTx = all;

  const payload = JSON.stringify({
    kind: 'amazon-transactions',
    extracted_at: new Date().toISOString(),
    data: all,
  });
  if (!CONFIG.DOWNLOAD) return payload;
  const blob = new Blob([payload], { type: 'application/json' });
  const a = document.createElement('a');
  a.href = URL.createObjectURL(blob);
  a.download = `amazon-transactions-${new Date().toISOString().slice(0, 10)}.json`;
  document.body.appendChild(a); a.click(); a.remove();
  return `downloaded ${a.download}: ${all.length} charges`;
})();
