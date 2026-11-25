/* The same contract line follows a purchase request through PO receipt and invoice matching. */
window.erpContractPricing = (() => {
  'use strict';
  const columns = ['合同币种','每件净价','每件税额'];
  function inputs(line, escape) {
    const field = (name, label, attrs) =>
      `<td><input name="${name}" aria-label="${label}" value="${escape(line[name] ?? '')}" ${attrs}></td>`;
    return field('contract_currency', columns[0], 'maxlength="3" size="5" placeholder="USD"') +
      field('net_unit_price', columns[1], 'inputmode="decimal" size="9"') +
      field('unit_tax', columns[2], 'inputmode="decimal" size="9" placeholder="0"');
  }
  function read(row) {
    const text = name => row.querySelector('[name="'+name+'"]').value.trim();
    const contract_currency = text('contract_currency').toUpperCase(), net_unit_price = text('net_unit_price'), unit_tax = text('unit_tax');
    if (!contract_currency && !net_unit_price && !unit_tax) return {};
    if (!/^[A-Z]{3}$/.test(contract_currency) || !/^\d+(\.\d{1,4})?$/.test(net_unit_price) || !/[1-9]/.test(net_unit_price))
      throw new Error('已计价的采购行须填写三位合同币种和大于零的每件净价。');
    if (unit_tax && !/^\d+(\.\d{1,4})?$/.test(unit_tax)) throw new Error('每件税额须为非负金额。');
    return {contract_currency, net_unit_price, unit_tax:unit_tax || '0'};
  }
  function description(line) {
    return line.contract_currency && line.net_unit_price != null
      ? `${line.contract_currency} · 净价 ${line.net_unit_price} / 件 · 税 ${line.unit_tax ?? 0} / 件`
      : '未登记合同价 · 人工核对';
  }
  function receivedValue(line) {
    if (!line.contract_currency || line.net_unit_price == null) return '—';
    const parts = [line.net_unit_price, line.unit_tax ?? 0].map(value => String(value).split('.'));
    const scale = Math.max(...parts.map(([,fraction='']) => fraction.length));
    const units = parts.map(([whole,fraction='']) => BigInt(whole + fraction.padEnd(scale,'0')));
    const sum = ((units[0]+units[1]) * BigInt(line.received)).toString().padStart(scale+1,'0');
    return (scale ? sum.slice(0,-scale)+'.'+sum.slice(-scale) : sum) + ' ' + line.contract_currency;
  }
  return {columns, inputs, read, description, receivedValue};
})();
