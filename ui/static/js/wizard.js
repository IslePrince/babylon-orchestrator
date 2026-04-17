/**
 * wizard.js — 5-step new project creation wizard.
 *
 * Steps: Basics → World → Git → Budgets → Review & Create
 */

const Wizard = {

  step: 1,
  totalSteps: 5,

  next() {
    if (Wizard.step === 1 && !Wizard._validateBasics()) return;
    if (Wizard.step < Wizard.totalSteps) {
      Wizard.step++;
      Wizard._render();
    }
    if (Wizard.step === Wizard.totalSteps) {
      Wizard._buildReview();
    }
  },

  prev() {
    if (Wizard.step > 1) {
      Wizard.step--;
      Wizard._render();
    }
  },

  _render() {
    // Update step indicators
    document.querySelectorAll('.wizard-step').forEach(el => {
      const s = parseInt(el.dataset.step);
      el.classList.toggle('active', s === Wizard.step);
      el.classList.toggle('done', s < Wizard.step);
    });

    // Show/hide panels
    document.querySelectorAll('.wizard-panel').forEach(el => {
      el.classList.toggle('hidden', parseInt(el.dataset.panel) !== Wizard.step);
    });

    // Show/hide buttons
    document.getElementById('wiz-prev').classList.toggle('hidden', Wizard.step === 1);
    document.getElementById('wiz-next').classList.toggle('hidden', Wizard.step === Wizard.totalSteps);
    document.getElementById('wiz-create').classList.toggle('hidden', Wizard.step !== Wizard.totalSteps);
  },

  _validateBasics() {
    const slug = document.getElementById('wiz-slug').value.trim();
    if (!slug) {
      App.showToast('Project slug is required', 'warning');
      return false;
    }
    if (!/^[a-z0-9][a-z0-9-]*$/.test(slug)) {
      App.showToast('Slug must be lowercase letters, numbers, and hyphens', 'warning');
      return false;
    }
    return true;
  },

  _getFormData() {
    return {
      slug: document.getElementById('wiz-slug').value.trim(),
      display_name: document.getElementById('wiz-display-name').value.trim(),
      source_title: document.getElementById('wiz-source-title').value.trim(),
      source_author: document.getElementById('wiz-source-author').value.trim(),
      source_year: document.getElementById('wiz-source-year').value.trim(),
      copyright_status: document.getElementById('wiz-copyright').value,
      period: document.getElementById('wiz-period').value.trim(),
      location: document.getElementById('wiz-location').value.trim(),
      tone: document.getElementById('wiz-tone').value,
      visual_ref_1: document.getElementById('wiz-visual-ref-1').value.trim(),
      visual_ref_2: document.getElementById('wiz-visual-ref-2').value.trim(),
      git_remote_url: document.getElementById('wiz-git-remote').value.trim(),
      api_budgets: {
        claude: parseFloat(document.getElementById('wiz-budget-claude').value) || 0,
        elevenlabs: parseFloat(document.getElementById('wiz-budget-elevenlabs').value) || 0,
        meshy: parseFloat(document.getElementById('wiz-budget-meshy').value) || 0,
        cartwheel: parseFloat(document.getElementById('wiz-budget-cartwheel').value) || 0,
        google_imagen: parseFloat(document.getElementById('wiz-budget-google-imagen').value) || 0,
      },
      source_file: document.getElementById('wiz-source-file').value.trim(),
    };
  },

  _buildReview() {
    const data = Wizard._getFormData();
    const container = document.getElementById('wiz-review');
    if (!container) return;

    const rows = [
      ['Slug', data.slug],
      ['Display name', data.display_name || data.slug],
      ['Source', `${data.source_title} by ${data.source_author} (${data.source_year})`],
      ['Copyright', data.copyright_status],
      ['Period', data.period],
      ['Location', data.location],
      ['Tone', data.tone],
      ['Git remote', data.git_remote_url || '(none)'],
      ['Budgets', Object.entries(data.api_budgets).map(([k, v]) => `${k}: $${v}`).join(', ')],
    ];

    container.innerHTML = rows.map(([label, value]) => `
      <div class="flex">
        <span class="w-32 text-gray-500 flex-shrink-0">${App.escapeHtml(label)}</span>
        <span class="text-gray-300">${App.escapeHtml(value)}</span>
      </div>
    `).join('');
  },

  async create() {
    const data = Wizard._getFormData();

    try {
      const result = await App.api('POST', '/api/projects/new', data);
      App.showToast(`Project "${data.slug}" created!`, 'success');

      // Auto-run ingest if source file provided
      if (data.source_file) {
        App.showToast('Starting ingest...', 'info');
        setTimeout(() => {
          App.slug = data.slug;
          StageRunner.run('ingest', { source: data.source_file }).catch(() => {});
        }, 500);
      }

      // Navigate to the new project
      setTimeout(() => {
        window.location.href = `/project/${data.slug}/dashboard`;
      }, data.source_file ? 1500 : 500);

    } catch (e) {
      // Error toast already shown by api()
    }
  },
};
