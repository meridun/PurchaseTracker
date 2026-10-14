// Amazon order extractor. Run in the browser console (or via Claude in Chrome) while
// on https://www.amazon.com/your-orders/orders?timeFilter=year-YYYY  (any page of it).
//
// For every order card on the current page it fetches the printable invoice
// (same-origin, uses your logged-in session) and returns a JSON string of orders
// with line items, prices, HSA-eligibility flags, ship-to name and payment method.
// Feed the result to:  pt import-amazon-json orders.json
//
// Nothing is sent anywhere; it only reads pages you can already see.

await (async () => {
  const norm = s => (s || '').replace(/\s+/g, ' ').trim();
  const money = s => {
    const m = (s || '').replace(/,/g, '').match(/(-?)\$\s*(\d+\.\d\d)/);
    return m ? (m[1] ? -1 : 1) * parseFloat(m[2]) : null;
  };

  const invoiceLinks = [...document.querySelectorAll('a')]
    .filter(a => /view invoice/i.test(a.textContent))
    .map(a => a.href);

  const orders = [];
  for (const url of invoiceLinks) {
    const html = await (await fetch(url, { credentials: 'include' })).text();
    const d = new DOMParser().parseFromString(html, 'text/html');
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
    orders.push({
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
    });
  }
  return JSON.stringify(orders);
})();
