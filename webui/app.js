const form = document.getElementById('download-form');
const urlInput = document.getElementById('url-input');
const submitBtn = document.getElementById('submit-btn');
const preview = document.getElementById('preview');
const previewThumb = document.getElementById('preview-thumb');
const previewTitle = document.getElementById('preview-title');
const previewSub = document.getElementById('preview-sub');
const qualityRow = document.getElementById('quality-row');
const qualitySelect = document.getElementById('quality-select');
const embedCheckbox = document.getElementById('embed-checkbox');
const localOnlyCheckbox = document.getElementById('local-only-checkbox');
const jobBox = document.getElementById('job');
const progressBar = document.getElementById('progress-bar');
const jobStatus = document.getElementById('job-status');
const jobLog = document.getElementById('job-log');
const resultLink = document.getElementById('result-link');

let probeTimer = null;
let pollTimer = null;

function resetQualityOptions() {
  qualitySelect.innerHTML = '<option value="">Best available</option>';
}

function updateQualityVisibility() {
  const mode = form.querySelector('input[name="mode"]:checked').value;
  qualityRow.classList.toggle('hidden', mode !== 'video');
}

form.querySelectorAll('input[name="mode"]').forEach((el) => {
  el.addEventListener('change', updateQualityVisibility);
});
updateQualityVisibility();

urlInput.addEventListener('input', () => {
  clearTimeout(probeTimer);
  const url = urlInput.value.trim();
  resetQualityOptions();
  if (!url) {
    preview.classList.add('hidden');
    return;
  }
  probeTimer = setTimeout(() => probeUrl(url), 600);
});

async function probeUrl(url) {
  try {
    const res = await fetch('/api/probe', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ url }),
    });
    const data = await res.json();
    if (!res.ok || data.error) return;
    previewThumb.src = data.thumbnail || '';
    previewTitle.textContent = data.title || url;
    previewSub.textContent = [data.uploader, data.duration_string].filter(Boolean).join(' · ');
    preview.classList.remove('hidden');

    resetQualityOptions();
    (data.heights || []).forEach((h) => {
      const opt = document.createElement('option');
      opt.value = String(h);
      opt.textContent = `${h}p`;
      qualitySelect.appendChild(opt);
    });
  } catch (_) {
    // silently ignore preview failures
  }
}

form.addEventListener('submit', async (e) => {
  e.preventDefault();
  const url = urlInput.value.trim();
  const mode = form.querySelector('input[name="mode"]:checked').value;
  if (!url) return;

  submitBtn.disabled = true;
  submitBtn.textContent = 'Starting...';
  jobBox.classList.remove('hidden');
  resultLink.classList.add('hidden');
  progressBar.style.width = '0%';
  progressBar.classList.remove('processing');
  jobLog.textContent = '';
  jobStatus.textContent = 'Queuing download…';

  try {
    const res = await fetch('/api/download', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        url,
        mode,
        quality: mode === 'video' ? qualitySelect.value : '',
        embed: embedCheckbox.checked,
        save_to_nas: !localOnlyCheckbox.checked,
      }),
    });
    const data = await res.json();
    if (!res.ok || data.error) {
      jobStatus.textContent = `Error: ${data.error || 'failed to start'}`;
      resetButton();
      return;
    }
    pollJob(data.job_id);
  } catch (err) {
    jobStatus.textContent = `Error: ${err.message}`;
    resetButton();
  }
});

function pollJob(jobId) {
  clearInterval(pollTimer);
  pollTimer = setInterval(async () => {
    try {
      const res = await fetch(`/api/status?job_id=${encodeURIComponent(jobId)}`);
      const data = await res.json();
      if (!res.ok) return;

      progressBar.style.width = `${data.progress || 0}%`;
      jobLog.textContent = (data.lines || []).join('\n');
      jobLog.scrollTop = jobLog.scrollHeight;

      if (data.status === 'running' || data.status === 'queued') {
        const processing = data.stage && data.stage !== 'downloading';
        progressBar.classList.toggle('processing', processing);
        jobStatus.textContent = processing
          ? `${data.stage}…`
          : `Downloading… ${data.progress ? data.progress.toFixed(1) + '%' : ''}`;
      } else if (data.status === 'finished') {
        progressBar.classList.remove('processing');
        progressBar.style.width = '100%';
        if (data.filename) {
          const url = data.save_to_nas
            ? `/downloads/${encodeURIComponent(data.subfolder)}/${encodeURIComponent(data.filename)}`
            : `/temp-downloads/${encodeURIComponent(jobId)}/${encodeURIComponent(data.filename)}`;
          resultLink.href = url;
          resultLink.textContent = `Save ${data.filename}`;
          resultLink.classList.remove('hidden');
          jobStatus.textContent = data.save_to_nas ? 'Done' : 'Done — starting your download…';
          if (!data.save_to_nas) {
            resultLink.click();
          }
        } else {
          jobStatus.textContent = 'Done (already downloaded previously)';
        }
        clearInterval(pollTimer);
        resetButton();
      } else if (data.status === 'error') {
        jobStatus.textContent = `Error: ${data.error || 'download failed'}`;
        clearInterval(pollTimer);
        resetButton();
      }
    } catch (_) {
      // keep polling; transient network hiccup
    }
  }, 1000);
}

function resetButton() {
  submitBtn.disabled = false;
  submitBtn.textContent = 'Download';
}
