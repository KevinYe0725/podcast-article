const { boot, report, sleep } = require('./harness');

(async () => {
  const out = {}, fails = [];
  const check = (name, pass) => { out[name] = !!pass; if (!pass) fails.push(name); };
  let page;
  try {
    page = await boot({ beforeParse(w) {
      Object.defineProperty(w, 'innerWidth', {value:390, configurable:true});
      Object.defineProperty(w, 'innerHeight', {value:844, configurable:true});
      Object.defineProperty(w, 'scrollY', {value:400, configurable:true});
      w.document.elementFromPoint = () => null; // jsdom has no hit-testing engine.
      const viewport = new w.EventTarget();
      Object.assign(viewport, {height:844, offsetTop:0, scale:1});
      Object.defineProperty(w, 'visualViewport', {value:viewport, configurable:true});
    }});
    const {window:w, doc, $} = page;
    const vars = doc.documentElement.style;
    w.visualViewport.height = 400;
    w.visualViewport.offsetTop = 34;
    w.visualViewport.dispatchEvent(new w.Event('resize'));
    check('keyboard resizing preserves the visible overlay height', vars.getPropertyValue('--pa-viewport-height') === '400px');
    check('keyboard panning follows the visible viewport top', vars.getPropertyValue('--pa-viewport-top') === '34px');
    check('floating audio clears the keyboard', vars.getPropertyValue('--pa-keyboard-height') === '410px');
    w.visualViewport.height = 844;
    w.visualViewport.offsetTop = 0;
    w.visualViewport.dispatchEvent(new w.Event('resize'));
    check('closing the keyboard restores full overlays', vars.getPropertyValue('--pa-viewport-height') === '844px' && vars.getPropertyValue('--pa-keyboard-height') === '0px');

    await w.openEpisode(encodeURIComponent('__UI测试单集'));
    const text = $('article').querySelector('p').firstChild;
    const fakeRange = {commonAncestorContainer:text, getBoundingClientRect:()=>({top:180,bottom:200,left:20,right:120,width:100,height:20})};
    w.getSelection = () => ({isCollapsed:false, rangeCount:1, getRangeAt:()=>fakeRange, toString:()=> 'Selected article passage'});
    Object.defineProperty($('selbtn'), 'offsetHeight', {value:30, configurable:true});
    Object.defineProperty($('selbtn'), 'offsetWidth', {value:100, configurable:true});
    w.positionSelBtn();
    check('selection action uses reader viewport coordinates', $('selbtn').style.top === '142px' && $('selbtn').style.left === '20px');
    $('result').dispatchEvent(new w.Event('scroll'));
    check('scrolling the reader hides an obsolete selection action', !$('selbtn').classList.contains('show'));
    doc.dispatchEvent(new w.Event('selectionchange'));
    await sleep(180);
    check('native touch selection exposes the reading action', $('selbtn').classList.contains('show'));

    w.closeResult();
    const card = doc.querySelector('.ep');
    const down = new w.MouseEvent('pointerdown', {bubbles:true, clientX:30, clientY:100, button:0});
    Object.defineProperty(down, 'pointerType', {value:'touch'});
    card.dispatchEvent(down);
    doc.dispatchEvent(new w.MouseEvent('pointermove', {bubbles:true, clientX:30, clientY:125}));
    check('swiping a phone card scrolls without starting a drag', !doc.body.classList.contains('dragging') && !doc.querySelector('.dragghost'));
    doc.dispatchEvent(new w.MouseEvent('pointercancel', {bubbles:true}));
  } catch (error) { out.error = String(error.stack || error); fails.push('viewport regression threw'); }
  finally { if (page) { await sleep(150); page.window.close(); } }
  report('手机键盘与阅读选区', out, fails);
})();
