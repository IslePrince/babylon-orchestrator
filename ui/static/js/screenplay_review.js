/**
 * screenplay_review.js — Screenplay review with inline audio playback
 * per dialogue line. Allows user to confirm all recordings are correct
 * before advancing to cinematographer.
 */

const ScreenplayReview = {

  slug: null,
  chapters: [],
  chapterId: null,
  recordings: [],      // [{recording_id, character_id, text, duration_sec, direction, audio_url}]
  dialogueLines: [],   // [{index, character_id, text, recording}]  parsed from content
  currentIdx: -1,
  playingAll: false,
  currentAudio: null,

  init(slug) {
    ScreenplayReview.slug = slug;
    ScreenplayReview._loadChapters();
    ScreenplayReview._bindKeys();
  },

  async _loadChapters() {
    try {
      const status = await App.api('GET', `/api/${ScreenplayReview.slug}/status`);
      ScreenplayReview.chapters = status.chapters || [];
      const sel = document.getElementById('rv-chapter-select');
      sel.innerHTML = '<option value="">Select chapter...</option>' +
        ScreenplayReview.chapters.map(ch =>
          `<option value="${ch.chapter_id}">${App.escapeHtml(ch.title || ch.chapter_id)}</option>`
        ).join('');

      const urlChapter = new URLSearchParams(window.location.search).get('chapter');
      const target = urlChapter && ScreenplayReview.chapters.find(c => c.chapter_id === urlChapter)
        ? urlChapter
        : (ScreenplayReview.chapters.length > 0 ? ScreenplayReview.chapters[0].chapter_id : null);
      if (target) {
        sel.value = target;
        ScreenplayReview.load();
      }
    } catch (e) {
      App.showToast('Failed to load chapters', 'error');
    }
  },

  async load() {
    const chapterId = document.getElementById('rv-chapter-select').value;
    if (!chapterId) {
      document.getElementById('rv-content').innerHTML =
        '<div class="text-gray-600 text-center py-20">Select a chapter to review</div>';
      return;
    }

    ScreenplayReview.chapterId = chapterId;
    ScreenplayReview.stopAll();

    document.getElementById('rv-content').innerHTML =
      '<div class="flex items-center justify-center py-20"><div class="skeleton h-6 w-48"></div></div>';

    try {
      const data = await App.api('GET',
        `/api/${ScreenplayReview.slug}/chapter/${chapterId}/screenplay-review`);
      ScreenplayReview.recordings = data.recordings || [];
      ScreenplayReview._render(data.content);
      ScreenplayReview._updateStats();
    } catch (e) {
      document.getElementById('rv-content').innerHTML =
        `<div class="text-red-400 text-center py-20">Failed to load: ${App.escapeHtml(e.message || e)}</div>`;
    }
  },

  _render(markdown) {
    const container = document.getElementById('rv-content');
    const lines = markdown.split('\n');
    let html = '';
    let dialogueLines = [];
    let lineIdx = 0;

    // Character name pattern (same as backend)
    const charPattern = /^([A-Z][A-Z\s'.\\d]+?)\s*(?:\(CONT'?D\)|\(V\.O\.\)|\(O\.S\.\))?\s*$/;

    let i = 0;
    while (i < lines.length) {
      const line = lines[i];
      const trimmed = line.trim();

      // Empty line
      if (!trimmed) {
        html += '<div class="h-4"></div>';
        i++;
        continue;
      }

      // Scene headings (## or INT./EXT.)
      if (trimmed.startsWith('##')) {
        html += `<div class="text-gray-300 font-bold mt-6 mb-3 text-base">${App.escapeHtml(trimmed.replace(/^#+\s*/, ''))}</div>`;
        i++;
        continue;
      }

      // Strip bold markers for character detection
      let clean = trimmed;
      if (clean.startsWith('**') && clean.endsWith('**')) {
        clean = clean.slice(2, -2).trim();
      }

      // Skip transitions
      if (clean.startsWith('FADE') || clean.startsWith('CUT TO')) {
        html += `<div class="text-gray-500 text-right text-sm my-2">${App.escapeHtml(clean)}</div>`;
        i++;
        continue;
      }

      // Scene heading (INT./EXT.)
      if (clean.startsWith('INT.') || clean.startsWith('EXT.')) {
        html += `<div class="text-gray-300 font-bold mt-6 mb-3 underline">${App.escapeHtml(clean)}</div>`;
        i++;
        continue;
      }

      // Character name?
      const match = charPattern.exec(clean);
      if (match) {
        const charName = match[1].trim();
        const charId = charName.toLowerCase().replace(/\s+/g, '_').replace(/'/g, '');
        i++;

        // Optional parenthetical direction
        let direction = '';
        if (i < lines.length && lines[i].trim().startsWith('(')) {
          direction = lines[i].trim();
          html += `<div class="text-center mt-4 mb-0.5">
            <span class="text-gray-200 font-bold tracking-wider">${App.escapeHtml(charName)}</span>
          </div>`;
          html += `<div class="text-center text-gray-500 text-sm italic mb-1">${App.escapeHtml(direction)}</div>`;
          i++;
        } else {
          html += `<div class="text-center mt-4 mb-1">
            <span class="text-gray-200 font-bold tracking-wider">${App.escapeHtml(charName)}</span>
          </div>`;
        }

        // Collect dialogue text
        let textParts = [];
        while (i < lines.length && lines[i].trim()) {
          let dl = lines[i].trim();
          let dlClean = dl;
          if (dlClean.startsWith('**') && dlClean.endsWith('**')) {
            dlClean = dlClean.slice(2, -2).trim();
          }
          if (charPattern.test(dlClean) || dlClean.startsWith('INT.') ||
              dlClean.startsWith('EXT.') || dlClean.startsWith('FADE')) {
            break;
          }
          textParts.push(dl);
          i++;
        }

        if (textParts.length > 0) {
          const dialogueText = textParts.join(' ');

          // Find matching recording
          const recording = ScreenplayReview._findRecording(dialogueText, charId);
          const dlIndex = dialogueLines.length;
          dialogueLines.push({
            index: dlIndex,
            character_id: charId,
            text: dialogueText,
            recording: recording,
          });

          const hasAudio = recording && recording.audio_url;
          const statusIcon = hasAudio
            ? '<span class="text-green-400" title="Recorded">&#9679;</span>'
            : '<span class="text-red-400" title="Not recorded">&#9675;</span>';
          const durText = recording ? `${recording.duration_sec.toFixed(1)}s` : '';
          const playBtn = hasAudio
            ? `<button onclick="ScreenplayReview.playLine(${dlIndex})"
                 class="text-gray-400 hover:text-white transition-colors" title="Play">&#9654;</button>`
            : '';

          html += `<div id="rv-line-${dlIndex}"
                        class="dialogue-line mx-auto max-w-lg px-4 py-2 rounded cursor-pointer
                               hover:bg-gray-800/50 transition-colors flex items-start gap-2
                               ${ScreenplayReview.currentIdx === dlIndex ? 'bg-gray-800/70 ring-1 ring-babylon-500' : ''}"
                        onclick="ScreenplayReview.selectLine(${dlIndex})">
              <div class="flex-shrink-0 flex items-center gap-1 mt-0.5 text-sm">
                ${statusIcon}
                ${playBtn}
              </div>
              <div class="flex-1 text-gray-300 text-sm leading-relaxed">${App.escapeHtml(dialogueText)}</div>
              <div class="flex-shrink-0 text-[10px] text-gray-600 mt-0.5">${durText}</div>
            </div>`;
        }
      } else {
        // Action/narrative line
        html += `<div class="text-gray-500 text-sm my-1 px-4">${App.escapeHtml(trimmed)}</div>`;
        i++;
      }
    }

    container.innerHTML = html;
    ScreenplayReview.dialogueLines = dialogueLines;
    ScreenplayReview.currentIdx = -1;
  },

  _findRecording(dialogueText, charId) {
    // Match by first 100 chars of text (same as backend recordings_map key)
    const key = dialogueText.substring(0, 100);
    return ScreenplayReview.recordings.find(r =>
      r.text.substring(0, 100) === key && r.character_id === charId
    ) || ScreenplayReview.recordings.find(r =>
      r.text.substring(0, 100) === key
    ) || null;
  },

  _updateStats() {
    const dl = ScreenplayReview.dialogueLines;
    const total = dl.length;
    const recorded = dl.filter(d => d.recording && d.recording.audio_url).length;
    const totalDur = dl.reduce((s, d) => s + (d.recording ? d.recording.duration_sec : 0), 0);

    document.getElementById('rv-stat-total').textContent = total;
    document.getElementById('rv-stat-recorded').textContent = recorded;
    const mins = Math.floor(totalDur / 60);
    const secs = Math.round(totalDur % 60);
    document.getElementById('rv-stat-duration').textContent = `${mins}:${String(secs).padStart(2, '0')}`;

    const unrecorded = total - recorded;
    const warnEl = document.getElementById('rv-unrecorded-warning');
    if (unrecorded > 0) {
      warnEl.classList.remove('hidden');
      document.getElementById('rv-unrecorded-text').textContent =
        `${unrecorded} line${unrecorded > 1 ? 's' : ''} not yet recorded`;
    } else {
      warnEl.classList.add('hidden');
    }
  },

  selectLine(idx) {
    // Deselect previous
    if (ScreenplayReview.currentIdx >= 0) {
      const prev = document.getElementById(`rv-line-${ScreenplayReview.currentIdx}`);
      if (prev) {
        prev.classList.remove('bg-gray-800/70', 'ring-1', 'ring-babylon-500');
      }
    }
    ScreenplayReview.currentIdx = idx;
    const el = document.getElementById(`rv-line-${idx}`);
    if (el) {
      el.classList.add('bg-gray-800/70', 'ring-1', 'ring-babylon-500');
      el.scrollIntoView({ behavior: 'smooth', block: 'nearest' });
    }
  },

  playLine(idx) {
    ScreenplayReview._stopAudio();
    const dl = ScreenplayReview.dialogueLines[idx];
    if (!dl || !dl.recording || !dl.recording.audio_url) return;

    ScreenplayReview.selectLine(idx);
    const audio = new Audio(dl.recording.audio_url);
    ScreenplayReview.currentAudio = audio;

    audio.onended = () => {
      ScreenplayReview.currentAudio = null;
      if (ScreenplayReview.playingAll && idx + 1 < ScreenplayReview.dialogueLines.length) {
        // Find next line with audio
        let next = idx + 1;
        while (next < ScreenplayReview.dialogueLines.length) {
          const ndl = ScreenplayReview.dialogueLines[next];
          if (ndl.recording && ndl.recording.audio_url) {
            ScreenplayReview.playLine(next);
            return;
          }
          next++;
        }
        ScreenplayReview.playingAll = false;
      }
    };

    audio.play().catch(e => console.warn('Audio play failed:', e));
  },

  playAll() {
    ScreenplayReview.playingAll = true;
    // Find first line with audio
    for (let i = 0; i < ScreenplayReview.dialogueLines.length; i++) {
      const dl = ScreenplayReview.dialogueLines[i];
      if (dl.recording && dl.recording.audio_url) {
        ScreenplayReview.playLine(i);
        return;
      }
    }
    ScreenplayReview.playingAll = false;
    App.showToast('No recorded lines to play', 'warning');
  },

  _stopAudio() {
    if (ScreenplayReview.currentAudio) {
      ScreenplayReview.currentAudio.pause();
      ScreenplayReview.currentAudio = null;
    }
  },

  stopAll() {
    ScreenplayReview.playingAll = false;
    ScreenplayReview._stopAudio();
  },

  _bindKeys() {
    document.addEventListener('keydown', (e) => {
      if (e.target.tagName === 'INPUT' || e.target.tagName === 'TEXTAREA' || e.target.tagName === 'SELECT') return;

      if (e.key === 'ArrowDown' || e.key === 'ArrowRight') {
        e.preventDefault();
        const next = Math.min(ScreenplayReview.currentIdx + 1, ScreenplayReview.dialogueLines.length - 1);
        ScreenplayReview.selectLine(next);
      } else if (e.key === 'ArrowUp' || e.key === 'ArrowLeft') {
        e.preventDefault();
        const prev = Math.max(ScreenplayReview.currentIdx - 1, 0);
        ScreenplayReview.selectLine(prev);
      } else if (e.key === ' ') {
        e.preventDefault();
        if (ScreenplayReview.currentAudio && !ScreenplayReview.currentAudio.paused) {
          ScreenplayReview.stopAll();
        } else if (ScreenplayReview.currentIdx >= 0) {
          ScreenplayReview.playLine(ScreenplayReview.currentIdx);
        } else {
          ScreenplayReview.playAll();
        }
      } else if (e.key === 'Escape') {
        ScreenplayReview.stopAll();
      }
    });
  },

};
