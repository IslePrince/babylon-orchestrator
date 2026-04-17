/**
 * costs.js — Cost ledger with budget gauges, Chart.js charts,
 * paginated transaction log, and CSV export.
 */

const Costs = {

  slug: null,
  data: null,
  transactions: [],
  filteredTx: [],
  page: 0,
  pageSize: 50,
  editingBudgets: false,

  async init(slug) {
    Costs.slug = slug;
    await Costs.loadCosts();
  },

  async loadCosts() {
    try {
      Costs.data = await App.api('GET', `/api/${Costs.slug}/costs`);
      Costs.transactions = Costs.data.transactions || [];
      Costs.filteredTx = [...Costs.transactions];
      Costs._renderGauges();
      Costs._renderCharts();
      Costs._populateFilter();
      Costs._renderTransactions();
    } catch (e) {
      document.getElementById('costs-root').innerHTML =
        '<div class="text-gray-600">No cost data available. Run some stages first.</div>';
    }
  },

  toggleBudgetEdit() {
    Costs.editingBudgets = !Costs.editingBudgets;
    const btn = document.getElementById('budget-edit-btn');
    if (btn) btn.textContent = Costs.editingBudgets ? 'Cancel' : 'Edit Budgets';
    Costs._renderGauges();
  },

  async saveBudgets() {
    const inputs = document.querySelectorAll('.budget-input');
    const updates = {};
    inputs.forEach(input => {
      const api = input.dataset.api;
      const val = parseFloat(input.value);
      if (api && !isNaN(val) && val >= 0) {
        updates[api] = val;
      }
    });

    try {
      await App.api('POST', `/api/${Costs.slug}/settings/budgets`, updates);
      App.showToast('Budgets updated', 'success');
      Costs.editingBudgets = false;
      const btn = document.getElementById('budget-edit-btn');
      if (btn) btn.textContent = 'Edit Budgets';
      await Costs.loadCosts();
    } catch (e) {
      App.showToast('Failed to save budgets: ' + e.message, 'error');
    }
  },

  _renderGauges() {
    const container = document.getElementById('cost-gauges');
    if (!container || !Costs.data) return;

    const budgets = Costs.data.api_budgets || {};
    const byApi = Costs.data.by_api || {};
    const editing = Costs.editingBudgets;

    let html = '';
    for (const [api, cfg] of Object.entries(budgets)) {
      if (!cfg.enabled) continue;
      const spent = (byApi[api] || {}).spent || 0;
      const budget = cfg.budget_usd || 0;
      const pct = budget > 0 ? Math.min((spent / budget) * 100, 100) : 0;
      const remaining = Math.max(0, budget - spent);

      html += `
        <div class="card p-3 text-center">
          <div class="text-xs text-gray-500 mb-2">${App.escapeHtml(api.replace(/_/g, ' '))}</div>
          <div class="relative w-16 h-16 mx-auto mb-2">
            <svg class="w-16 h-16 transform -rotate-90" viewBox="0 0 36 36">
              <circle cx="18" cy="18" r="15.9" fill="none" stroke="#1f2937" stroke-width="3"/>
              <circle cx="18" cy="18" r="15.9" fill="none"
                      stroke="${pct >= 90 ? '#ef4444' : pct >= 70 ? '#eab308' : '#22c55e'}"
                      stroke-width="3"
                      stroke-dasharray="${pct} ${100 - pct}"
                      stroke-linecap="round"/>
            </svg>
            <div class="absolute inset-0 flex items-center justify-center text-xs font-medium text-gray-300">
              ${Math.round(pct)}%
            </div>
          </div>
          ${editing ? `
            <div class="flex items-center justify-center gap-1 text-xs">
              <span class="text-gray-500">$</span>
              <input type="number" step="1" min="0" value="${budget}"
                     data-api="${App.escapeHtml(api)}"
                     class="budget-input w-16 bg-gray-800 border border-gray-600 rounded px-1 py-0.5 text-xs text-gray-200 text-center focus:outline-none focus:ring-1 focus:ring-babylon-500" />
            </div>
            <div class="text-xs text-gray-500 mt-1">${App.formatCost(spent)} spent</div>
          ` : `
            <div class="text-xs text-gray-400">${App.formatCost(spent)} / ${App.formatCost(budget)}</div>
            <div class="text-xs ${remaining < 1 ? 'text-red-400' : 'text-gray-600'} mt-0.5">${App.formatCost(remaining)} left</div>
          `}
        </div>
      `;
    }

    if (editing) {
      html += `
        <div class="card p-3 flex items-center justify-center">
          <button onclick="Costs.saveBudgets()"
                  class="px-3 py-1.5 bg-babylon-600 hover:bg-babylon-500 text-white text-xs rounded transition-colors">
            Save
          </button>
        </div>
      `;
    }

    container.innerHTML = html || '<div class="col-span-full text-gray-600 text-xs">No API budgets configured.</div>';
  },

  _renderCharts() {
    Costs._renderCumulativeChart();
    Costs._renderStageChart();
  },

  _renderCumulativeChart() {
    const canvas = document.getElementById('chart-cumulative');
    if (!canvas || !Costs.transactions.length) return;

    // Sort transactions by date and compute cumulative
    const sorted = [...Costs.transactions].sort((a, b) =>
      (a.timestamp || '').localeCompare(b.timestamp || '')
    );

    let cumulative = 0;
    const labels = [];
    const data = [];
    sorted.forEach((tx, i) => {
      cumulative += tx.cost_usd || 0;
      // Only label every Nth point for readability
      const date = (tx.timestamp || '').split('T')[0];
      labels.push(i % Math.max(1, Math.floor(sorted.length / 10)) === 0 ? date : '');
      data.push(cumulative);
    });

    new Chart(canvas, {
      type: 'line',
      data: {
        labels,
        datasets: [{
          label: 'Cumulative ($)',
          data,
          borderColor: '#ee7a12',
          backgroundColor: 'rgba(238, 122, 18, 0.1)',
          fill: true,
          tension: 0.3,
          pointRadius: 0,
        }]
      },
      options: {
        responsive: true,
        plugins: { legend: { display: false } },
        scales: {
          x: { ticks: { color: '#6b7280', maxRotation: 0 }, grid: { color: '#1f2937' } },
          y: { ticks: { color: '#6b7280', callback: v => '$' + v.toFixed(2) }, grid: { color: '#1f2937' } }
        }
      }
    });
  },

  _renderStageChart() {
    const canvas = document.getElementById('chart-by-stage');
    if (!canvas || !Costs.data) return;

    const byStage = Costs.data.by_stage || {};
    const labels = Object.keys(byStage);
    const data = labels.map(k => (byStage[k] || {}).spent || 0);

    if (labels.length === 0) return;

    new Chart(canvas, {
      type: 'bar',
      data: {
        labels: labels.map(l => l.replace(/_/g, ' ')),
        datasets: [{
          label: 'Spend ($)',
          data,
          backgroundColor: '#b94409',
          borderRadius: 4,
        }]
      },
      options: {
        responsive: true,
        plugins: { legend: { display: false } },
        scales: {
          x: { ticks: { color: '#6b7280' }, grid: { display: false } },
          y: { ticks: { color: '#6b7280', callback: v => '$' + v.toFixed(2) }, grid: { color: '#1f2937' } }
        }
      }
    });
  },

  _populateFilter() {
    const sel = document.getElementById('cost-filter-api');
    if (!sel) return;

    const apis = new Set(Costs.transactions.map(t => t.api || ''));
    sel.innerHTML = '<option value="">All APIs</option>' +
      [...apis].filter(Boolean).map(api =>
        `<option value="${api}">${App.escapeHtml(api)}</option>`
      ).join('');
  },

  filterTransactions() {
    const api = document.getElementById('cost-filter-api').value;
    Costs.filteredTx = api
      ? Costs.transactions.filter(t => t.api === api)
      : [...Costs.transactions];
    Costs.page = 0;
    Costs._renderTransactions();
  },

  _renderTransactions() {
    const container = document.getElementById('cost-transactions');
    const countEl = document.getElementById('cost-tx-count');
    const pagEl = document.getElementById('cost-pagination');
    if (!container) return;

    const txs = Costs.filteredTx;
    if (countEl) countEl.textContent = `${txs.length} transactions`;

    if (txs.length === 0) {
      container.innerHTML = '<div class="text-gray-600 text-xs py-4 text-center">No transactions recorded.</div>';
      if (pagEl) pagEl.innerHTML = '';
      return;
    }

    const start = Costs.page * Costs.pageSize;
    const pageItems = txs.slice(start, start + Costs.pageSize);

    container.innerHTML = `
      <table class="w-full text-sm">
        <thead><tr class="text-xs text-gray-500 border-b border-gray-800">
          <th class="text-left py-2 pr-4">Timestamp</th>
          <th class="text-left py-2 pr-4">API</th>
          <th class="text-left py-2 pr-4">Stage</th>
          <th class="text-left py-2 pr-4">Description</th>
          <th class="text-right py-2">Amount</th>
        </tr></thead>
        <tbody>
          ${pageItems.map(tx => `
            <tr class="border-b border-gray-800/50 hover:bg-gray-800/30">
              <td class="py-1.5 pr-4 text-xs text-gray-500">${App.escapeHtml((tx.timestamp || '').replace('T', ' ').slice(0, 19))}</td>
              <td class="py-1.5 pr-4 text-xs text-gray-400">${App.escapeHtml(tx.api || '')}</td>
              <td class="py-1.5 pr-4 text-xs text-gray-400">${App.escapeHtml(tx.stage || '')}</td>
              <td class="py-1.5 pr-4 text-xs text-gray-300">${App.escapeHtml(tx.description || '')}</td>
              <td class="py-1.5 text-xs text-right text-gray-300">${App.formatCost(tx.cost_usd)}</td>
            </tr>
          `).join('')}
        </tbody>
      </table>
    `;

    // Pagination
    const totalPages = Math.ceil(txs.length / Costs.pageSize);
    if (pagEl && totalPages > 1) {
      let pagHtml = '';
      for (let p = 0; p < totalPages; p++) {
        const active = p === Costs.page ? 'bg-babylon-900 text-babylon-300' : 'bg-gray-800 text-gray-400 hover:bg-gray-700';
        pagHtml += `<button onclick="Costs.page=${p};Costs._renderTransactions()" class="px-2 py-1 rounded text-xs ${active}">${p + 1}</button>`;
      }
      pagEl.innerHTML = pagHtml;
    } else if (pagEl) {
      pagEl.innerHTML = '';
    }
  },

  exportCSV() {
    const txs = Costs.filteredTx;
    if (txs.length === 0) {
      App.showToast('No transactions to export', 'warning');
      return;
    }

    const header = 'timestamp,api,stage,description,amount_usd\n';
    const rows = txs.map(tx =>
      `"${tx.timestamp || ''}","${tx.api || ''}","${tx.stage || ''}","${(tx.description || '').replace(/"/g, '""')}",${tx.cost_usd || 0}`
    ).join('\n');

    const blob = new Blob([header + rows], { type: 'text/csv' });
    const url = URL.createObjectURL(blob);
    const a = document.createElement('a');
    a.href = url;
    a.download = `costs_${Costs.slug}_${new Date().toISOString().slice(0, 10)}.csv`;
    a.click();
    URL.revokeObjectURL(url);
    App.showToast('CSV exported', 'success');
  },
};
