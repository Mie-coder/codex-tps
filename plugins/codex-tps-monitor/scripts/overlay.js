/* Local, temporary Codex TPS UI. No chat text, network calls or app state writes. */
(payload => {
  const NAME = '__codexTPSMonitor';
  const VERSION = 3;
  const ATTR = 'data-codex-tps';
  const STYLE_ID = 'codex-tps-style';
  const norm = value => {
    const id = String(value || '').replace(/^local:/, '');
    return /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i.test(id) ? id.toLowerCase() : '';
  };
  const activeNodes = selector => Array.from(document.querySelectorAll(selector))
    .filter(node => !node.closest('[data-app-shell-active-page="false"]'));
  const activeThread = () => {
    for (const [selector, attribute] of [
      ['[data-above-composer-conversation-id]', 'data-above-composer-conversation-id'],
      ['[data-conversation-id]', 'data-conversation-id'],
      ['[data-app-action-sidebar-thread-active="true"]', 'data-app-action-sidebar-thread-id'],
    ]) {
      const ids = new Set(activeNodes(selector).map(node => norm(node.getAttribute(attribute))).filter(Boolean));
      if (ids.size) return ids.size === 1 ? ids.values().next().value : '';
    }
    return '';
  };
  // The backend uses the same identity lookup; this branch creates no UI.
  if (payload === null) return activeThread();
  if (window[NAME]?.version === VERSION) return window[NAME].update(payload);
  window[NAME]?.stop();
  const fmt = value => typeof value === 'number' && Number.isFinite(value) ? value.toFixed(1) : '—';
  const count = value => typeof value === 'number' ? value.toLocaleString('zh-CN') : '—';
  let data = payload, timer, stopped = false, lastUpdate = Date.now();
  const expanded = new Set();
  const style = document.createElement('style');
  style.id = STYLE_ID;
  style.textContent = `
    [${ATTR}] {display:block;box-sizing:border-box;max-width:100%;width:100%;margin:8px 0;
      color:var(--color-text-secondary,CanvasText);background:color-mix(in srgb,CanvasText 4%,transparent);
      border:1px solid color-mix(in srgb,CanvasText 13%,transparent);border-radius:8px;
      font:12px/1.6 -apple-system,BlinkMacSystemFont,"PingFang SC",sans-serif;overflow-wrap:anywhere;}
    [${ATTR}] summary{display:flex;align-items:center;flex-wrap:wrap;gap:4px 12px;
      cursor:pointer;padding:7px 10px;list-style:none;user-select:none;}
    [${ATTR}] summary::-webkit-details-marker{display:none}
    [${ATTR}] summary:focus-visible{outline:2px solid #3982f7;outline-offset:2px;border-radius:7px}
    [${ATTR}] strong{font-weight:600;color:var(--color-text,CanvasText);font-variant-numeric:tabular-nums}
    [${ATTR}] [data-tps-action]{margin-left:auto;white-space:nowrap}
    [${ATTR}] [data-tps-body]{border-top:1px solid color-mix(in srgb,CanvasText 10%,transparent);padding:12px}
    [${ATTR}] dl{margin:0;display:grid;grid-template-columns:minmax(0,1fr) auto;gap:6px 18px;}
    [${ATTR}] dt,[${ATTR}] dd{margin:0;font:inherit} [${ATTR}] dd{text-align:right;font-variant-numeric:tabular-nums;max-width:280px}
    [${ATTR}] p{margin:9px 0 0;font:inherit;opacity:.78}
  `;
  document.head.append(style);

  function element(tag, text) {
    const el = document.createElement(tag);
    if (text !== undefined) el.textContent = text;
    return el;
  }

  function finalNode(turn) {
    const ids = new Set((turn.final_ids || []).map(id => encodeURIComponent(id)));
    // Exact IDs only. Never infer a match from reply text or DOM position.
    for (const node of activeNodes('[data-local-conversation-item-target-ids]')) {
      if ((node.getAttribute('data-local-conversation-item-target-ids') || '').split(/\s+/).some(id => ids.has(id))) {
        return node.closest('[data-local-conversation-final-assistant]') ||
          node.querySelector('[data-local-conversation-final-assistant]') || node;
      }
    }
    const keys = new Set([`assistant:${turn.turn_id}`, turn.turn_id,
      ...(turn.user_ids || []).map(id => `user:${id}`)]);
    for (const node of activeNodes('[data-content-search-turn-key],[data-content-search-assistant-turn-key],[data-turn-key]')) {
      if (keys.has(node.getAttribute('data-content-search-turn-key')) ||
          keys.has(node.getAttribute('data-content-search-assistant-turn-key')) ||
          keys.has(node.getAttribute('data-turn-key'))) {
        const replies = node.matches('[data-local-conversation-final-assistant]') ? [node] :
          Array.from(node.querySelectorAll('[data-local-conversation-final-assistant]'));
        if (replies.length === 1) return replies[0];
      }
    }
    return null;
  }

  function placement(node) {
    let scope = node;
    for (let depth = 0; depth < 3 && scope; depth++, scope = scope.parentElement) {
      // Do not ascend into a container shared by several replies.
      if (scope.querySelectorAll('[data-local-conversation-final-assistant]').length > 1) break;
      const times = scope.querySelectorAll('[data-assistant-message-sent-time]');
      if (times.length === 1 && times[0].parentElement?.parentElement) {
        const after = times[0].parentElement;
        return {host: after.parentElement, after};
      }
    }
    return {host: node, after: null};
  }

  function makeChip(turn, key) {
    const chip = element('details'); chip.setAttribute(ATTR, key); chip.lang = 'zh-CN';
    const summary = element('summary');
    const primary = turn.status === 'interrupted' ? '已中断' : turn.status === 'failed' ? '未完成' :
      turn.effective_tps != null ? `有效 TPS ≈${fmt(turn.effective_tps)} tok/s` :
      turn.wall_tps != null ? '有效 TPS 暂不可用' : '暂无可靠统计';
    summary.append(element('strong', primary));
    if (turn.wall_tps != null) summary.append(element('span', `整轮 ${fmt(turn.wall_tps)} tok/s`));
    const action = element('span', '提示详情 ▾'); action.setAttribute('data-tps-action', ''); summary.append(action);
    const body = element('div'); body.setAttribute('data-tps-body', '');
    const dl = element('dl');
    const rows = [
      ['总输出（含推理）', `${count(turn.output_tokens)} tokens`],
      ['其中推理输出', `${count(turn.reasoning_tokens)} tokens`],
      ['模型响应耗时（估算）', `${fmt(turn.model_seconds)} 秒`],
      ['整轮耗时', `${fmt(turn.wall_seconds)} 秒`],
      ['模型响应次数', count(turn.response_count)],
      ['模型', turn.models?.length > 1 ? `混合模型：${turn.models.join(' / ')}` : turn.models?.[0] || '未记录'],
    ];
    rows.forEach(([label, value]) => dl.append(element('dt', label), element('dd', value)));
    body.append(dl,
      element('p', '有效 TPS 排除可识别的工具等待，仍包含请求和首 token 等待；不是纯生成速度。'),
      element('p', '总输出包含推理和工具调用，不等于回答正文长度。整轮 TPS 包含本轮全部等待。'));
    if (turn.notes?.length) body.append(element('p', `统计说明：${turn.notes.join('；')}`));
    chip.append(summary, body); chip.open = expanded.has(key);
    summary.setAttribute('aria-expanded', String(chip.open));
    chip.addEventListener('toggle', () => {
      if (chip.open) expanded.add(key); else expanded.delete(key);
      summary.setAttribute('aria-expanded', String(chip.open));
      action.textContent = chip.open ? '收起详情 ▴' : '提示详情 ▾';
    });
    chip.__signature = JSON.stringify(turn);
    return chip;
  }

  function render() {
    timer = null;
    if (stopped) return;
    const thread = activeThread();
    const existing = new Map(Array.from(document.querySelectorAll(`[${ATTR}]`), el => [el.getAttribute(ATTR), el]));
    const kept = new Set();
    if (thread && thread === norm(data.thread_id)) {
      for (const turn of data.turns || []) {
        const node = finalNode(turn);
        if (!node) continue;
        const {host, after} = placement(node);
        const key = `${thread}/${turn.turn_id}`;
        let chip = existing.get(key);
        if (chip && chip.__signature !== JSON.stringify(turn)) {chip.remove(); chip = null;}
        if (!chip) chip = makeChip(turn, key);
        if (after && (chip.parentElement !== host || chip.previousElementSibling !== after)) after.after(chip);
        else if (!after && chip.parentElement !== host) host.append(chip);
        kept.add(chip);
      }
    }
    document.querySelectorAll(`[${ATTR}]`).forEach(chip => {if (!kept.has(chip)) chip.remove();});
    return {ok: true, thread_id: thread, rendered: kept.size};
  }

  function schedule(records) {
    if (records.every(r => r.target.nodeType === 1 && (r.target.closest(`[${ATTR}]`) || r.target.id === STYLE_ID))) return;
    if (!timer) timer = setTimeout(render, 120);
  }
  const observer = new MutationObserver(schedule);
  observer.observe(document.body, {childList:true, subtree:true, attributes:true,
    attributeFilter:['data-app-action-sidebar-thread-active','data-conversation-id',
      'data-above-composer-conversation-id','data-app-shell-active-page',
      'data-local-conversation-item-target-ids','data-content-search-turn-key',
      'data-content-search-assistant-turn-key','data-turn-key']});
  const watchdog = setInterval(() => {if (Date.now() - lastUpdate > 15000) api.stop();}, 5000);
  const api = {
    version: VERSION,
    update(next) { data = next; lastUpdate = Date.now(); return render(); },
    stop() {
      stopped = true; observer.disconnect(); clearTimeout(timer); clearInterval(watchdog);
      document.querySelectorAll(`[${ATTR}]`).forEach(el => el.remove()); style.remove();
      delete window[NAME];
    },
  };
  window[NAME] = api;
  return render();
})
