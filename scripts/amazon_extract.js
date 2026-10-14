// Amazon order extractor. Run in the browser console (F12 -> Console) while logged in,
// on https://www.amazon.com/your-orders/orders?timeFilter=year-YYYY
//
// Walks every page of the order list for that filter, fetches each order's printable
// invoice (same-origin, your own session), and produces JSON for `pt import-amazon`:
// line items, prices, FSA/HSA flags, ship-to, payment method, charge summary, and the
// invoice HTML itself (saved by the importer as the receipt).
//
// Nothing is sent anywhere; it only reads pages you can already see.

const CONFIG = {
  // Preferred: paste the output of `pt amazon-refs` here. Those are the order numbers
  // seen on Amazon's Transactions page that have no invoice imported yet.
  ORDER_REFS: null,         // e.g. ["112-1234567-1234567", ...]; null = use the current list page
  MAX_PAGES: 50,            // list pages to walk when ORDER_REFS is null (10 orders each)
  INCLUDE_INVOICE_HTML: true,
  DOWNLOAD: true,           // false: return the JSON string instead of saving a file
  DELAY_MS: 250,            // pause between fetches
};

await (async () => {
  const norm = s => (s || '').replace(/\s+/g, ' ').trim();
  const money = s => {
    const m = (s || '').replace(/,/g, '').match(/(-?)\$\s*(\d+\.\d\d)/);
    return m ? (m[1] ? -1 : 1) * parseFloat(m[2]) : null;
  };
  const sleep = ms => new Promise(r => setTimeout(r, ms));
  const fetchDoc = async url => {
    const html = await (await fetch(url)).text();
    return [new DOMParser().parseFromString(html, 'text/html'), html];
  };

  const parseInvoice = (d, html) => {
    // Some invoice variants embed <script> inside data components; drop them before reading text.
    d.querySelectorAll('script, style, noscript').forEach(e => e.remove());
    const q  = (n, r = d) => r.querySelector(`[data-component="${n}"]`);
    const qa = (n, r = d) => [...r.querySelectorAll(`[data-component="${n}"]`)];
    const t  = (n, r = d) => norm(q(n, r)?.textContent);
    const charges = {};
    for (const li of q('chargeSummary')?.querySelectorAll('li') || []) {
      const s = norm(li.textContent);
      const i = s.indexOf(':');
      if (i > 0) charges[s.slice(0, i).trim()] = s.slice(i + 1).trim();
    }
    const items = qa('purchasedItemsRightGrid').map(g => {
      const a = q('itemTitle', g)?.querySelector('a');
      const prices = [...(q('unitPrice', g)?.querySelectorAll('span span') || [])]
        .map(s => money(s.textContent)).filter(v => v !== null);
      const qm = t('quantity', g).match(/(\d+)/);
      return {
        title: norm(a?.textContent),
        asin: ((a?.getAttribute('href') || '').match(/\/dp\/([A-Z0-9]{10})/) || [])[1] || null,
        quantity: qm ? parseInt(qm[1], 10) : 1,
        unit_price: prices[0] ?? null,
        line_price: prices[1] ?? prices[0] ?? null,
        hsa_eligible: /FSA or HSA eligible/i.test(t('healthBenefitAccountInformation', g)),
        sold_by: t('orderedMerchant', g).replace(/^Sold by:\s*/i, '') || null,
      };
    });
    const shipLis = q('shippingAddress')?.querySelectorAll('li') || [];
    return {
      order_ref: t('orderId'),
      order_date: t('orderDate'),
      ship_to: norm(shipLis[0]?.textContent) || null,
      payment_method: norm(q('viewPaymentPlanSummaryWidget')?.textContent)
        .replace(/^Payment method\s*/i, '').replace(/View related transactions.*$/i, '').trim() || null,
      subtotal: money(charges['Item(s) Subtotal']),
      shipping: money(charges['Shipping & Handling']),
      tax: money(charges['Estimated tax to be collected'] ?? charges['Estimated tax'] ?? charges['Total tax']),
      grand_total: money(charges['Grand Total']),
      hsa_eligible_total: money(charges['FSA or HSA eligible']),
      charges,
      shipments: qa('shipmentStatus').map(e => norm(e.textContent)),
      items,
      invoice_html: CONFIG.INCLUDE_INVOICE_HTML ? html : undefined,
    };
  };

  const invoiceUrl = ref => {
    const u = new URL('/gp/css/summary/print.html', location.origin);
    u.searchParams.set('orderID', ref);
    return u.href;
  };

  // Which orders to fetch. Order-list pages fetched without a browser render come back
  // without order numbers (they are filled in client-side), so walking the list only
  // works for the page you are looking at. The reliable source of order numbers is the
  // Transactions page (scripts/amazon_transactions.js -> `pt amazon-refs`).
  let refs;
  if (CONFIG.ORDER_REFS) {
    refs = CONFIG.ORDER_REFS;
  } else {
    refs = [...document.querySelectorAll('.order-card')]
      .map(c => (c.textContent.match(/ORDER #\s*([\w-]{15,})/i) || [])[1]).filter(Boolean);
    console.warn(`ORDER_REFS not set: extracting only the ${refs.length} orders on this page`);
  }

  const orders = [];
  const failed = [];
  for (const ref of [...new Set(refs)]) {
    try {
      const [d, html] = await fetchDoc(invoiceUrl(ref));
      const o = parseInvoice(d, html);
      if (!o.order_ref) { o.order_ref = ref; o.parse_warning = 'invoice page had no order components'; }
      orders.push(o);
    } catch (e) {
      failed.push({ ref, error: e.message });
    }
    window.__ptProgress = `${orders.length}/${refs.length} fetched, ${failed.length} failed`;
    if (orders.length % 25 === 0) console.log(window.__ptProgress);
    await sleep(CONFIG.DELAY_MS);
  }
  window.__ptOrders = orders;
  if (failed.length) console.warn('failed:', failed);

  const payload = JSON.stringify({
    kind: 'amazon-orders',
    extracted_at: new Date().toISOString(),
    source_url: location.origin + location.pathname,
    data: orders,
  });
  if (!CONFIG.DOWNLOAD) return payload;
  // Invoice HTML makes this large (~250 KB per order), so gzip it; pt reads .json.gz.
  const gz = await new Response(new Blob([payload]).stream().pipeThrough(new CompressionStream('gzip'))).blob();
  const a = document.createElement('a');
  a.href = URL.createObjectURL(gz);
  a.download = `amazon-orders-${new Date().toISOString().slice(0, 10)}.json.gz`;
  document.body.appendChild(a); a.click(); a.remove();
  return `downloaded ${a.download}: ${orders.length} orders, ${(gz.size / 1048576).toFixed(1)} MB`;
})();
