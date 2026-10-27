export default function(component) {
  const {data = {}, parentElement, setTriggerValue} = component;
  const app = parentElement.querySelector('[data-app]');
  if (!app) return;
  app._workbenchData = data;
  app._setTriggerValue = setTriggerValue;
  const rootKey = 'nongshi-workbench-ui-v1';
  let ui = app._workbenchUi;
  if (!ui) {
    try { ui = JSON.parse(localStorage.getItem(rootKey) || '{}'); } catch (_) { ui = {}; }
    app._workbenchUi = ui;
  }
  const escape = (value) => String(value ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
  const fmtDate = (iso, options = {month:'2-digit',day:'2-digit'}) => {
    if (!iso) return '日期待定';
    const d = new Date(`${iso}T12:00:00+08:00`);
    return new Intl.DateTimeFormat('zh-CN', options).format(d);
  };
  const weekDay = (iso) => '日一二三四五六'[new Date(`${iso}T12:00:00+08:00`).getDay()];
  const modeLabel = data.mode === 'demo' ? '独立模拟示例' : '真实地块';
  const field = data.field || null;
  const fieldKey = `${data.mode || 'real'}:${field?.id || 'blank'}`;
  const dates = data.days || [];
  const savedDate = ui.selected?.[fieldKey];
  let selectedDate = dates.some(d => d.date === savedDate)
    ? savedDate
    : dates.some(d => d.date === data.today) ? data.today : (dates[0]?.date || '');
  if (!ui.selected) ui.selected = {};
  ui.selected[fieldKey] = selectedDate;
  let tab = ui.tab || 'schedule';
  let weekOpen = Boolean(ui.weekOpen);
  let panel = ui.panel || '';
  const persistUI = () => { try { localStorage.setItem(rootKey, JSON.stringify(ui)); } catch (_) {} };
  if (ui.pendingOperationId && Date.now() - Number(ui.pendingAt || 0) > 60_000) {
    ui.pendingOperationId = ''; ui.pendingAt = 0; persistUI();
  }
  const setBusy = busy => app.querySelectorAll('button[type="submit"],button[data-action]').forEach(button => {
    if (busy) {
      if (button.dataset.actionWasDisabled === undefined) {
        button.dataset.actionWasDisabled = String(button.disabled);
      }
      button.disabled = true;
    }
    else if (button.dataset.actionWasDisabled !== undefined) { button.disabled = button.dataset.actionWasDisabled === 'true'; delete button.dataset.actionWasDisabled; }
  });
  const act = (kind, payload = {}, item = {}) => {
    const currentData = app._workbenchData || data;
    const currentField = currentData.field || null;
    if (ui.pendingOperationId && currentData.action_result?.operation_id === ui.pendingOperationId) {
      ui.pendingOperationId = ''; ui.pendingAt = 0; persistUI();
    }
    if (ui.pendingOperationId) return toast('上一项操作还在保存，请稍候片刻。');
    const operationId = crypto.randomUUID();
    ui.pendingOperationId = operationId; ui.pendingAt = Date.now(); persistUI(); setBusy(true);
    const action = {
      operation_id: operationId, kind,
      field_id: item.field_id || currentField?.id || currentData.new_field_id || 'field-pending',
      expected_draft_version: item.draft_version ?? currentField?.draft_version ?? 0,
      expected_fact_version: item.fact_version ?? currentField?.fact_version ?? 0,
      expected_task_version: item.task_version ?? currentField?.task_version ?? 0,
      expected_plan_id: item.plan_id ?? currentData.plan?.id ?? null,
      payload
    };
    (app._setTriggerValue || setTriggerValue)('action', action);
  };
  const toast = (text) => {
    const node = app.querySelector('[data-toast]');
    if (!node) return;
    node.textContent = text; node.dataset.show = 'true';
    clearTimeout(node._timer); node._timer = setTimeout(() => { node.dataset.show = 'false'; }, 3600);
  };
  const statusLabel = {scheduled:'已安排',completed:'已完成',pending:'待确认',unscheduled:'未排入',not_generated:'未生成',cancelled:'已取消'};
  const toneFor = status => status === 'scheduled' || status === 'completed' ? 'green' : status === 'pending' ? 'amber' : status === 'unscheduled' ? 'red' : 'neutral';
  const activePlan = data.preview || data.plan;
  const currentItems = activePlan?.items || data.items || [];
  const pendingCount = currentItems.filter(i => i.status === 'pending').length;
  const scheduledCount = currentItems.filter(i => i.status === 'scheduled').length;
  const unscheduledCount = currentItems.filter(i => i.status === 'unscheduled').length;
  const selectedDay = dates.find(d => d.date === selectedDate) || dates[0] || {date:'', items:[]};
  const allItems = data.items || currentItems;
  const renderStatus = (status) => `<span class="status-pill" data-tone="${toneFor(status)}">${escape(statusLabel[status] || status)}</span>`;
  const renderFieldRail = () => {
    app.querySelector('[data-mode]').textContent = modeLabel;
    const fieldsNode = app.querySelector('[data-fields]');
    fieldsNode.innerHTML = (data.fields || []).map(f => `<button class="field-option" data-action="select-field" data-field-id="${escape(f.id)}" aria-current="${f.id === field?.id}"><span class="field-glyph">田</span><span>${escape(f.name || '未命名地块')}</span></button>`).join('') +
      `<button class="field-option" data-action="new-field"><span class="field-glyph">＋</span><span>新增空白地块</span></button>`;
    const summary = app.querySelector('[data-field-summary]');
    if (!field?.profile) {
      summary.innerHTML = `<div class="field-onboarding-note"><span class="field-glyph">＋</span><strong>还没有已保存地块</strong><p>填写已确认的信息，未知内容可以留空。</p></div>`;
    } else {
      const p = field.profile;
      summary.innerHTML = `<h3>${escape(p.name)}</h3><p>${escape([p.province,p.crop,p.stage].filter(Boolean).join(' · ') || '基本情况还未补充')}</p><div class="field-meta"><span class="meta-pill">${escape(p.field_type_label || '田型未填写')}</span><span class="meta-pill">v${field.draft_version}</span></div><button class="text-button" data-action="profile">编辑地块资料　↗</button>`;
    }
    const resource = app.querySelector('[data-resource]');
    resource.innerHTML = dates.slice(0,4).map(day => `<div class="resource-line"><span>${fmtDate(day.date,{month:'numeric',day:'numeric'})}</span><span>${escape(day.resource_label || '时段待确认')}</span></div>`).join('') || '<span>保存地块后可设定时间</span>';
    const weather = app.querySelector('[data-weather]');
    weather.innerHTML = data.weather ? `<strong>${escape(data.weather.summary)}</strong><p>${escape(data.weather.coverage)} · ${escape(data.weather.freshness)}</p>` : '<span>尚无当前地点的七天预报</span>';
  };
  const renderDays = () => {
    app.querySelector('[data-days]').innerHTML = dates.map(day => {
      const n = day.items.filter(i => i.status === 'scheduled' || i.status === 'completed').length;
      const mins = day.items.filter(i => i.status === 'scheduled').reduce((sum,i) => sum + (i.duration || 0),0);
      const tag = day.workability === 'available' ? '可作业' : day.workability === 'unavailable' ? '现场条件受限' : '条件待确认';
      return `<button class="day-button" role="tab" aria-selected="${day.date === selectedDate}" data-action="select-date" data-date="${escape(day.date)}"><span class="day-week">周${weekDay(day.date)} · ${fmtDate(day.date,{month:'numeric',day:'numeric'})}</span><strong class="day-number">${escape(day.date.slice(-2))}</strong><span class="day-count">${n}项 · ${mins}分钟</span><span class="sr-only">${escape(tag)}</span></button>`;
    }).join('');
    const active = app.querySelector(`[data-date="${CSS.escape(selectedDate)}"]`);
    if (active && matchMedia('(max-width: 999px)').matches) active.scrollIntoView({block:'nearest',inline:'center'});
  };
  const renderTask = item => {
    const timeText = item.start ? `${escape(item.start.slice(11,16))}` : '时间待定';
    const range = item.start && item.end ? `${escape(item.start.slice(11,16))}–${escape(item.end.slice(11,16))}` : '';
    const purpose = item.purpose ? `本次目的：${escape(item.purpose)}` : '';
    const group = item.execution_group_id ? `<span class="task-group">合并出行 · 共 ${item.group_duration_minutes} 分钟</span>` : '';
    const readyToRecord = item.status === 'scheduled' && item.can_record;
    const mainAction = readyToRecord ? '记录结果' : item.status === 'completed' ? '查看记录' : '查看详情';
    const mainActionKind = readyToRecord ? 'open-result' : 'task-detail';
    return `<article class="task-card" data-tone="${toneFor(item.status)}"><div class="task-time"><strong>${range || timeText}</strong><small>${item.duration ? `${item.duration} 分钟` : escape(item.type_label || '农事任务')}</small></div><div class="task-body"><div class="task-title-row"><span class="task-title">${escape(item.title)}</span>${renderStatus(item.status)}</div><div class="task-purpose">${purpose}</div><div class="task-reason">${escape(item.reason || item.type_label || '')}</div>${group}</div><button class="task-main-action" data-action="${mainActionKind}" data-task-id="${escape(item.id)}">${mainAction}　›</button></article>`;
  };
  const renderPreview = () => {
    const node = app.querySelector('[data-preview]');
    if (!data.preview) { node.hidden = true; node.innerHTML = ''; return; }
    node.hidden = false;
    const changes = data.preview.changes || [];
    node.innerHTML = `<div class="preview-top"><div><span class="eyebrow">调整预览 · 尚未采用</span><h3>核对这次安排变化</h3><p class="muted-copy">地块 v${field?.draft_version} · 现场记录 v${field?.fact_version} · 任务 v${field?.task_version}</p></div><span class="status-pill" data-tone="amber">待你确认</span></div><div>${changes.length ? changes.map(c=>`<div class="preview-change"><strong>${escape(c.title)}</strong> · ${escape(c.kind_label)}：${escape(c.old_time || '未安排')} → ${escape(c.new_time || '未排入')}<br><span class="muted-copy">${escape(c.reason)}</span></div>`).join('') : '<p class="muted-copy">首次预览；请核对下方七天安排。</p>'}</div><div class="preview-actions"><button class="primary-button" data-action="adopt" data-preview-id="${escape(data.preview.id)}">采用这版安排</button><button class="outline-button" data-action="cancel-preview" data-preview-id="${escape(data.preview.id)}">暂不采用</button></div>`;
  };
  const renderSchedule = () => {
    app.querySelector('[data-field-title]').textContent = field?.profile?.name || '先从一块地开始';
    app.querySelector('[data-date-range]').textContent = data.date_range || '未来七天';
    const state = app.querySelector('[data-plan-status]');
    state.dataset.tone = data.preview ? 'amber' : data.plan ? (field?.plan_stale ? 'amber' : 'green') : 'neutral';
    state.innerHTML = `<i></i><span>${escape(data.preview ? '有调整预览待核对' : field?.plan_stale ? '地块情况变化，计划待更新' : data.plan ? `已采用 · V${data.plan.version}` : '尚无已采用安排')}</span>`;
    app.querySelector('[data-day-title]').textContent = selectedDay.date ? `${fmtDate(selectedDay.date,{month:'numeric',day:'numeric'})} · ${selectedDate === data.today ? '今天' : `周${weekDay(selectedDay.date)}`}安排` : '今日安排';
    app.querySelector('[data-day-subtitle]').textContent = selectedDay.workability_label || '现场与天气条件分别确认';
    const notice = app.querySelector('[data-notice]');
    notice.hidden = !data.notice;
    notice.textContent = data.notice || '';
    const dayItems = (selectedDay.items || []).slice().sort((a,b)=>(a.start||'').localeCompare(b.start||''));
    app.querySelector('[data-tasks]').innerHTML = !field?.profile ? `<div class="empty-state empty-first"><div class="empty-icon">田</div><h3>从一块地开始</h3><p>保存地块名称后，补充确认过的农情与时间，再查看七天安排。</p><button class="primary-button" data-action="profile">建立地块档案</button></div>` : dayItems.length ? dayItems.map(renderTask).join('') : `<div class="empty-state"><div class="empty-icon">☼</div><h3>${selectedDay.date === data.today ? '今天没有已安排事项' : '这一天没有已安排事项'}</h3><p>${escape(selectedDay.empty_reason || '待确认任务仍保留在下方；这里不会为了填满日期而生成安排。')}</p><button class="outline-button" data-action="schedule-reason">查看待确认与未排入</button></div>`;
    const pending = app.querySelector('[data-pending]');
    const pendingItems = allItems.filter(i => ['pending','unscheduled'].includes(i.status));
    pending.hidden = !pendingItems.length;
    pending.innerHTML = pendingItems.length ? `<h3>还需要核对的事项 · ${pendingItems.length}</h3>${pendingItems.slice(0,3).map(i=>`<div class="pending-row"><span>${escape(i.title)} · ${escape(statusLabel[i.status])}</span><span>${escape(i.reason || '查看缺少信息')}</span></div>`).join('')}${pendingItems.length>3?`<div class="muted-copy">还有 ${pendingItems.length-3} 项，展开整周清单查看</div>`:''}` : '';
    app.querySelector('[data-week]').hidden = !weekOpen;
    app.querySelector('[data-week]').innerHTML = weekOpen ? dates.map(d=>`<section class="week-day"><h3>${fmtDate(d.date,{month:'numeric',day:'numeric'})} · 周${weekDay(d.date)} <span class="muted-copy">${d.items.length}项</span></h3>${d.items.length?d.items.map(renderTask).join(''):`<p class="muted-copy">${escape(d.empty_reason || '暂无安排；不自动补任务。')}</p>`}</section>`).join('') : '';
    const weekButton = app.querySelector('[data-action="week"]');
    if (weekButton) weekButton.textContent = weekOpen ? '返回当天' : '整周清单';
    renderDays(); renderPreview();
  };
  const renderAssistant = () => {
    const context = app.querySelector('[data-assistant-context]');
    context.textContent = field?.profile ? `当前地块：${field.profile.name} · 已保存信息以现场确认内容为准` : '先保存一块地，助手就能结合当前安排帮你整理修改。';
    const messages = app.querySelector('[data-messages]');
    const rows = data.messages || [];
    messages.innerHTML = rows.length ? rows.slice(-18).map(m=>`<div class="message ${m.role === 'user' ? 'user' : 'assistant'}">${escape(m.content)}${m.time?`<span class="message-time">${escape(m.time)}</span>`:''}</div>`).join('') : `<div class="assistant-empty">${escape(field?.profile ? data.agent_busy ? '正在理解并核对信息…' : '告诉我现场变化或时间安排。我会先展示修改内容，确认后再保存。' : '保存地块后，可以用自然语言补充信息。')}</div>`;
    messages.scrollTop = messages.scrollHeight;
    const proposals = app.querySelector('[data-proposals]');
    proposals.innerHTML = (data.proposals || []).map(p=>`<section class="proposal-card"><h3>请核对这次修改</h3><div>${(p.rows||[]).map(row=>`<div class="proposal-change">${escape(row)}</div>`).join('')}</div><div class="proposal-buttons"><button class="primary-button" data-action="confirm-proposal" data-proposal-id="${escape(p.id)}">${p.confirm_label || '确认并保存'}</button><button class="outline-button" data-action="ignore-proposal" data-proposal-id="${escape(p.id)}">放弃</button></div></section>`).join('');
    app.querySelector('[data-agent-status]').innerHTML = data.agent_busy ? '<span class="loading-dot"></span>正在处理 · 仍可查看已保存安排' : data.agent_status ? escape(data.agent_status) : '自然语言内容由服务端核验；页面显示可追溯的工具结果。';
    const input = app.querySelector('#message-input');
    if (data.agent_busy) input.disabled = true; else input.disabled = !field?.profile;
  };
  const render = () => {
    let closeAfterDraft = false;
    let refreshWeatherPanel = false;
    if (ui.pendingOperationId && data.action_result) {
      if (data.action_result.operation_id === ui.pendingOperationId) {
        if (data.action_result.success) app._panelDirty = false;
        refreshWeatherPanel = Boolean(
          data.action_result.success && panel === 'weather' && app.querySelector('[data-dialog]').open
        );
        if (app._closeAfterDraft) {
          closeAfterDraft = Boolean(
            data.action_result.success && data.action_result.outcome === 'draft_saved'
          );
          app._closeAfterDraft = false;
        }
        ui.pendingOperationId = ''; ui.pendingAt = 0; persistUI();
      }
    }
    renderFieldRail(); renderSchedule(); renderAssistant();
    const layout = app.querySelector('.layout'); layout.dataset.active = tab;
    app.querySelectorAll('.mobile-nav button').forEach(b=>b.setAttribute('aria-current', b.dataset.action === `tab-${tab}` ? 'page' : 'false'));
    const toastText = data.action_result?.message || '';
    if (toastText) toast(toastText);
    setBusy(Boolean(ui.pendingOperationId));
    if (refreshWeatherPanel) {
      const body = app.querySelector('[data-dialog-body]');
      const query = body?.querySelector('[data-form="location-search"] input[name="query"]')?.value || '';
      showPanel('weather');
      const refreshedQuery = app.querySelector(
        '[data-dialog-body] [data-form="location-search"] input[name="query"]'
      );
      if (refreshedQuery) refreshedQuery.value = query;
    }
    if (closeAfterDraft) closePanel(true);
  };
  const selectMarkup = (name, label, choices, value='') => `<label class="form-field"><span>${escape(label)}</span><select name="${escape(name)}">${choices.map(([v,t])=>`<option value="${escape(v)}" ${String(v)===String(value)?'selected':''}>${escape(t)}</option>`).join('')}</select></label>`;
  const checkedMarkup = (name, label, value) => { const checked = value === true || value === 'true' || value === 'on' || value === 1; return `<label class="check-field"><input type="checkbox" name="${escape(name)}" value="true" ${checked?'checked':''}>${escape(label)}</label>`; };
  const profilePanel = () => {
    const p = field?.profile || {};
    const draft = data.editor_drafts?.profile?.payload || {};
    return `<form data-form="profile"><p class="muted-copy">先填写确定的信息。未知项留空，不会替你推断。</p><div class="form-grid"><label class="form-field full"><span>地块名称 *</span><input name="name" maxlength="80" required value="${escape(draft.name ?? p.name ?? '')}" placeholder="例如：北侧低洼田"></label><label class="form-field"><span>计划起始日</span><input name="start_date" type="date" min="${escape(data.today)}" value="${escape(draft.start_date ?? data.start_date)}"><small class="form-hint">默认从明天开始，也可从今天安排。调整日期不会自动搬动已确认的逐日条件。</small></label><label class="form-field"><span>省份</span><input name="province" value="${escape(draft.province ?? p.province ?? '')}" placeholder="可留空"></label>${checkedMarkup('province_confirmed','已核对省份',draft.province_confirmed ?? p.province_confirmed)}${selectMarkup('crop','作物',[['','未填写'],['油菜','油菜'],['其他作物','其他作物']],draft.crop ?? p.crop ?? '')}${checkedMarkup('crop_confirmed','已核对作物',draft.crop_confirmed ?? p.crop_confirmed)}${selectMarkup('stage','生育期',[['','未填写'],['苗期','苗期'],['其他生育期','其他生育期']],draft.stage ?? p.stage ?? '')}${checkedMarkup('stage_confirmed','已现场核对生育期',draft.stage_confirmed ?? p.stage_confirmed)}${selectMarkup('field_type','田型',[['','未填写'],['lowland_rice_stubble','低洼/稻茬田'],['dry_hilly','丘陵旱地'],['other','其他']],draft.field_type ?? p.field_type ?? '')}<label class="form-field"><span>面积（亩）</span><input name="area_mu" type="number" min="0.1" step="0.1" value="${escape(draft.area_mu ?? p.area_mu ?? '')}"></label><label class="form-field"><span>巡田需求</span><select name="inspection_requested"><option value="">暂不确定</option><option value="true" ${String(draft.inspection_requested ?? p.inspection_requested) === 'true'?'selected':''}>需要</option><option value="false" ${String(draft.inspection_requested ?? p.inspection_requested) === 'false'?'selected':''}>不需要</option></select></label><label class="form-field"><span>巡田预计时长（分钟）</span><input name="inspection_duration_minutes" type="number" min="1" max="1440" step="1" value="${escape(draft.inspection_duration_minutes ?? p.inspection_duration_minutes ?? '')}" placeholder="未知可留空"></label><label class="form-field"><span>维护预计时长（分钟）</span><input name="maintenance_duration_minutes" type="number" min="1" max="1440" step="1" value="${escape(draft.maintenance_duration_minutes ?? p.maintenance_duration_minutes ?? '')}" placeholder="未知可留空"></label><label class="form-field full"><span>备注</span><textarea name="notes" maxlength="1000">${escape(draft.notes ?? field?.notes ?? '')}</textarea></label></div><div class="dialog-actions"><button class="outline-button" type="submit" name="intent" value="draft">保存草稿</button><button class="primary-button" type="submit" name="intent" value="save">保存地块资料</button></div></form>`;
  };
  const boolOptions = [['','待确认'],['true','可作业'],['false','不可作业']];
  const schedulePanel = () => {
    const draftRows = data.editor_drafts?.schedule?.payload?.rows || [];
    const draftByDate = new Map(draftRows.map(row => [row.date, row]));
    return `<form data-form="schedule"><p class="muted-copy">逐日确认现场条件与人员时间；天气预报不会自动变成“可作业”。可用时段示例：09:00-11:00;13:00-15:00。</p><div class="schedule-grid">${dates.map(day=>{const d=draftByDate.get(day.date)||data.schedule?.[day.date]||{};return `<div class="schedule-day" data-day="${escape(day.date)}"><strong>${fmtDate(day.date,{month:'numeric',day:'numeric'})} 周${weekDay(day.date)}</strong><label>现场<select name="site_workable">${boolOptions.map(([v,t])=>`<option value="${v}" ${String(d.site_workable ?? '')===v?'selected':''}>${t}</option>`).join('')}</select></label><label>天气<select name="weather_workable">${boolOptions.map(([v,t])=>`<option value="${v}" ${String(d.weather_workable ?? '')===v?'selected':''}>${t}</option>`).join('')}</select></label><label>可用分钟<input name="resource_minutes" type="number" min="0" max="1440" step="15" value="${escape(d.resource_minutes ?? '')}" placeholder="未知"></label><label>具体时段<input name="intervals" placeholder="09:00-11:00" value="${escape(d.intervals || '')}"></label></div>`}).join('')}</div><div class="dialog-actions"><button class="outline-button" type="submit" name="intent" value="draft">保存草稿</button><button class="primary-button" type="submit" name="intent" value="save">保存七天条件</button></div></form>`;
  };
  const taskPanel = () => {
    const draft = data.editor_drafts?.task?.payload || {};
    return `<form data-form="task"><p class="muted-copy">这是用户自定义待办，农艺适用性未评估。巡田和沟渠维护请通过地块条件流程生成。</p><div class="form-grid"><label class="form-field full"><span>待办名称 *</span><input name="title" maxlength="80" required value="${escape(draft.title || '')}" placeholder="例如：清点农资"></label><label class="form-field full"><span>这项待办要完成什么 *</span><textarea name="purpose" required maxlength="400" placeholder="写清目的，不填写药肥处方">${escape(draft.purpose || '')}</textarea></label>${selectMarkup('category','事项类型',[['onsite','需要到现场'],['offsite','非现场']],draft.category || 'onsite')}<label class="form-field"><span>预计时长（分钟）*</span><input name="duration_minutes" type="number" min="1" step="1" required value="${escape(draft.duration_minutes || '30')}"></label>${selectMarkup('recurrence','重复方式',[['once','单次'],['dates','指定多个日期'],['interval','每隔若干天']],draft.recurrence || 'once')}<label class="form-field"><span>开始日期 *</span><input name="anchor_date" type="date" required value="${escape(draft.anchor_date || data.start_date || '')}"></label><label class="form-field"><span>最晚完成／结束日期 *</span><input name="due_date" type="date" required value="${escape(draft.due_date || data.start_date || '')}"></label><label class="form-field"><span>指定日期（逗号分隔）</span><input name="selected_dates" value="${escape(draft.selected_dates || '')}" placeholder="2026-11-01, 2026-11-04"></label><label class="form-field"><span>间隔天数</span><input name="interval_days" type="number" min="1" max="365" value="${escape(draft.interval_days || '')}" placeholder="仅间隔周期填写"></label><label class="form-field"><span>优先级</span><select name="priority"><option value="1" ${String(draft.priority || 2)==='1'?'selected':''}>高</option><option value="2" ${String(draft.priority || 2)==='2'?'selected':''}>普通</option><option value="3" ${String(draft.priority || 2)==='3'?'selected':''}>低</option></select></label><label class="form-field full"><span>完成标准 *</span><input name="completion_standard" maxlength="160" required value="${escape(draft.completion_standard || '')}" placeholder="例如：核对清单并记录结果"></label></div><div class="dialog-actions"><button class="outline-button" type="submit" name="intent" value="draft">保存草稿</button><button class="primary-button" type="submit" name="intent" value="save">保存并生成预览</button></div></form>`;
  };
  const taskManagementPanel = () => {
    const currentGroup = (data.execution_groups || []).find(g => g.id === ui.panel_subject && g.status !== 'cancelled');
    const taskRows = (data.group_candidates || []).map(t => `<label class="group-candidate"><input type="checkbox" name="member_task_ids" value="${escape(t.id)}" ${currentGroup?.member_task_ids?.includes(t.id)?'checked':''}><span><strong>${escape(t.title)}</strong><small>${escape(t.date_label)} · ${t.duration_minutes} 分钟 · ${escape(statusLabel[t.status] || t.status)}</small></span></label>`).join('');
    const seriesRows = (data.task_series || []).map(s => { const d=data.editor_drafts?.[`series:${s.id}`]?.payload || {}; return `<details class="series-editor"><summary><strong>${escape(s.title)}</strong><span class="meta-pill">${escape(s.recurrence_label)}</span></summary><form data-form="series-edit" data-series-id="${escape(s.id)}"><p class="muted-copy">只调整未完成的后续实例；已完成记录保留原样。保存后会生成新的安排预览。</p><input type="hidden" name="series_id" value="${escape(s.id)}"><div class="form-grid"><label class="form-field"><span>任务名称</span><input name="title" maxlength="80" required value="${escape(d.title ?? s.title)}"></label><label class="form-field"><span>目的</span><input name="purpose" maxlength="400" value="${escape(d.purpose ?? s.purpose)}"></label>${selectMarkup('recurrence','重复方式',[['once','单次'],['dates','指定多个日期'],['interval','每隔若干天']],d.recurrence ?? s.recurrence)}<label class="form-field"><span>起始日期</span><input name="anchor_date" type="date" required value="${escape(d.anchor_date ?? s.anchor_date)}"></label><label class="form-field"><span>最晚完成日期（单次）</span><input name="due_date" type="date" value="${escape(d.due_date ?? s.due_date)}"></label><label class="form-field"><span>周期结束日期</span><input name="end_date" type="date" value="${escape(d.end_date ?? s.end_date)}"></label><label class="form-field full"><span>指定日期（逗号分隔）</span><input name="selected_dates" value="${escape(d.selected_dates ?? s.selected_dates)}" placeholder="2026-10-29, 2026-11-01"></label><label class="form-field"><span>间隔天数</span><input name="interval_days" type="number" min="1" max="365" value="${escape(d.interval_days ?? s.interval_days)}"></label><label class="form-field"><span>每次时长（分钟）</span><input name="duration_minutes" type="number" min="1" max="1440" required value="${escape(d.duration_minutes ?? s.duration_minutes)}"></label><label class="form-field"><span>优先级</span><select name="priority"><option value="1" ${(d.priority ?? s.priority)===1?'selected':''}>高</option><option value="2" ${(d.priority ?? s.priority)===2?'selected':''}>普通</option><option value="3" ${(d.priority ?? s.priority)===3?'selected':''}>低</option></select></label><label class="form-field full"><span>完成标准</span><input name="completion_standard" maxlength="160" value="${escape(d.completion_standard ?? s.completion_standard)}"></label></div><div class="dialog-actions"><button class="outline-button" type="submit" name="intent" value="draft">保存草稿</button><button class="primary-button" type="submit" name="intent" value="save">保存后续设置并预览</button></div></form></details>`; }).join('');
    const groupOptions = (data.execution_groups || []).map(g=>`<article class="group-row" data-group-status="${escape(g.status)}"><div><strong>${escape(g.date_label)} · ${escape(g.member_names.join('、'))}</strong><p>${escape(g.status_label)} · 共 ${g.duration_minutes} 分钟</p></div>${g.status==='cancelled'?'<span class="status-pill" data-tone="neutral">已取消</span>':`<div class="group-row-actions"><button class="text-button" data-action="edit-group" data-group-id="${escape(g.id)}">调整</button><button class="text-button danger-text" data-action="cancel-group" data-group-id="${escape(g.id)}">取消</button></div>`}</article>`).join('');
    const outingDraft=data.editor_drafts?.[`outing:${currentGroup?.id || ''}`]?.payload || data.editor_drafts?.outing?.payload || {};
    const issueRows = (data.issues || []).map(issue => {
      const draft = data.editor_drafts?.[`follow_up:${issue.id}`]?.payload || {};
      const followUp = issue.status === 'open' ? `<details class="issue-followup"><summary>安排一次复查</summary><form data-form="follow-up" data-issue-id="${escape(issue.id)}"><p class="muted-copy">只确认复查日期、目的和时长；现场条件仍需逐日核对。</p><div class="form-grid"><label class="form-field full"><span>复查目的</span><input name="purpose" maxlength="400" required value="${escape(draft.purpose ?? `复查“${issue.title}”的当前情况`)}"></label><label class="form-field"><span>日期</span><select name="anchor_date">${dates.map(day => `<option value="${escape(day.date)}" ${(draft.anchor_date ?? dates[0]?.date) === day.date ? 'selected' : ''}>${fmtDate(day.date,{month:'numeric',day:'numeric'})} 周${weekDay(day.date)}</option>`).join('')}</select></label><label class="form-field"><span>预计时长（分钟）</span><input name="duration_minutes" type="number" min="1" max="1440" required value="${escape(draft.duration_minutes ?? '30')}"></label></div><div class="dialog-actions"><button class="outline-button" type="submit" name="intent" value="draft">保存草稿</button><button class="primary-button" type="submit" name="intent" value="save">保存并生成预览</button></div></form></details>` : '';
      return `<article class="issue-row"><div><strong>${escape(issue.title)} · ${escape(issue.status_label)}</strong><p>${escape(issue.description || '现场记录未补充说明')} · 关联 ${issue.related_task_count} 项任务</p>${followUp}</div></article>`;
    }).join('');
    return `<div><section class="management-section"><h3>周期设置</h3><p class="muted-copy">修改只影响未完成的后续任务。历史实例和执行结果保持不变。</p>${seriesRows || '<p class="empty-copy">当前没有可调整的任务周期。</p>'}</section><section class="management-section"><h3>合并同日出行</h3><p class="muted-copy">只可合并同一地块、同日且没有前置关系的现场巡田或复查。成员结果仍分别记录。</p><form data-form="outing" data-group-id="${escape(currentGroup?.id || '')}"><input type="hidden" name="group_id" value="${escape(currentGroup?.id || '')}"><div class="group-candidates">${taskRows || '<p class="empty-copy">未来七天没有符合条件的巡田或复查任务。</p>'}</div><div class="form-grid"><label class="form-field"><span>共同执行日期</span><select name="execution_date">${dates.map(d=>`<option value="${escape(d.date)}" ${(outingDraft.execution_date ?? currentGroup?.date)===d.date?'selected':''}>${fmtDate(d.date,{month:'numeric',day:'numeric'})} 周${weekDay(d.date)}</option>`).join('')}</select></label><label class="form-field"><span>确认总时长（分钟）</span><input name="duration_minutes" type="number" min="1" max="1440" required value="${escape(outingDraft.duration_minutes ?? currentGroup?.duration_minutes ?? '45')}"></label></div><div class="dialog-actions"><button class="outline-button" type="submit" name="intent" value="draft">保存草稿</button><button class="primary-button" type="submit" name="intent" value="save">确认合并并预览</button></div></form>${groupOptions || '<p class="empty-copy">尚无已确认的合并出行。</p>'}</section><section class="management-section"><h3>现场问题</h3>${issueRows || '<p class="empty-copy">尚未记录需跟进的问题。</p>'}</section></div>`;
  };
  const resultPanel = item => {
    const fields = item.result_fields?.length ? item.result_fields : ['任务结果'];
    const draft = data.editor_drafts?.[`result:${item.id}`]?.payload || {};
    const relatedIssue = item.type === 'follow_up'
      ? (data.issues || []).find(issue => issue.id === item.related_issue_id && issue.status === 'open')
      : null;
    const resolution = relatedIssue
      ? `<label class="form-field full"><span>问题状态</span><select name="resolve_issue"><option value="" ${draft.resolve_issue !== 'resolved'?'selected':''}>暂不确认已解决</option><option value="resolved" ${draft.resolve_issue === 'resolved'?'selected':''}>确认本次复查后已解决</option></select><small class="form-hint">须勾选任务已完成，且所有复查项目均为“正常”，才会关闭问题。</small></label>`
      : '';
    return `<form data-form="result" data-task-id="${escape(item.id)}"><p class="muted-copy">按实际观察记录。完成情况与现场发现分别保存。</p>${fields.map((name,index)=>{const value=draft[`result_${index}`]||'unknown';return `<div class="result-row"><label for="result-${index}">${escape(name)}</label><select id="result-${index}" name="result_${index}"><option value="unknown" ${value==='unknown'?'selected':''}>暂不确定</option><option value="normal" ${value==='normal'?'selected':''}>正常</option><option value="issue" ${value==='issue'?'selected':''}>发现问题</option><option value="unable" ${value==='unable'?'selected':''}>无法判断</option><option value="not_checked" ${value==='not_checked'?'selected':''}>未检查</option></select></div>`}).join('')}${resolution}<label class="form-field full" style="margin-top:12px"><span>现场补充</span><textarea name="notes" maxlength="800" placeholder="记录观察到的情况">${escape(draft.notes||'')}</textarea></label><label class="check-field"><input type="checkbox" name="completed" ${draft.completed?'checked':''}>本次任务已完成</label><div class="dialog-actions"><button class="outline-button" type="submit" name="intent" value="draft">保存草稿</button><button class="primary-button" type="submit" name="intent" value="save">保存现场结果</button></div></form>`;
  };
  const observationPanel = () => `<form data-form="observation"><p class="muted-copy">只记录你已经确认的现场情况。未知可以保持待确认。</p><div class="form-grid">${selectMarkup('field','观察项目',[['standing_water','积水情况'],['ditch_connectivity','沟渠连通'],['maintenance_need_confirmed','维护需求'],['maintenance_problem_confirmed','维护问题'],['maintenance_purpose','维护目的'],['moisture','土壤墒情'],['actual_rain_observed','实际降雨'],['pest_signs','虫害迹象'],['missing_seedlings','缺苗情况'],['crowding','拥挤情况']],'standing_water')}<label class="form-field"><span>记录值</span><select name="value"><option value="">暂不确定</option><option value="true">是 / 有</option><option value="false">否 / 没有</option><option value="clear">通畅</option><option value="impaired">受阻</option><option value="dry">偏干</option><option value="normal">适中</option><option value="wet">偏湿</option><option value="clear_existing_ditch">清理既有沟渠</option><option value="other">其他目的</option></select></label></div><div class="dialog-actions"><button class="primary-button" type="submit" name="intent" value="save">保存现场记录</button></div></form>`;
  const resourcePanel = () => { const draft=data.editor_drafts?.resource?.payload || {}; return `<form data-form="resource"><p class="muted-copy">只修改人员时间，不会把缺失的现场条件改成可作业。</p><div class="form-grid"><label class="form-field"><span>日期 *</span><select name="day">${dates.map(d=>`<option value="${escape(d.date)}" ${(draft.day ?? selectedDate)===d.date?'selected':''}>${fmtDate(d.date,{month:'numeric',day:'numeric'})} 周${weekDay(d.date)}</option>`).join('')}</select></label><label class="form-field"><span>可用分钟数</span><input name="resource_minutes" type="number" min="0" max="1440" step="15" value="${escape(draft.resource_minutes ?? '0')}"></label><label class="form-field full"><span>可用时段 *</span><input name="intervals" required value="${escape(draft.intervals || '')}" placeholder="09:00-11:00;13:00-15:00"></label></div><div class="dialog-actions"><button class="outline-button" type="submit" name="intent" value="draft">保存草稿</button><button class="primary-button" type="submit" name="intent" value="save">保存人员时间</button></div></form>`; };
  const weatherPanel = () => `<div><p class="muted-copy">先搜索并手动确认天气查询点。县级位置会标出查询级别；位置不能当作田块实测坐标。</p><form data-form="location-search" class="form-grid"><label class="form-field full"><span>地点或行政区</span><input name="query" required minlength="2" placeholder="例如：湖北省武汉市"></label><div class="dialog-actions form-field full"><button class="primary-button" type="submit">搜索地点</button></div></form>${(data.locations||[]).length?`<div class="location-list"><h3>选择一个查询点</h3>${data.locations.map(l=>`<button class="location-choice" data-action="choose-location" data-location-id="${escape(l.id)}">${escape(l.label)}<br><small>${escape(l.resolution_label)} · ${escape(l.source_label)}${l.match_reason?` · ${escape(l.match_reason)}`:''}</small></button>`).join('')}</div>`:''}${data.location?`<div class="evidence-row"><strong>已确认：${escape(data.location.display_name)}</strong><p>${escape(data.location.resolution_label)} · ${escape(data.location.geocoding_source)} · ${escape(data.location.coverage)}</p><button class="primary-button" data-action="fetch-weather">查询/刷新七天天气</button></div>`:''}${data.forecast?(data.forecast.days||[]).map(d=>`<div class="forecast-row"><strong>${fmtDate(d.day,{month:'numeric',day:'numeric'})}</strong><span>${escape(d.temperature)}</span><span>${escape(d.precipitation)}</span></div>`).join(''):'<p class="empty-copy">当前地点暂无覆盖所选计划日期的天气快照。</p>'}</div>`;
  const evidencePanel = () => `<div><p class="muted-copy">规则候选处于开发验证阶段。内容展示用途与限制，不替代田间确认。</p>${(data.evidence||[]).length?(data.evidence||[]).map(e=>`<article class="evidence-row"><strong>${escape(e.rule_id)} / ${escape(e.source_id)} · ${escape(e.title)}</strong><small>${escape(e.publisher)} · ${escape(e.date)} · ${escape(e.pages)} · ${escape(e.status)}</small><p>适用条件：${escape(e.applicability || '未提供')}</p><p>本项目用途：${escape(e.use || '未提供')}。限制：${escape(e.limits || '未提供')}</p>${e.quote?`<p>原文：${escape(e.quote)}</p>`:''}</article>`).join(''):'<p class="empty-copy">所选事项暂无可展示的直接依据；不据此推断处置方法。</p>'}</div>`;
  const historyPanel = () => `<div><p class="muted-copy">历史版本仅供查看。恢复时会按当前地块情况重新核对。</p>${(data.history||[]).length?(data.history||[]).map(h=>`<article class="evidence-row"><strong>V${h.version} · ${escape(h.created_at)}</strong><p>${escape(h.summary)}</p><small>${escape(h.changes||'七天安排快照')}</small></article>`).join(''):'<p class="empty-copy">当前还没有已采用的计划版本。</p>'}</div>`;
  const showUnsavedPrompt = () => {
    const body=app.querySelector('[data-dialog-body]');
    if(!body||body.querySelector('[data-unsaved-prompt]'))return;
    const form=body.querySelector('form[data-form]');
    const canSave=Boolean(form?.querySelector('button[name="intent"][value="draft"]'));
    body.insertAdjacentHTML('afterbegin',`<section class="unsaved-prompt" data-unsaved-prompt role="alertdialog" aria-label="未保存内容"><strong>这份内容还没有保存</strong><p>保存草稿后可在此地块继续填写；放弃后，这些输入不会进入计划或现场记录。</p><div class="unsaved-actions">${canSave?'<button class="primary-button" data-action="unsaved-save">保存草稿并关闭</button>':''}<button class="outline-button" data-action="unsaved-discard">放弃修改</button><button class="text-button" data-action="unsaved-continue">继续编辑</button></div></section>`);
  };
  const showPanel = (kind, itemId='') => {
    app._panelDirty=false;
    panel = kind; ui.panel = kind; ui.panel_subject = itemId; persistUI();
    const dialog = app.querySelector('[data-dialog]');
    const title = {profile:'地块资料',schedule:'七天作业条件与可用时间',task:'新增农事待办',observation:'记录现场情况',resource:'修改人员时间',weather:'地点与天气',evidence:'安排依据',history:'计划历史',export:'导出计划',detail:'任务详情',result:'记录执行结果',mode:'切换数据空间',help:'关于农时窗口'}[kind] || '农事窗口';
    app.querySelector('[data-dialog-eyebrow]').textContent = kind==='detail' ? '本次任务' : '农时窗口';
    app.querySelector('[data-dialog-title]').textContent = title;
    const item = allItems.find(i=>i.id===itemId) || (data.detail?.id===itemId?data.detail:null);
    let body = '';
    if (kind==='profile') body=profilePanel();
    else if (kind==='schedule') body=schedulePanel();
    else if (kind==='task') body=taskPanel();
    else if (kind==='management') body=taskManagementPanel();
    else if (kind==='result' && item) body=resultPanel(item);
    else if (kind==='observation') body=observationPanel();
    else if (kind==='resource') body=resourcePanel();
    else if (kind==='weather') body=weatherPanel();
    else if (kind==='evidence') body=evidencePanel();
    else if (kind==='history') body=historyPanel();
    else if (kind==='export') body=`<p class="muted-copy">文件内容来自服务端当前记录。</p><div class="dialog-actions"><button class="primary-button" data-action="download-json">下载 JSON</button><button class="outline-button" data-action="download-markdown">下载 Markdown 清单</button></div>`;
    else if (kind==='detail' && item) body=`<div class="task-detail"><span class="status-pill" data-tone="${toneFor(item.status)}">${escape(statusLabel[item.status])}</span><h3 style="margin:12px 0">${escape(item.title)}</h3><p class="muted-copy">${escape(item.date_label||'日期待确认')} · ${escape(item.time_label||'')}</p><article class="evidence-row"><strong>本次目的</strong><p>${escape(item.purpose||'当前没有单独填写目的')}</p></article>${item.steps?.length?`<article class="evidence-row"><strong>现场步骤</strong><p>${item.steps.map(escape).join('<br>')}</p></article>`:''}${item.result_fields?.length?`<article class="evidence-row"><strong>记录项目</strong><p>${item.result_fields.map(escape).join('、')}</p></article>`:''}${item.completion_standard?`<article class="evidence-row"><strong>完成标准</strong><p>${escape(item.completion_standard)}</p></article>`:''}<article class="evidence-row"><strong>安排原因</strong><p>${escape(item.reason||'已按当前条件计算')}</p></article>${item.evidence?.length?item.evidence.map(e=>`<article class="evidence-row"><strong>${escape(e.rule_id)}/${escape(e.source_id)} · ${escape(e.title)}</strong><p>${escape(e.use||'')} · ${escape(e.limits||'')}</p>${e.quote?`<small>${escape(e.quote)}</small>`:''}</article>`).join(''):'<p class="empty-copy">当前没有可核验的农业直接依据。</p>'}${item.status==='scheduled'&&item.can_record?`<button class="primary-button" data-action="open-result" data-task-id="${escape(item.id)}">记录这项任务的结果</button>`:''}${item.status==='scheduled'&&!item.completed?`<form data-form="cancel-task" data-task-id="${escape(item.id)}"><label class="form-field full"><span>取消本次的原因</span><textarea name="reason" required maxlength="300" placeholder="填写本次取消原因"></textarea></label><button class="outline-button danger-outline" type="submit">取消本次任务</button></form>`:''}</div>`;
    else if (kind==='mode') body=`<div class="form-grid"><button class="outline-button" data-action="switch-mode" data-mode-value="real">真实地块 · 保存你的地块与记录</button><button class="outline-button" data-action="switch-mode" data-mode-value="demo">独立模拟示例 · 不写入真实数据</button>${data.mode==='demo'?'<button class="outline-button" data-action="seed-demo" data-scenario="sparse">新建稀疏示例</button><button class="outline-button" data-action="seed-demo" data-scenario="rich">新建任务丰富示例</button>':''}</div>`;
    else if (kind==='help') body='<p class="muted-copy">Agent先整理修改供你核对；确认后保存事实并计算预览。采用安排需要你明确操作。天气预报用于风险提示，不代表现场作业确认。</p>';
    else body='<p class="empty-copy">请选择一项记录。</p>';
    app.querySelector('[data-dialog-body]').innerHTML = body;
    if (!dialog.open) dialog.showModal();
  };
  const closePanel = (force=false) => { if(!force&&app._panelDirty){showUnsavedPrompt();return;} const dialog=app.querySelector('[data-dialog]');app._panelDirty=false;if(dialog.open)dialog.close();panel='';ui.panel='';persistUI(); };
  const openLocal = action => {
    if(action==='mode-menu') return showPanel('mode');
    if(action==='profile'||action==='edit-profile') return showPanel('profile');
    if(action==='edit-schedule') return showPanel('schedule');
    if(action==='task') return showPanel('task');
    if(action==='task-management') return showPanel('management');
    if(action==='observation') return showPanel('observation');
    if(action==='resource') return showPanel('resource');
    if(action==='weather') return showPanel('weather');
    if(action==='evidence') return showPanel('evidence');
    if(action==='history') return showPanel('history');
    if(action==='export') return showPanel('export');
    if(action==='assistant-help') return showPanel('help');
  };
  const readForm = form => { const values = Object.fromEntries(new FormData(form).entries()); for (const input of form.querySelectorAll('input[type=checkbox][name]')) values[input.name] = input.checked; return values; };
  const download = (name, content, type) => { const a=document.createElement('a');a.href=URL.createObjectURL(new Blob([content],{type}));a.download=name;a.click();setTimeout(()=>URL.revokeObjectURL(a.href),500); };
  const submitForm = (form, event) => {
    event.preventDefault(); const kind=form.dataset.form; const values=readForm(form); const intent=event.submitter?.value || 'save';
    if(kind==='message'){ if(!String(values.message||'').trim())return toast('先写下现场变化或时间安排'); act('send_message',{message:String(values.message).trim()}); form.reset(); return; }
    if(kind==='profile'){ act(intent==='draft'?'save_profile_draft':'save_profile',values); return; }
    if(kind==='schedule'){
      const rows=[...form.querySelectorAll('.schedule-day')].map(row=>({date:row.dataset.day,site_workable:row.querySelector('[name=site_workable]').value,weather_workable:row.querySelector('[name=weather_workable]').value,resource_minutes:row.querySelector('[name=resource_minutes]').value,intervals:row.querySelector('[name=intervals]').value}));
      act(intent==='draft'?'save_profile_draft':'save_schedule',{rows,form_kind:'schedule'});return;
    }
    if(kind==='task'){ if(intent==='draft'){act('save_profile_draft',{...values,form_kind:'task'});return;} act('save_task_series',values);return; }
    if(kind==='follow-up'){ const issueId=form.dataset.issueId; if(intent==='draft'){act('save_profile_draft',{...values,form_kind:'follow_up',issue_id:issueId});return;} act('save_follow_up',{...values,issue_id:issueId});return; }
    if(kind==='series-edit'){
      if(intent==='draft'){act('save_profile_draft',{...values,form_kind:'series',series_id:form.dataset.seriesId});return;}
      act('update_task_series',{...values,series_id:form.dataset.seriesId});return;
    }
    if(kind==='outing'){
      const payload={...values,member_task_ids:new FormData(form).getAll('member_task_ids')};
      if(intent==='draft'){act('save_profile_draft',{...payload,form_kind:'outing',group_id:form.dataset.groupId});return;}
      act('save_execution_group',payload);return;
    }
    if(kind==='result'){
      if(intent==='draft'){act('save_profile_draft',{...values,task_id:form.dataset.taskId,form_kind:'result'});return;}
      const result_values={}; for(const [name,value] of Object.entries(values)){const m=name.match(/^result_(\d+)$/);if(m){const item=allItems.find(i=>i.id===form.dataset.taskId);const key=item?.result_fields?.[Number(m[1])]||'task_result';result_values[key]=value;}}
      const item=allItems.find(i=>i.id===form.dataset.taskId);act('record_result',{task_id:form.dataset.taskId,result_values,notes:values.notes||'',completed:Boolean(values.completed),resolve_issue_id:values.resolve_issue==='resolved'?item?.related_issue_id:null});return;
    }
    if(kind==='observation'){act('record_observation',values);return;}
    if(kind==='resource'){act(intent==='draft'?'save_profile_draft':'record_resource',intent==='draft'?{...values,form_kind:'resource'}:values);return;}
    if(kind==='location-search'){act('search_location',values);return;}
    if(kind==='cancel-task'){act('cancel_task',{task_id:form.dataset.taskId,reason:values.reason});return;}
  };
  const handleClick = event => {
      const button=event.target.closest('[data-action]'); if(!button)return;
      const action=button.dataset.action;
      if(action==='unsaved-continue'){button.closest('[data-unsaved-prompt]')?.remove();return;}
      if(action==='unsaved-discard'){closePanel(true);return;}
      if(action==='unsaved-save'){
        const form=app.querySelector('[data-dialog-body] form[data-form]');
        const draftButton=form?.querySelector('button[name="intent"][value="draft"]');
        if(!form||!draftButton)return toast('当前表单没有可保存草稿的入口');
        app._closeAfterDraft=true;form.requestSubmit(draftButton);return;
      }
      if(action==='close-dialog'){closePanel();return;}
      if(action==='week'){weekOpen=!weekOpen;ui.weekOpen=weekOpen;persistUI();renderSchedule();return;}
      if(action==='tab-schedule'||action==='tab-fields'||action==='tab-assistant'){tab=action.slice(4);ui.tab=tab;persistUI();render();return;}
      if(action==='select-date'&&button.dataset.date){selectedDate=button.dataset.date;ui.selected[fieldKey]=selectedDate;persistUI();renderSchedule();return;}
      if(action==='select-field'){act('select_field',{selected_field_id:button.dataset.fieldId});return;}
      if(action==='new-field'){act('new_field',{});return;}
      if(action==='preview'||action==='preview-again'){act('preview_plan',{});return;}
      if(action==='adopt'){act('adopt_plan',{preview_id:button.dataset.previewId});return;}
      if(action==='cancel-preview'){act('cancel_preview',{preview_id:button.dataset.previewId});return;}
      if(action==='confirm-proposal'){act('confirm_proposal',{proposal_id:button.dataset.proposalId});return;}
      if(action==='ignore-proposal'){act('ignore_proposal',{proposal_id:button.dataset.proposalId});return;}
      if(action==='cancel-group'){act('cancel_execution_group',{group_id:button.dataset.groupId});return;}
      if(action==='edit-group'){showPanel('management',button.dataset.groupId);return;}
      if(action==='retry-preview'){act('retry_preview',{proposal_id:button.dataset.proposalId});return;}
      if(action==='task-detail'){showPanel('detail',button.dataset.taskId);return;}
      if(action==='open-result'){showPanel('result',button.dataset.taskId);return;}
      if(action==='choose-location'){act('select_location',{location_id:button.dataset.locationId});return;}
      if(action==='fetch-weather'){act('fetch_weather',{});return;}
      if(action==='switch-mode'){act('switch_mode',{mode:button.dataset.modeValue});closePanel();return;}
      if(action==='seed-demo'){act('seed_demo',{scenario:button.dataset.scenario});closePanel();return;}
      if(action==='download-json'){download('nongshi-plan.json',data.export_json||'{}','application/json');return;}
      if(action==='download-markdown'){download('nongshi-plan.md',data.export_markdown||'','text/markdown;charset=utf-8');return;}
      if(action==='schedule-reason'){app.querySelector('[data-pending]').scrollIntoView({behavior:'smooth',block:'center'});return;}
      openLocal(action);
  };
  const handleSubmit = event => {
    const form=event.target.closest('form[data-form]');
    if(form)submitForm(form,event);
  };
  const handleDialogClick = event => {
    if(event.target===event.currentTarget)closePanel();
  };
  const handleDialogClose = () => {
    if(app._panelDirty){const dialog=app.querySelector('[data-dialog]');dialog.showModal();showUnsavedPrompt();return;}
    if(panel){panel='';ui.panel='';persistUI();}
  };
  const handleDirty = event => { if(event.target.closest('.work-dialog form[data-form]'))app._panelDirty=true; };
  app._workbenchHandlers = {
    click: handleClick,
    submit: handleSubmit,
    dirty: handleDirty,
    dialogClick: handleDialogClick,
    dialogClose: handleDialogClose,
  };
  if (!app.dataset.bound) {
    app.dataset.bound='true';
    app.addEventListener('click', event => app._workbenchHandlers?.click(event));
    app.addEventListener('submit', event => app._workbenchHandlers?.submit(event));
    app.addEventListener('input', event => app._workbenchHandlers?.dirty(event));
    app.addEventListener('change', event => app._workbenchHandlers?.dirty(event));
    app.querySelector('[data-dialog]').addEventListener('click', event => app._workbenchHandlers?.dialogClick(event));
    app.querySelector('[data-dialog]').addEventListener('close', () => app._workbenchHandlers?.dialogClose());
  }
  render();
  if (data.agent_busy && !app._pollTimer) {
    app._pollTimer=setInterval(()=>{
      const current=app._workbenchData||data; const currentField=current.field||null;
      (app._setTriggerValue||setTriggerValue)('action',{operation_id:crypto.randomUUID(),kind:'poll_agent',field_id:currentField?.id||'',expected_draft_version:currentField?.draft_version||0,expected_fact_version:currentField?.fact_version||0,expected_task_version:currentField?.task_version||0,expected_plan_id:current.plan?.id||null,payload:{}});
    },800);
  } else if (!data.agent_busy && app._pollTimer) { clearInterval(app._pollTimer);app._pollTimer=null; }
  if (ui.panel && !app.querySelector('[data-dialog]').open) showPanel(ui.panel, ui.panel_subject || '');
  if (data.operation==='close_panel') closePanel();
}
