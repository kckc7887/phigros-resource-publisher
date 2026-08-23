const $ = (id) => {
  const el = document.getElementById(id);
  if (!el) throw new Error(`页面缺少元素 #${id}，请强制刷新（Ctrl+F5）后重试`);
  return el;
};
const stages = [...document.querySelectorAll('[data-stage]')];
let pollTimer;
let uploadScopes = {};

const ACTION_HINTS = {
  parse: '解包整理到 work/latest，不连接对象存储。',
  full: '解包整理后，按所选范围上传到对象存储。',
  upload: '不重新解包；直接上传 work/latest 中已有结果的指定内容。',
};

const MODE_HINTS = {
  local: '选择本机已有 APK。',
  live: '从 TapTap 下载最新 APK 到 work/cache/latest-apk（只保留最后一次）。',
};

async function loadDefaults() {
  const defaults = await fetch('/api/defaults').then((response) => response.json());
  $('endpoint').value = defaults.endpoint || '';
  $('bucket').value = defaults.bucket || '';
  $('publicBase').value = defaults.publicBase || '';
  uploadScopes = defaults.uploadScopes || {};
  const scopeSelect = $('uploadScope');
  scopeSelect.innerHTML = '';
  for (const [value, label] of Object.entries(uploadScopes)) {
    const option = document.createElement('option');
    option.value = value;
    option.textContent = label;
    scopeSelect.appendChild(option);
  }
  if (!scopeSelect.value) scopeSelect.value = 'all';
}

function needsUpload() {
  const action = $('action').value;
  return action === 'full' || action === 'upload';
}

function syncFields() {
  const action = $('action').value;
  const mode = $('mode').value;
  $('actionHint').textContent = ACTION_HINTS[action] || '';
  $('modeHint').textContent = MODE_HINTS[mode] || '';
  $('sourceFields').hidden = action === 'upload';
  $('localFields').hidden = action === 'upload' || mode !== 'local';
  $('uploadFields').hidden = !needsUpload();
  const scope = $('uploadScope').value || 'all';
  $('scopeHint').textContent = scope === 'all'
    ? '全量上传成功后会清理桶内旧版本；局部上传不会删除其他对象。'
    : '局部上传只覆盖所选对象，不会清理桶内其他资源。';
}

function renderStatus(state) {
  $('message').textContent = state.error || state.message;
  $('progressText').textContent = `${Math.round(state.progress || 0)}%`;
  $('progressBar').style.width = `${state.progress || 0}%`;
  $('logs').textContent = state.logs?.length ? state.logs.join('\n') : '尚无日志。';
  $('logs').scrollTop = $('logs').scrollHeight;
  $('statusBadge').textContent = state.status.toUpperCase();
  $('statusBadge').className = `status ${state.status}`;
  $('start').disabled = state.status === 'running';
  stages.forEach((item) => item.classList.toggle('active', item.dataset.stage === state.stage));

  if (state.result) {
    const summary = {
      动作: state.result.action,
      APK来源: state.result.mode,
      上传范围: state.result.upload_scope || '（未上传）',
      游戏版本: state.result.game_version,
      最新版本: state.result.latest?.version ?? '（未查询）',
      下载探测: state.result.download_probe ? `HTTP ${state.result.download_probe.status}` : '（未探测）',
      APK: state.result.apk_path || '（仅上传）',
      资源数: state.result.release?.asset_count,
      物量表: state.result.release?.note_counts,
      整理目录: state.result.release?.release_root,
      上传结果: state.result.upload || '未执行',
    };
    $('result').textContent = JSON.stringify(summary, null, 2);
    $('result').hidden = false;
  }
}

async function poll() {
  try {
    const state = await fetch('/api/status', { cache: 'no-store' }).then((response) => response.json());
    renderStatus(state);
    if (state.status !== 'running') {
      clearInterval(pollTimer);
      pollTimer = undefined;
    }
  } catch (error) {
    $('message').textContent = `无法读取本地服务状态：${error}`;
  }
}

async function pickApk() {
  const response = await fetch('/api/pick-apk', { method: 'POST' });
  const body = await response.json();
  if (!response.ok) throw new Error(body.error || `HTTP ${response.status}`);
  $('apkPath').value = body.apkPath;
}

async function start() {
  const action = $('action').value;
  const mode = $('mode').value;
  const payload = { action, mode };
  if (action !== 'upload' && mode === 'local') {
    const apkPath = $('apkPath').value.trim();
    if (!apkPath) throw new Error('请先选择或填写本地 APK 路径');
    payload.apk_path = apkPath;
  }
  if (needsUpload()) {
    payload.s3 = {
      endpoint: $('endpoint').value.trim(),
      bucket: $('bucket').value.trim(),
      public_base: $('publicBase').value.trim(),
      access_key: $('accessKey').value,
      secret_key: $('secretKey').value,
      upload_scope: $('uploadScope').value || 'all',
    };
  }
  $('result').hidden = true;
  const response = await fetch('/api/start', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(payload),
  });
  const body = await response.json();
  if (!response.ok) throw new Error(body.error || `HTTP ${response.status}`);
  renderStatus({ status: 'running', stage: 'starting', message: '任务已启动', progress: 0, logs: [] });
  clearInterval(pollTimer);
  pollTimer = setInterval(poll, 1000);
  await poll();
}

$('action').addEventListener('change', syncFields);
$('mode').addEventListener('change', syncFields);
$('uploadScope').addEventListener('change', syncFields);
$('pickApk').addEventListener('click', () => pickApk().catch((error) => {
  $('message').textContent = error.message;
}));
$('start').addEventListener('click', () => start().catch((error) => {
  $('message').textContent = error.message;
  $('statusBadge').textContent = 'ERROR';
  $('statusBadge').className = 'status error';
}));

loadDefaults().then(() => {
  syncFields();
  return poll();
}).catch((error) => {
  $('message').textContent = `初始化失败：${error}`;
});
