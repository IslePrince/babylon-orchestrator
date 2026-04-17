/**
 * sse.js — Stage runner with SSE progress streaming + polling fallback.
 *
 * Usage:
 *   StageRunner.run('ingest', { source: '/path/to/file.txt', dry_run: true });
 */

const StageRunner = {

  activeJobs: {},  // job_id -> { stage, chapterId, startedAt, progress, message, eventSource, pollTimer }

  /**
   * Launch a stage and stream progress.
   * @param {string} stage   — stage name (ingest, screenplay, etc.)
   * @param {object} opts    — { chapter_id, scene_id, batch_id, character_id, source, dry_run }
   * @returns {Promise<object>} — resolves with final result on done, rejects on error
   */
  run(stage, opts = {}) {
    const slug = App.slug;
    if (!slug) {
      App.showToast('No project selected', 'error');
      return Promise.reject(new Error('No project selected'));
    }

    return new Promise(async (resolve, reject) => {
      try {
        // 1. POST to start the stage
        const body = { stage, ...opts };
        const resp = await App.api('POST', `/api/${slug}/stages/run`, body);
        const jobId = resp.job_id;

        // Track the job
        StageRunner.activeJobs[jobId] = {
          stage,
          chapterId: opts.chapter_id || null,
          startedAt: new Date(),
          progress: 0,
          message: 'Starting...',
          costSoFar: 0,
          eventSource: null,
          pollTimer: null,
        };
        StageRunner._renderActiveJobs();

        // 2. Open SSE stream
        StageRunner._connectSSE(jobId, stage, slug, resolve, reject);

      } catch (e) {
        App.showToast(`Failed to start ${stage}: ${e.message}`, 'error');
        reject(e);
      }
    });
  },

  /**
   * Connect EventSource for a job. On error, falls back to polling.
   */
  _connectSSE(jobId, stage, slug, resolve, reject) {
    const es = new EventSource(`/api/stream/${jobId}`);
    const job = StageRunner.activeJobs[jobId];
    if (!job) return;
    job.eventSource = es;

    es.onmessage = (event) => {
      let data;
      try {
        data = JSON.parse(event.data);
      } catch (e) {
        return;
      }

      // Heartbeat — ignore
      if (data.heartbeat) return;

      // Progress update
      if (data.progress !== undefined) {
        const j = StageRunner.activeJobs[jobId];
        if (j) {
          j.progress = data.progress;
          j.message = data.message || '';
          j.costSoFar = data.cost_so_far || 0;
          StageRunner._renderActiveJobs();
        }
      }

      // Done
      if (data.done) {
        es.close();
        StageRunner._handleDone(jobId, stage, slug, data, resolve, reject);
      }
    };

    es.onerror = () => {
      es.close();
      const j = StageRunner.activeJobs[jobId];
      if (j) j.eventSource = null;

      // Don't give up — fall back to polling the job status
      StageRunner._startPolling(jobId, stage, slug, resolve, reject);
    };
  },

  /**
   * Poll /api/jobs/<job_id> as SSE fallback.
   */
  _startPolling(jobId, stage, slug, resolve, reject) {
    const job = StageRunner.activeJobs[jobId];
    if (!job) return;

    // Avoid duplicate poll timers
    if (job.pollTimer) return;

    job.message = 'Reconnecting...';
    StageRunner._renderActiveJobs();

    const poll = async () => {
      try {
        const data = await App.api('GET', `/api/jobs/${jobId}`);

        if (data.status === 'complete') {
          StageRunner._handleDone(jobId, stage, slug, {
            done: true,
            status: 'complete',
            result: data.result || {},
            auto_advance_job_id: data.auto_advance_job_id,
            auto_advance_stage: data.auto_advance_stage,
          }, resolve, reject);
        } else if (data.status === 'error') {
          StageRunner._handleDone(jobId, stage, slug, {
            done: true,
            status: 'error',
            error: data.error || 'unknown error',
          }, resolve, reject);
        } else {
          // Still running — update progress from server and poll again
          const j = StageRunner.activeJobs[jobId];
          if (j) {
            j.progress = data.progress || j.progress || 0;
            j.message = data.message || 'Running...';
            j.costSoFar = data.cost_so_far || j.costSoFar || 0;
            j.pollTimer = setTimeout(poll, 3000);
            StageRunner._renderActiveJobs();
          }
        }
      } catch (e) {
        // Server unreachable — give up after this
        StageRunner._markDone(jobId, 'error');
        App.showToast(`Lost connection to ${stage} job`, 'error');
        reject(new Error('Connection lost'));
      }
    };

    job.pollTimer = setTimeout(poll, 2000);
  },

  /**
   * Handle a completed/failed job (from SSE or polling).
   */
  _handleDone(jobId, stage, slug, data, resolve, reject) {
    // Clear poll timer if active
    const job = StageRunner.activeJobs[jobId];
    if (job && job.pollTimer) {
      clearTimeout(job.pollTimer);
      job.pollTimer = null;
    }
    if (job && job.eventSource) {
      job.eventSource.close();
      job.eventSource = null;
    }

    if (data.status === 'complete') {
      StageRunner._markDone(jobId, 'complete');
      const result = data.result || {};
      const skipped = result.skipped || 0;
      const skippedChars = result.skipped_characters || [];
      if (skipped > 0 && skippedChars.length > 0) {
        App.showToast(`${stage} complete — ${skipped} lines skipped (no voice: ${skippedChars.join(', ')}). Assign voices and re-run.`, 'warning');
      } else {
        App.showToast(`${stage} complete`, 'success');
      }

      // Small delay before refreshing — lets the server-side SSE
      // connection fully close so it's not blocking other requests
      setTimeout(() => {
        if (typeof Dashboard !== 'undefined') Dashboard.load(slug);
        App.loadRightPanel(slug);
      }, 500);

      // Track auto-advanced job if present
      if (data.auto_advance_job_id && data.auto_advance_stage) {
        StageRunner._trackAutoAdvance(
          data.auto_advance_job_id,
          data.auto_advance_stage,
          slug
        );
      }

      resolve(data.result || {});
    } else {
      StageRunner._markDone(jobId, 'error');
      App.showToast(`${stage} failed: ${data.error || 'unknown error'}`, 'error');
      reject(new Error(data.error || 'Stage failed'));
    }
  },

  /**
   * Subscribe to an auto-advanced job (spawned server-side).
   */
  _trackAutoAdvance(jobId, stage, slug) {
    App.showToast(`Auto-advancing to ${stage}...`, 'info');

    StageRunner.activeJobs[jobId] = {
      stage,
      chapterId: null,
      startedAt: new Date(),
      progress: 0,
      message: 'Auto-advanced...',
      costSoFar: 0,
      eventSource: null,
      pollTimer: null,
    };
    StageRunner._renderActiveJobs();

    // Try SSE first, falls back to polling
    StageRunner._connectSSE(jobId, stage, slug,
      () => {},  // resolve
      () => {},  // reject
    );
  },

  /**
   * Mark a job as done and schedule cleanup.
   */
  _markDone(jobId, status) {
    const job = StageRunner.activeJobs[jobId];
    if (job) {
      job.progress = status === 'complete' ? 100 : job.progress;
      job.status = status;
      if (job.pollTimer) {
        clearTimeout(job.pollTimer);
        job.pollTimer = null;
      }
      StageRunner._renderActiveJobs();
      // Remove from active after a delay
      setTimeout(() => {
        delete StageRunner.activeJobs[jobId];
        StageRunner._renderActiveJobs();
      }, 8000);
    }
  },

  /**
   * Render the active jobs panel in the right sidebar.
   */
  _renderActiveJobs() {
    const container = document.getElementById('active-jobs');
    if (!container) return;

    const jobs = Object.entries(StageRunner.activeJobs);
    if (jobs.length === 0) {
      container.innerHTML = '<div class="text-xs text-gray-600">No active jobs</div>';
      return;
    }

    container.innerHTML = jobs.map(([jobId, job]) => {
      const stageName = (job.stage || '').replace(/_/g, ' ');
      const chapterLabel = job.chapterId ? ` (${job.chapterId})` : '';
      const isDone = job.status === 'complete' || job.status === 'error';
      const barColor = job.status === 'error' ? 'bg-red-500' : 'bg-babylon-500';
      const statusIcon = job.status === 'complete' ? '&#10003;'
        : job.status === 'error' ? '&#10007;'
        : '';

      return `
        <div class="mb-3">
          <div class="flex items-center justify-between text-xs mb-1">
            <span class="text-gray-300">${statusIcon} ${App.escapeHtml(stageName)}${App.escapeHtml(chapterLabel)}</span>
            <span class="text-gray-500">${job.progress}%</span>
          </div>
          <div class="progress-bar">
            <div class="${barColor} progress-fill ${isDone ? '' : 'progress-active'}"
                 style="width:${job.progress}%"></div>
          </div>
          <div class="text-xs text-gray-600 mt-1 truncate">${App.escapeHtml(job.message || '')}</div>
          ${job.costSoFar > 0 ? `<div class="text-xs text-gray-600">${App.formatCost(job.costSoFar)}</div>` : ''}
        </div>
      `;
    }).join('');
  },

  /**
   * Reconnect to any running jobs after page navigation.
   * Fetches active jobs from the server and starts polling for each.
   */
  async reconnect(slug) {
    try {
      const jobs = await App.api('GET', `/api/${slug}/jobs/active`);
      if (!jobs || jobs.length === 0) return;

      for (const job of jobs) {
        const jobId = job.job_id;
        // Skip if already tracking this job
        if (StageRunner.activeJobs[jobId]) continue;

        StageRunner.activeJobs[jobId] = {
          stage: job.stage,
          chapterId: job.chapter_id || null,
          startedAt: new Date(job.started_at),
          progress: 0,
          message: 'Reconnecting...',
          costSoFar: 0,
          eventSource: null,
          pollTimer: null,
        };

        // Use polling (SSE queue events are already consumed by the original connection)
        StageRunner._startPolling(jobId, job.stage, slug, () => {}, () => {});
      }

      StageRunner._renderActiveJobs();
    } catch (e) {
      // Silently fail — no active jobs to reconnect to
    }
  },

  /**
   * Cancel a running job (closes SSE + stops polling).
   */
  cancel(jobId) {
    const job = StageRunner.activeJobs[jobId];
    if (job) {
      if (job.eventSource) job.eventSource.close();
      if (job.pollTimer) clearTimeout(job.pollTimer);
    }
    delete StageRunner.activeJobs[jobId];
    StageRunner._renderActiveJobs();
  },
};
