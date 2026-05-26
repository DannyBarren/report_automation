/**
 * field_recorder.js — User-driven guided field recording (template JSON = source of truth).
 *
 * Step progression is controlled only by the Next Step button (+ backend session).
 * “Mark this.” auto-advance is disabled during recording (post-processing only).
 */
(function (global) {
  'use strict';

  var C = global.JOBDOC_REC || {};
  var identificationPhrase = C.identificationPhrase || 'Mark this.';
  var uploadUrl = C.uploadUrl || '/upload_video';

  var DRAFT_DB = 'jobdoc_field_drafts';
  var DRAFT_STORE = 'recordings';
  var DRAFT_KEY = 'latest';

  var state = {
    sessionId: null,
    currentStep: null,
    sessionMeta: null,
    marksDetected: 0,
    paused: false,
    recording: false,
    highVis: false,
    chunks: [],
    stream: null,
    mediaRecorder: null,
    selectedMime: '',
    startedAt: 0,
    pauseStartedAt: 0,
    totalPausedMs: 0,
    pendingBlob: null,
    draftDb: null,
    advancing: false,
  };

  var els = {};

  function $(id) { return document.getElementById(id); }

  function reportTypesFromDom() {
    if (C.reportTypes && C.reportTypes.length) return C.reportTypes;
    var out = [];
    document.querySelectorAll('[data-report-type]').forEach(function (inp) {
      out.push(inp.getAttribute('data-report-type'));
    });
    return out;
  }

  function init() {
    els = {
      root: $('field-rec-root'),
      dock: $('guided-dock'),
      dockProgress: $('dock-progress'),
      dockTitle: $('dock-title'),
      dockScreen: $('dock-screen'),
      dockVoice: $('dock-voice'),
      progressFill: $('step-progress-fill'),
      markReminder: $('overlay-mark-cue'),
      videoWrap: $('video-stage'),
      preview: $('rec-preview-video'),
      overlayScreen: $('overlay-screen-text'),
      lamp: $('rec-lamp'),
      lampLabel: $('rec-lamp-label'),
      timer: $('rec-timer'),
      msg: $('rec-msg'),
      walkHint: $('walk-hint'),
      uploadBar: $('upload-progress'),
      uploadFill: $('upload-progress-fill'),
      btnCam: $('btn-request-permission'),
      btnStart: $('btn-start'),
      btnPause: $('btn-pause'),
      btnStop: $('btn-stop-upload'),
      btnSpeak: $('btn-speak-step'),
      btnNext: $('btn-next-step'),
      btnRetryUpload: $('btn-retry-upload'),
      btnHighVis: $('btn-high-vis'),
      checklist: $('section-checklist'),
      draftRestore: $('draft-restore'),
      btnRestoreDraft: $('btn-restore-draft'),
      btnDiscardDraft: $('btn-discard-draft'),
    };

    state.highVis = localStorage.getItem('jobdoc_high_vis') === '1';
    applyHighVis(state.highVis);
    if (els.btnHighVis) {
      els.btnHighVis.setAttribute('aria-pressed', state.highVis ? 'true' : 'false');
      els.btnHighVis.addEventListener('click', toggleHighVis);
    }

    bindControls();
    openDraftDb(function () { checkForLocalDraft(); });
    startGuidanceSession();

    if (!hasRecordingSupport()) {
      setLamp('error');
      setMessage('Recording not supported in this browser. Use Safari or Chrome on HTTPS.');
    } else if (!global.isSecureContext) {
      setLamp('error');
      setMessage('HTTPS required for camera and microphone on mobile.');
    }
  }

  function hasRecordingSupport() {
    return !!(navigator.mediaDevices && navigator.mediaDevices.getUserMedia && global.MediaRecorder);
  }

  function toggleHighVis() {
    state.highVis = !state.highVis;
    localStorage.setItem('jobdoc_high_vis', state.highVis ? '1' : '0');
    applyHighVis(state.highVis);
    if (els.btnHighVis) els.btnHighVis.setAttribute('aria-pressed', state.highVis ? 'true' : 'false');
  }

  function applyHighVis(on) {
    if (els.root) els.root.classList.toggle('high-vis', on);
  }

  function startGuidanceSession() {
    var types = reportTypesFromDom();
    if (!types.length) {
      fallbackClientPlan();
      return;
    }
    if (!C.sessionStartUrl) {
      fallbackClientPlan();
      return;
    }
    setMessage('Loading inspection steps from template…');
    fetch(C.sessionStartUrl, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ report_types: types }),
    })
      .then(function (r) { return r.json(); })
      .then(function (data) {
        if (!data.ok) {
          setMessage(data.error || 'Could not load guidance.');
          fallbackClientPlan();
          return;
        }
        state.sessionId = data.session_id;
        state.sessionMeta = data;
        applySessionPayload(data, false);
        setMessage('');
        if (data.intro_script) {
          setTimeout(function () { speakText(data.intro_script); }, 500);
        }
      })
      .catch(function (err) {
        setMessage('Network error loading steps — using offline template copy.');
        fallbackClientPlan();
      });
  }

  function fallbackClientPlan() {
    if (!C.guidancePlan || !C.guidancePlan.steps || !C.guidancePlan.steps.length) {
      setMessage('No guidance template loaded.');
      return;
    }
    var s = C.guidancePlan.steps[0];
    state.currentStep = {
      step_index: s.step_index,
      total_steps: s.total_steps,
      section_id: s.section_id,
      title: s.title,
      voice_prompt: s.voice_prompt,
      on_screen_text: s.on_screen_text,
      spoken_intro: s.spoken_intro,
      progress_label: 'Step ' + s.step_index + ' of ' + s.total_steps + ' — ' + s.title,
      is_last: s.step_index >= s.total_steps,
      can_advance: s.step_index < s.total_steps,
    };
    state.sessionMeta = { identification_phrase: C.guidancePlan.identification_phrase };
    renderCurrentStep(false);
  }

  function applySessionPayload(data, speak) {
    state.sessionMeta = data;
    if (data.identification_phrase) {
      identificationPhrase = data.identification_phrase;
    }
    state.currentStep = data.current_step;
    renderCurrentStep(speak);
  }

  function renderCurrentStep(speak) {
    var s = state.currentStep;
    if (!s || !els.dock) return;

    var label = s.progress_label || ('Step ' + s.step_index + ' of ' + s.total_steps + ' — ' + s.title);
    if (els.dockProgress) els.dockProgress.textContent = label;
    if (els.dockTitle) els.dockTitle.textContent = s.title;
    if (els.dockScreen) els.dockScreen.textContent = s.on_screen_text || s.title;
    if (els.dockVoice) els.dockVoice.textContent = s.voice_prompt || '';
    if (els.overlayScreen) els.overlayScreen.textContent = s.on_screen_text || s.title;

    if (els.progressFill && s.total_steps) {
      var pct = Math.round((s.step_index / s.total_steps) * 100);
      els.progressFill.style.width = Math.max(8, pct) + '%';
    }

    if (els.btnNext) {
      els.btnNext.disabled = !!s.is_last || state.advancing;
      els.btnNext.textContent = s.is_last ? 'Final step — finish & upload' : 'Next step →';
    }

    if (els.walkHint) {
      els.walkHint.textContent = s.is_last
        ? 'You are on the last section. Complete your narration, then stop and upload.'
        : 'Say “' + identificationPhrase + '”, narrate this area, then tap Next step when ready.';
    }

    updateChecklistHighlight(s.step_index);
    if (speak) speakText(s.spoken_intro || s.voice_prompt);
  }

  function updateChecklistHighlight(activeStepIndex) {
    if (!els.checklist) return;
    els.checklist.querySelectorAll('[data-step-index]').forEach(function (card) {
      var idx = parseInt(card.getAttribute('data-step-index'), 10);
      card.classList.toggle('current', idx === activeStepIndex);
      card.classList.toggle('done', idx < activeStepIndex);
    });
  }

  function flashMarkCue() {
    if (!els.markReminder) return;
    els.markReminder.classList.remove('flash');
    void els.markReminder.offsetWidth;
    els.markReminder.classList.add('flash');
    if (navigator.vibrate) navigator.vibrate([60, 30, 60]);
  }

  function speakText(text) {
    if (!text) return;
    if (!window.speechSynthesis) {
      setMessage('Voice unavailable — read the on-screen text.');
      return;
    }
    window.speechSynthesis.cancel();
    var u = new SpeechSynthesisUtterance(text);
    u.rate = state.highVis ? 0.88 : 0.95;
    u.volume = 1;
    window.speechSynthesis.speak(u);
  }

  function advanceViaServer() {
    if (!state.sessionId || !C.nextStepUrl) {
      setMessage('Session not ready.');
      return;
    }
    if (state.currentStep && state.currentStep.is_last) {
      setMessage('Final section — finish narrating, then upload.');
      return;
    }
    state.advancing = true;
    if (els.btnNext) els.btnNext.disabled = true;
    setMessage('Loading next section…');

    fetch(C.nextStepUrl, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ session_id: state.sessionId }),
    })
      .then(function (r) { return r.json(); })
      .then(function (data) {
        state.advancing = false;
        if (!data.ok) {
          setMessage(data.error || 'Could not advance.');
          if (els.btnNext) els.btnNext.disabled = false;
          return;
        }
        applySessionPayload(data, true);
        setMessage('Now at: ' + (data.current_step && data.current_step.title));
      })
      .catch(function () {
        state.advancing = false;
        setMessage('Network error — try Next step again.');
        if (els.btnNext) els.btnNext.disabled = false;
      });
  }

  function gotoStep(stepIndex) {
    if (!state.sessionId || !C.gotoStepUrl) return;
    fetch(C.gotoStepUrl, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ session_id: state.sessionId, step_index: stepIndex }),
    })
      .then(function (r) { return r.json(); })
      .then(function (data) {
        if (data.ok) applySessionPayload(data, true);
      });
  }

  function setMessage(t) { if (els.msg) els.msg.textContent = t || ''; }

  function setLamp(mode) {
    if (!els.lamp) return;
    els.lamp.classList.remove('recording', 'paused');
    if (mode === 'recording') { els.lamp.classList.add('recording'); els.lampLabel.textContent = 'Recording'; }
    else if (mode === 'paused') { els.lamp.classList.add('paused'); els.lampLabel.textContent = 'Paused'; }
    else if (mode === 'uploading') els.lampLabel.textContent = 'Uploading…';
    else if (mode === 'requesting') els.lampLabel.textContent = 'Starting…';
    else if (mode === 'error') els.lampLabel.textContent = 'Check device';
    else els.lampLabel.textContent = 'Ready';
  }

  function formatElapsed(ms) {
    var t = Math.floor(ms / 1000);
    var m = Math.floor(t / 60);
    var s = t % 60;
    return m + ':' + (s < 10 ? '0' : '') + s;
  }

  var timerHandle = null;
  function startTimer() {
    stopTimer();
    timerHandle = setInterval(function () {
      if (!state.recording || state.paused) return;
      var ms = Date.now() - state.startedAt - state.totalPausedMs;
      if (els.timer) els.timer.textContent = formatElapsed(ms);
    }, 400);
  }
  function stopTimer() { if (timerHandle) { clearInterval(timerHandle); timerHandle = null; } }

  function clearStream() {
    if (state.stream) {
      state.stream.getTracks().forEach(function (t) { t.stop(); });
      state.stream = null;
    }
    if (els.preview) els.preview.srcObject = null;
  }

  function getMime() {
    if (!MediaRecorder.isTypeSupported) return '';
    var c = ['video/mp4;codecs=h264,aac', 'video/mp4', 'video/webm;codecs=vp8,opus', 'video/webm'];
    for (var i = 0; i < c.length; i++) if (MediaRecorder.isTypeSupported(c[i])) return c[i];
    return '';
  }

  async function openCamera() {
    if (!hasRecordingSupport() || !global.isSecureContext) return false;
    setLamp('requesting');
    setMessage('Allow camera and microphone…');
    try {
      clearStream();
      var constraints = {
        video: { facingMode: { ideal: 'environment' }, width: { ideal: 1280 }, height: { ideal: 720 } },
        audio: { echoCancellation: true, noiseSuppression: true },
      };
      try {
        state.stream = await navigator.mediaDevices.getUserMedia(constraints);
      } catch (_e) {
        state.stream = await navigator.mediaDevices.getUserMedia({ video: true, audio: true });
      }
      els.preview.srcObject = state.stream;
      els.preview.muted = true;
      await els.preview.play().catch(function () {});
      setLamp('ready');
      setMessage('Camera ready. Tap Record, then use Next step between areas.');
      return true;
    } catch (err) {
      setLamp('error');
      setMessage(err.name === 'NotAllowedError' ? 'Permission denied — enable camera/mic in settings.' : String(err.message || err));
      return false;
    }
  }

  function startRecorder() {
    state.chunks = [];
    state.selectedMime = getMime();
    try {
      state.mediaRecorder = state.selectedMime
        ? new MediaRecorder(state.stream, { mimeType: state.selectedMime })
        : new MediaRecorder(state.stream);
    } catch (_e) {
      state.mediaRecorder = new MediaRecorder(state.stream);
    }
    state.mediaRecorder.ondataavailable = function (e) {
      if (e.data && e.data.size) state.chunks.push(e.data);
    };
    state.mediaRecorder.onstop = function () {
      stopTimer();
      var mime = state.mediaRecorder.mimeType || state.selectedMime || 'video/webm';
      state.pendingBlob = new Blob(state.chunks, { type: mime });
      uploadWithRetry(state.pendingBlob, mime);
    };
    state.mediaRecorder.start(1000);
    state.recording = true;
    state.paused = false;
    state.startedAt = Date.now();
    state.totalPausedMs = 0;
    setLamp('recording');
    setMessage('Recording — use Next step when you move to the next area.');
    startTimer();
    if (els.btnPause) els.btnPause.disabled = false;
    if (els.btnStop) els.btnStop.disabled = false;
    if (els.btnStart) els.btnStart.disabled = true;
    var s = state.currentStep;
    speakText((s && (s.spoken_intro || s.voice_prompt)) || ('Begin. Say ' + identificationPhrase + ' at each stop.'));
  }

  function pauseRecorder() {
    if (!state.mediaRecorder || state.mediaRecorder.state !== 'recording') return;
    state.mediaRecorder.pause();
    state.paused = true;
    state.pauseStartedAt = Date.now();
    setLamp('paused');
    setMessage('Paused — tap Resume when ready.');
    if (els.btnPause) els.btnPause.textContent = 'Resume';
  }

  function resumeRecorder() {
    if (!state.mediaRecorder || state.mediaRecorder.state !== 'paused') return;
    state.totalPausedMs += Date.now() - state.pauseStartedAt;
    state.mediaRecorder.resume();
    state.paused = false;
    setLamp('recording');
    setMessage('Recording resumed.');
    if (els.btnPause) els.btnPause.textContent = 'Pause';
  }

  function stopRecorder() {
    if (state.mediaRecorder && state.mediaRecorder.state !== 'inactive') {
      setLamp('uploading');
      setMessage('Stopping…');
      state.mediaRecorder.stop();
    }
    state.recording = false;
    if (els.btnStop) els.btnStop.disabled = true;
    if (els.btnPause) els.btnPause.disabled = true;
    if (state.sessionMeta && state.sessionMeta.outro_script) {
      speakText(state.sessionMeta.outro_script);
    }
  }

  function setUploadProgress(pct) {
    if (!els.uploadBar) return;
    els.uploadBar.hidden = pct < 0;
    if (els.uploadFill) els.uploadFill.style.width = Math.max(0, Math.min(100, pct)) + '%';
  }

  function uploadWithRetry(blob, mime, attempt) {
    attempt = attempt || 1;
    var maxAttempts = 4;
    setUploadProgress(5);
    setMessage('Uploading (attempt ' + attempt + '/' + maxAttempts + ')…');
    if (els.btnRetryUpload) els.btnRetryUpload.hidden = true;

    var fd = new FormData();
    var ext = (mime || '').indexOf('mp4') >= 0 ? 'mp4' : 'webm';
    fd.append('video', blob, 'field-' + Date.now() + '.' + ext);
    reportTypesFromDom().forEach(function (rt) { fd.append('report_types', rt); });

    var xhr = new XMLHttpRequest();
    xhr.open('POST', uploadUrl);
    xhr.setRequestHeader('X-JobDoc-Client', '1');
    xhr.timeout = 300000;

    xhr.upload.onprogress = function (ev) {
      if (ev.lengthComputable) {
        var pct = Math.round((ev.loaded / ev.total) * 100);
        setUploadProgress(pct);
        setMessage('Uploading… ' + pct + '%');
      }
    };

    xhr.onload = function () {
      var data;
      try { data = JSON.parse(xhr.responseText); } catch (_e) { data = null; }
      if (xhr.status >= 200 && xhr.status < 300 && data && data.ok && data.redirect_url) {
        setUploadProgress(100);
        setMessage('Upload complete!');
        clearLocalDraft();
        window.location.href = data.redirect_url;
        return;
      }
      retryOrFail(blob, mime, attempt, maxAttempts, 'Server rejected upload.');
    };
    xhr.onerror = function () { retryOrFail(blob, mime, attempt, maxAttempts, 'Network error.'); };
    xhr.ontimeout = function () { retryOrFail(blob, mime, attempt, maxAttempts, 'Upload timed out.'); };
    xhr.send(fd);
  }

  function openDraftDb(cb) {
    if (!global.indexedDB) { if (cb) cb(); return; }
    var req = indexedDB.open(DRAFT_DB, 1);
    req.onupgradeneeded = function (ev) {
      var db = ev.target.result;
      if (!db.objectStoreNames.contains(DRAFT_STORE)) db.createObjectStore(DRAFT_STORE);
    };
    req.onsuccess = function (ev) { state.draftDb = ev.target.result; if (cb) cb(); };
    req.onerror = function () { if (cb) cb(); };
  }

  function saveDraftLocally(blob, mime) {
    if (!state.draftDb) return;
    var reportTypes = reportTypesFromDom();
    try {
      var tx = state.draftDb.transaction(DRAFT_STORE, 'readwrite');
      tx.objectStore(DRAFT_STORE).put({ blob: blob, mime: mime, savedAt: new Date().toISOString(), reportTypes: reportTypes }, DRAFT_KEY);
      localStorage.setItem('jobdoc_has_draft', '1');
    } catch (_e) { /* */ }
  }

  function clearLocalDraft() {
    localStorage.removeItem('jobdoc_has_draft');
    if (!state.draftDb) return;
    try {
      var tx = state.draftDb.transaction(DRAFT_STORE, 'readwrite');
      tx.objectStore(DRAFT_STORE).delete(DRAFT_KEY);
    } catch (_e) { /* */ }
    if (els.draftRestore) els.draftRestore.hidden = true;
  }

  function checkForLocalDraft() {
    if (localStorage.getItem('jobdoc_has_draft') !== '1' || !state.draftDb) return;
    var tx = state.draftDb.transaction(DRAFT_STORE, 'readonly');
    var req = tx.objectStore(DRAFT_STORE).get(DRAFT_KEY);
    req.onsuccess = function () {
      if (req.result && req.result.blob && els.draftRestore) {
        els.draftRestore.hidden = false;
        state.pendingBlob = req.result.blob;
        if (els.btnRestoreDraft) {
          els.btnRestoreDraft.onclick = function () {
            uploadWithRetry(req.result.blob, req.result.mime || 'video/webm', 1);
          };
        }
        if (els.btnDiscardDraft) {
          els.btnDiscardDraft.onclick = function () {
            clearLocalDraft();
            setMessage('Draft discarded.');
          };
        }
      }
    };
  }

  function retryOrFail(blob, mime, attempt, maxAttempts, reason) {
    if (attempt < maxAttempts) {
      var delay = Math.min(8000, 1500 * attempt);
      setMessage(reason + ' Retrying in ' + Math.round(delay / 1000) + 's…');
      setTimeout(function () { uploadWithRetry(blob, mime, attempt + 1); }, delay);
      return;
    }
    saveDraftLocally(blob, mime);
    setUploadProgress(-1);
    setLamp('error');
    setMessage(reason + ' Recording saved on device — tap Retry upload when online.');
    if (els.btnRetryUpload) {
      els.btnRetryUpload.hidden = false;
      els.btnRetryUpload.onclick = function () { uploadWithRetry(blob, mime, 1); };
    }
    if (els.btnStart) els.btnStart.disabled = false;
    if (els.btnCam) els.btnCam.disabled = false;
    clearStream();
  }

  function runCountdown(cb) {
    var overlay = $('countdown-overlay');
    var num = $('countdown-num');
    if (!overlay || !num) { cb(); return; }
    var n = 3;
    overlay.classList.add('show');
    num.textContent = String(n);
    var iv = setInterval(function () {
      n -= 1;
      if (n <= 0) {
        clearInterval(iv);
        overlay.classList.remove('show');
        cb();
        return;
      }
      num.textContent = String(n);
    }, 900);
  }

  function bindControls() {
    if (els.btnCam) els.btnCam.addEventListener('click', function () { openCamera(); });
    if (els.btnStart) {
      els.btnStart.addEventListener('click', function () {
        runCountdown(async function () {
          if (!state.stream) {
            var ok = await openCamera();
            if (!ok) return;
          }
          startRecorder();
        });
      });
    }
    if (els.btnPause) {
      els.btnPause.addEventListener('click', function () {
        if (state.paused) resumeRecorder();
        else pauseRecorder();
      });
    }
    if (els.btnStop) els.btnStop.addEventListener('click', stopRecorder);
    if (els.btnSpeak) {
      els.btnSpeak.addEventListener('click', function () {
        var s = state.currentStep;
        if (s) speakText(s.spoken_intro || s.voice_prompt);
      });
    }
    if (els.btnNext) {
      els.btnNext.addEventListener('click', function () {
        if (state.currentStep && state.currentStep.is_last) {
          setMessage('Last section — tap ■ Upload when finished.');
          return;
        }
        advanceViaServer();
      });
    }
    document.querySelectorAll('[data-goto-step]').forEach(function (btn) {
      btn.addEventListener('click', function () {
        var i = parseInt(btn.getAttribute('data-goto-step'), 10);
        if (!isNaN(i)) gotoStep(i);
      });
    });
  }

  global.JobDocFieldRecorder = { init: init };
  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', init);
  } else {
    init();
  }
})(window);
