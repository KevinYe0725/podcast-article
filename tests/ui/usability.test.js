const { boot, until, report, sleep } = require('./harness');
const response = (data, status = 200) => ({ ok: status < 400, status, json: async () => data, clone() { return response(data, status); } });
const close = async page => { await sleep(250); page.window.close(); };

(async () => {
  const out = {}, fails = [];
  const check = (name, pass) => { out[name] = !!pass; if (!pass) fails.push(name); };
  try {
    const draft = await boot();
    await draft.window.openSettings();
    check('clean settings hide the save controls', draft.$('savebar').hidden);
    draft.$('s_name').value = 'Unsaved preference';
    draft.$('s_name').dispatchEvent(new draft.window.Event('input', {bubbles:true}));
    draft.window.closeSettings();
    await draft.window.openSettings();
    check('navigation preserves unsaved preferences', draft.$('s_name').value === 'Unsaved preference');
    check('Notion destination can be configured', !!draft.$('notion_parent') && !!draft.$('notion_key'));
    check('invitation custom limits start collapsed', !!draft.$('invite-options') && !draft.$('invite-options').open);
    draft.window.finishJob({status:'done',workdir:'background-result',meta:{title:'Background article'},article_html:'<h1>New article</h1>'});
    check('background completion does not interrupt settings', draft.$('settings').style.display === 'block' && !draft.$('result').classList.contains('show'));
    check('background completion offers a reading action', draft.$('toast').textContent.includes('立即阅读'));
    await draft.window.loadSettings();
    await close(draft);

    let rejectRun, runCalls = 0;
    const run = await boot({ beforeParse(w) {
      const original = w.fetch;
      w.fetch = (url, opts) => String(url).endsWith('/api/run')
        ? (runCalls++, new Promise(resolve => { rejectRun = resolve; })) : original(url, opts);
    }});
    run.$('url').value = 'https://example.com/episode';
    const firstRun = run.window.startRun();
    await until(() => runCalls > 0);
    const secondRun = run.window.startRun();
    check('repeated generate clicks start one request', runCalls === 1);
    rejectRun(response({ error: 'test generation failure' }, 503));
    if (runCalls === 1) await Promise.all([firstRun, secondRun]);
    check('failed generation keeps the link and restores button', run.$('url').value.includes('example.com') && !run.$('go').disabled);
    await close(run);

    const pending = new Map();
    const search = await boot({ beforeParse(w) {
      const original = w.fetch;
      w.fetch = (url, opts) => String(url).includes('/api/search?')
        ? new Promise(resolve => pending.set(new URL(url, 'http://localhost').searchParams.get('q'), resolve))
        : original(url, opts);
    }});
    const oldRequest = search.window.runSearch('old');
    const newRequest = search.window.runSearch('new');
    await until(() => pending.size === 2);
    pending.get('new')(response({query:'new',items:[],stats:{}}));
    await newRequest;
    pending.get('old')(response({query:'old',items:[],stats:{}}));
    await oldRequest;
    check('slow old search cannot overwrite the latest query', search.$('libtitle').textContent.includes('new'));
    const afterClear = search.window.runSearch('cancelled');
    await until(() => pending.has('cancelled'));
    search.window.clearSearch();
    pending.get('cancelled')(response({query:'cancelled',items:[],stats:{}}));
    await afterClear;
    check('clearing search ignores an in-flight response', search.$('libtitle').textContent === '全部文章');
    await close(search);

    const articles = new Map();
    const reader = await boot({beforeParse(w) {
      const original = w.fetch;
      w.fetch = (url, opts) => {
        if (/\/api\/file\/race-(old|new)\/meta\.json$/.test(url)) return new Promise(resolve => articles.set(url.includes('race-old')?'old':'new', resolve));
        if (/\/api\/file\/race-(old|new)\/article\.md$/.test(url)) return Promise.resolve(response({html:'<h1>'+ (url.includes('race-old')?'old':'new') +'</h1>'}));
        return original(url, opts);
      };
    }});
    const oldArticle = reader.window.openEpisode('race-old');
    const newArticle = reader.window.openEpisode('race-new');
    await until(() => articles.size === 2);
    articles.get('new')(response({title:'new',url:'https://example.com/new'}));
    await newArticle;
    articles.get('old')(response({title:'old',url:'https://example.com/old'}));
    await oldArticle;
    check('rapid article switching keeps the latest article', reader.$('article').textContent === 'new' && reader.window.location.hash.includes('race-new'));
    await close(reader);

    const saved = [];
    const personal = await boot({beforeParse(w) {
      const original = w.fetch;
      w.fetch = (url, opts) => {
        if (String(url).endsWith('/api/integrations/secrets/LLM_API_KEY') && opts?.method === 'PUT') return Promise.resolve(response({status:{configured:true,masked:'test'}}));
        if (String(url).endsWith('/api/settings') && opts?.method === 'POST') { saved.push(JSON.parse(opts.body)); return Promise.resolve(response({})); }
        return original(url,opts);
      };
    }});
    await personal.window.openSettings('keys');
    personal.$('s_name').value = 'Other unsaved preference';
    personal.$('s_name').dispatchEvent(new personal.window.Event('input',{bubbles:true}));
    personal.$('api_llm_key').value = 'test-only-api-key';
    await personal.window.saveIntegrationSecret('LLM_API_KEY','api_llm_key','api_llm_key_status');
    check('saving an API key also enables its service', saved.length === 1 && saved[0].services.llm_mode === 'personal');
    check('API save preserves unrelated unsaved settings', !saved[0].profile && !('asr_mode' in saved[0].services) && personal.$('s_name').value === 'Other unsaved preference');
    check('saved API keys are cleared from the input', personal.$('api_llm_key').value === '');
    await close(personal);

    let historyReply;
    const conversation = await boot({beforeParse(w) {
      const original = w.fetch;
      w.fetch = (url,opts) => {
        if (String(url).includes('/api/qa?')) return new Promise(resolve => {historyReply=resolve;});
        if (String(url).endsWith('/api/ask') && opts?.method === 'POST') return Promise.resolve(response({error:'Test-only provider error'},503));
        return original(url,opts);
      };
    }});
    await conversation.window.openEpisode(encodeURIComponent('__UI测试单集'));
    conversation.window.openAssist();
    await until(() => !!historyReply);
    conversation.$('aq').value = 'A rapidly pasted question';
    conversation.$('aq').dispatchEvent(new conversation.window.Event('input',{bubbles:true}));
    await conversation.window.askAI();
    historyReply(response({last_thread:''}));
    await sleep(100);
    check('late history loading cannot erase a new question', conversation.$('amessages').textContent.includes('A rapidly pasted question'));
    conversation.window.closeAssist();
    conversation.window.openAssist();
    check('closing and reopening the assistant preserves messages', conversation.$('amessages').textContent.includes('A rapidly pasted question'));
    await close(conversation);

    let csrfCalls = 0, writes = 0;
    const csrf = await boot({ beforeParse(w) {
      const original = w.fetch;
      w.fetch = (url, opts) => {
        if (String(url).endsWith('/api/auth/csrf')) return Promise.resolve(response({csrf_token: 'fresh-' + ++csrfCalls}));
        if (String(url).endsWith('/api/categories') && opts?.method === 'POST') {
          writes++;
          return Promise.resolve(writes === 1 ? response({error:'csrf_failed'},403) : response({id:'test'}));
        }
        return original(url, opts);
      };
    }});
    const result = await csrf.window.fetch('/api/categories', {method:'POST', body:'{}'});
    check('stale CSRF is refreshed and retried once', result.ok && writes === 2 && csrfCalls === 2);
    await close(csrf);

    const queue = await boot({ beforeParse(w) {
      const original = w.fetch;
      w.fetch = (url, opts) => String(url).endsWith('/api/queue/test-error')
        ? Promise.resolve(response({message:'Cannot remove this task'},409)) : original(url, opts);
    }});
    await queue.window.queueRemove('test-error');
    check('queue errors are shown instead of ignored', queue.$('toast').textContent.includes('Cannot remove'));
    await close(queue);

    let offline = true;
    const recovery = await boot({ beforeParse(w) {
      const original = w.fetch;
      w.fetch = (url, opts) => offline && String(url).endsWith('/api/auth/me')
        ? Promise.reject(new Error('offline')) : original(url, opts);
    }});
    check('temporary connection failure does not log out', recovery.navigationAttempts.length === 0);
    check('temporary connection failure offers retry', !!recovery.$('app-error') && !recovery.$('app-error').hidden);
    offline = false;
    await recovery.window.initializeAuthenticatedApp();
    check('retry restores the authenticated app', !recovery.$('account-menu').hidden && recovery.$('app-error')?.hidden);
    await close(recovery);
  } catch (error) {
    out.error = String(error.stack || error);
    fails.push('usability regression suite threw');
  }
  report('交互可靠性与设置简化', out, fails);
})();
