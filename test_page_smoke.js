// Smoke test for console_page.html: runs the REAL page in WebKit with a fake backend and clicks through every feature.
// Run:  osascript -l JavaScript test_page_smoke.js console_page.html     (macOS)   -- exits 1 if anything fails
ObjC.import('Cocoa'); ObjC.import('WebKit'); ObjC.import('Foundation');
function run(argv){
  var dir = argv[0].replace(/[^\/]*$/, '');
  function read(p){ return $.NSString.stringWithContentsOfFileEncodingError(p, $.NSUTF8StringEncoding, null).js; }
  var html = read(argv[0]).replace('<head>', '<head><script>' + read(dir + 'test_page_smoke_stub.js') + '</script>');
  var wv = $.WKWebView.alloc.initWithFrameConfiguration($.NSMakeRect(0,0,1300,900), $.WKWebViewConfiguration.alloc.init);
  var win = $.NSWindow.alloc.initWithContentRectStyleMaskBackingDefer($.NSMakeRect(0,0,1300,900), 1, 2, false); win.contentView = wv;
  wv.loadHTMLStringBaseURL(html, $.NSURL.URLWithString('https://example.test/'));
  function spin(s){ $.NSRunLoop.currentRunLoop.runUntilDate($.NSDate.dateWithTimeIntervalSinceNow(s)); }
  function ev(js, wait){ var o = null; wv.evaluateJavaScriptCompletionHandler(js, function(r, e){ o = r ? ObjC.unwrap(r) : ('ERR ' + (e ? ObjC.unwrap(e.localizedDescription) : '?')); }); spin(wait || 0.35); return o; }
  var pass = 0, fails = [];
  function check(name, cond, detail){ if (cond) pass++; else { fails.push(name); console.log('  FAIL: ' + name + (detail !== undefined ? '  -> ' + detail : '')); } }
  function q(sel, prop){ return ev("(function(){var e=document.querySelector(" + JSON.stringify(sel) + ");return e?String(e." + (prop || 'textContent') + "):'__none__'})()"); }
  function count(sel){ return parseInt(ev("document.querySelectorAll(" + JSON.stringify(sel) + ").length")); }
  spin(1.8);
  check('page loads with no JavaScript errors', ev('window.__errs.length') == 0, ev('window.__errs.join(" | ")'));
  // ---- Trainer
  check('status card shows the freshness state', q('#status .head').indexOf('Model up to date') >= 0, q('#status'));
  check('status card shows the published counts', q('#status').indexOf('1800') >= 0);
  check('four demo option cards render with chips', count('#opts .opt') == 4 && count('#opts .opt .chip') >= 6, count('#opts .opt'));
  check('data table lists every table', count('.tblrow') == 8, count('.tblrow'));
  check('group tags: recommendations and views are green, everything else is blue', count('.tblrow .pill.ok') == 3 && count('.tblrow .pill.blue') == 5 && count('.tblrow .pill.warn') == 0 && count('.tblrow .pill.mu') == 0, count('.tblrow .pill.ok') + '/' + count('.tblrow .pill.blue'));
  check('the datasets description sentence is gone', ev("document.body.textContent.indexOf('Every table the pipeline reads')") == -1);
  ev("document.querySelectorAll('.tblrow')[2].click()", 0.9);
  check('clicking a table shows its 5 sample rows', count('.minis') == 1 && count('.mini tr') == 6, count('.mini tr'));
  check('hidden tables (run_log, demo_*) are not listed', ev("document.querySelector('#ds').textContent.indexOf('run_log')") == -1 && ev("document.querySelector('#ds').textContent.indexOf('demo_')") == -1);
  ev("document.querySelector('#btnAll').click()", 2.2);
  check('Expand all opens every table', count('.tblrow.open') == 8 && q('#btnAll') == 'Collapse all', count('.tblrow.open') + ' ' + q('#btnAll'));
  ev("document.querySelector('#btnAll').click()", 0.3);
  check('Collapse all closes every table', count('.tblrow.open') == 0 && q('#btnAll') == 'Expand all');
  spin(0.1);
  check('Browse Data has only Expand all (no Refresh), under the title line', count('#btnDs') == 0 && count('.dtools button') == 1 && count('#btnAll') == 1 && count('.tblrow') == 8);
  ev("document.querySelector('#btnPrev').click()", 0.8);
  check('Preview shows both gates', count('#prev .tile') == 2, q('#prev'));
  ev("document.querySelector('#btnRun').click()", 2.6);
  check('a run renders its timeline, verification, before/after and report', count('#trace .step') == 2 && q('#trace').indexOf('Verification') >= 0 && q('#trace').indexOf('all good') >= 0, q('#trace').slice(0, 80));
  check('the gate examples table renders', count('#trace table') >= 2);
  ev("(function(){var c=document.querySelector('#opts .opt .chip:last-of-type');if(c)c.click();return 1})()");
  check('Trainer order: status, run, trace, data pipeline last', ev("(function(){var c=Array.from(document.querySelectorAll('#tab-trainer > .sect'));return c.map(function(x){return x.id||(x.querySelector('#trace')?'trace':(x.querySelector('#ds')?'data':'run'))}).join(',')})()") == 'status,run,trace,data');
  check('table rows have no lines between them; the column headings have no line either', ev("getComputedStyle(document.querySelector('.tblrow td')).borderBottomWidth") == '0px' && ev("getComputedStyle(document.querySelector('.dt th')).borderBottomWidth") == '0px');
  check('section titles are verb-first', ev("Array.from(document.querySelectorAll('#tab-trainer > .sect')).map(function(x){return x.querySelector('h2').textContent}).join('|')") == 'Check Model Status|Run Trainer|Watch Logs|Browse Data');
  check('Trainer sections have no box, and a line under each title', count('#tab-trainer .card') == 0 && ev("(function(){var s=Array.from(document.querySelectorAll('#tab-trainer > .sect'));return s.every(function(x){var c=getComputedStyle(x),h=getComputedStyle(x.querySelector('h2'));return c.borderTopWidth=='0px'&&c.boxShadow=='none'&&(h.borderBottomWidth=='1px'||x.querySelector('.sect__head'))})})()") == 1);
  check('the Recommender keeps its cards', count('#tab-recommender .card') == 2);
  check('Manual and Auto sit side by side with short descriptions', count('.modes input[type=radio]') == 2 && q('.mode:nth-child(1) .mname') == 'Manual' && q('.mode:nth-child(1) .mdesc') == 'stops at each gate' && q('.mode:nth-child(2) .mname') == 'Auto' && q('.mode:nth-child(2) .mdesc') == 'runs as scheduled' && count('#modeHint') == 0 && ev("(function(){var a=document.querySelectorAll('.mode');return Math.abs(a[0].getBoundingClientRect().top-a[1].getBoundingClientRect().top)<4})()") == 1);
  check('status labels are sentence case', ev("getComputedStyle(document.querySelector('#status .fact span')).textTransform") == 'none');
  check('Browse Data headings are sentence case', ev("getComputedStyle(document.querySelector('.dt th')).textTransform") == 'none');
  ev("document.querySelector('input[value=stepper]').click()", 0.2);
  check('the Plain-English explanations checkbox is gone', count('#narr') == 0 && ev("document.body.textContent.indexOf('Plain-English')") == -1);
  check('Preview and Run are in one right-hand group beside the mode toggle', count('.runrow .runbtns button') == 2 && count('.runrow .modes') == 1);
  check('the gates are explained under the options, with numbered gate icons and no word Gate', count('.runrow .gates p') == 2 && count('.gates svg.gico') == 2 && q('#minWeeks') == '2' && count('.gates b') == 0 && q('.gates p:nth-child(1) .gtxt').indexOf('Retrain:') == 0 && q('.gates p:nth-child(2) .gtxt').indexOf('Publish:') == 0 && ev("document.querySelector('.gates').textContent.indexOf('Gate')") == -1);
  check('content sits at least 12px below each title line', ev("(function(){var ok=true;document.querySelectorAll('#tab-trainer > .sect').forEach(function(s){var t=s.querySelector('h2').parentNode===s?s.querySelector('h2'):s.querySelector('.sect__head');var n=t.nextElementSibling;ok=ok&&n.getBoundingClientRect().top-t.getBoundingClientRect().bottom>=12});return ok})()") == 1);
  check('the five facts sit on one row and Published contacts does not wrap', ev("(function(){var f=Array.from(document.querySelectorAll('#status .fact'));var t=f[0].getBoundingClientRect().top;return f.length==5&&f.every(function(x){return Math.abs(x.getBoundingClientRect().top-t)<2})&&document.querySelectorAll('#status .fact b')[3].getBoundingClientRect().height<26})()") == 1);
  check('facts are 13px values with 12px labels', ev("getComputedStyle(document.querySelector('#status .fact b')).fontSize") == '13px' && ev("getComputedStyle(document.querySelector('#status .fact span')).fontSize") == '12px');
  check('the options say Manual and Auto; the button says Preview', ev("Array.from(document.querySelectorAll('.mname')).map(function(s){return s.textContent}).join('|')") == 'Manual|Auto' && q('#btnPrev') == 'Preview');
  check('status headline matches body text (16px, normal weight)', ev("getComputedStyle(document.querySelector('#status .head')).fontSize") == '16px' && ev("getComputedStyle(document.querySelector('#status .head')).fontWeight") == '400');
  check('the Run button is named Run Trainer', q('#btnRun') == 'Run Trainer');
  check('the Trainer page never says "demo" (except while an experiment is open in the panel)', ev("document.querySelector('#tab-trainer').textContent.toLowerCase().indexOf('demo')") == -1, ev("document.querySelector('#tab-trainer').textContent.toLowerCase().indexOf('demo')"));
  check('radio mode options, no pill toggle', count('.seg') == 0 && ev("getComputedStyle(document.querySelector('.mode input')).appearance") != 'none');
  check('section titles are larger', parseFloat(ev("getComputedStyle(document.querySelector('#status h2')).fontSize")) >= 21);
  check('status headline and explanation share one row', ev("(function(){var a=document.querySelector('#status .head').getBoundingClientRect(),b=document.querySelector('#status .statusline .mute').getBoundingClientRect();return Math.abs((a.top+a.bottom)/2-(b.top+b.bottom)/2)<8 && b.left>a.right-1})()") == 1);
  check('gate text is no wider than the Manual/Auto row', ev("(function(){var m=document.querySelector('.modes').getBoundingClientRect(),g=document.querySelector('.gates').getBoundingClientRect();return g.right<=m.right+1 && g.width>m.width-6})()") == 1);
  check('Run Trainer is white with an accent outline, like Preview', ev("getComputedStyle(document.querySelector('#btnRun')).backgroundColor") == ev("getComputedStyle(document.querySelector('#btnPrev')).backgroundColor") && ev("getComputedStyle(document.querySelector('#btnRun')).borderTopColor") != ev("getComputedStyle(document.querySelector('#btnPrev')).borderTopColor"));
  check('intros say "agent"', q('#tab-trainer .hero p').indexOf('The Trainer agent keeps the recommendations current.') == 0 && ev("document.querySelector('#tab-recommender .hero p').textContent.indexOf('The Recommender agent answers')") == 0 && ev("document.body.textContent.indexOf('(Agent')") == -1);
  // ---- Admin
  check('Trainer no longer holds the demo card', count('#tab-trainer #demoCard') == 0 && count('#tab-trainer #opts') == 0);
  check('Admin tab is visible for operators', q('#tabAdmin', 'hidden') == 'false');
  check('Admin is a link outside the tab row', count('.tabs #tabAdmin') == 0 && count('.tabs .tab-btn') == 2 && q('#tabAdmin','textContent').trim() == '' && q('#tabAdmin','title') == 'Admin' && q('#tabAdmin','ariaLabel') == 'Admin');
  ev("location.hash='#admin'", 0.4);
  check('Admin tab shows the four demo cards', count('#tab-admin.active') == 1 && count('#tab-admin #demoCard #opts .opt') == 4 && count('.adminlink.active') == 1 && count('.tab-btn.active') == 0);
  check('option cards are a 2 x 2 grid with Roman numerals', ev("getComputedStyle(document.querySelector('#opts')).gridTemplateColumns.split(' ').length") == 2 && ev("Array.from(document.querySelectorAll('#opts .opt h3')).map(function(h){return h.firstChild.textContent}).join('|')") == 'I. Option|II. Option|III. Option|IV. Option' + '');
  ev("document.querySelector('[data-tab=trainer]').click()", 0.3);
  ev("window.__demo=true;loadState()", 0.8);
  check('Trainer status shows the active demo change note with an Admin link', q('#status .demoNote').indexOf('Changes in progress: I') >= 0 && count('#status .demoNote a[target="nba-admin"]') == 1, q('#status .demoNote'));
  ev("window.__runId='r2';document.querySelector('#btnRun').click()", 2.6);
  check('the Gate 2 pause offers only Continue and Abort', ev("Array.from(document.querySelectorAll('#trace .gate button')).map(function(b){return b.textContent}).join('|')") == 'Continue|Abort' && ev("document.body.textContent.indexOf('Try a change')") == -1 && ev("document.body.textContent.indexOf('Re-check (')") == -1);
  check('a skipped Gate 2 points operators to Admin for experiments', q('#trace .gate').indexOf('For experimentation, see Admin.') > 0 && count('#trace .gate a[target="nba-admin"]') == 1, q('#trace .gate'));
  ev("window.__rechecks=0;new BroadcastChannel('nba-studio').postMessage({t:'changed'})", 2.2);
  check('a change applied in another window re-checks the paused gate by itself', ev('window.__rechecks') >= 1 && q('#trace .gate').indexOf('Re-checked after your change.') > 0, ev('window.__rechecks'));
  check('the side panel is gone', count('#drawer') == 0 && count('#scrim') == 0 && count('#tab-admin #demoCard') == 1);
  ev("clearInterval(POLL);RUN=null;RUNLIVE=null;window.__runId='r1'", 0.2);
  ev("window.__runId='r1'", 0.1);
  // viewers: no Admin tab, no shortcut
  ev("window.__operator=false;loadState()", 0.8);
  check('Admin tab is hidden for viewers', q('#tabAdmin', 'hidden') == 'true');
  ev("location.hash='#admin'", 0.4);
  check('viewers cannot open Admin by hash', count('#tab-admin.active') == 0);
  ev("window.__operator=true;window.__demo=false;loadState();location.hash='#trainer'", 0.8);
  // ---- two-window demo support
  check('Admin opens in its own named window; Back link returns to the Trainer window', q('#tabAdmin','target') == 'nba-admin' && q('#tab-admin .backlink','target') == 'nba-trainer' && q('#tabAdmin','rel') == '');
  check('selected pill and Apply use the quiet outline style', ev("getComputedStyle(document.querySelector('#opts .chip.selected')).backgroundColor") == ev("getComputedStyle(document.body).backgroundColor") && ev("getComputedStyle(document.querySelector('#opts .opt .btn')).backgroundColor") == ev("getComputedStyle(document.querySelector('#btnPrev')).backgroundColor"));
  ev("document.querySelectorAll('.tblrow')[1].click()", 0.9);
  ev("window.__stateCalls=0;window.dispatchEvent(new Event('focus'))", 0.9);
  check('regaining focus refreshes the status', ev('window.__stateCalls') >= 1, ev('window.__stateCalls'));
  check('...without closing a table the user opened', count('.tblrow.open') == 1, count('.tblrow.open'));
  ev("document.querySelectorAll('.tblrow')[1].click()", 0.3);
  // ---- opening /#admin in a fresh window (before the page knows who you are)
  ev("OPKNOWN=false;OPERATOR=false;history.replaceState(null,'','#admin');showTab('admin')", 0.2);
  check('a fresh load of #admin keeps the request until the operator check finishes', ev('location.hash') == '#admin');
  ev("loadState()", 1.0);
  check('...then opens the Admin page for an operator', count('#tab-admin.active') == 1 && ev('location.hash') == '#admin');
  ev("window.__operator=false;loadState()", 1.0);
  ev("OPKNOWN=false;OPERATOR=false;history.replaceState(null,'','#admin');showTab('admin');loadState()", 1.0);
  check('a viewer who opens #admin lands on the Trainer and the address is corrected', count('#tab-admin.active') == 0 && ev('location.hash') == '#trainer', ev('location.hash'));
  ev("window.__operator=true;loadState();location.hash='#trainer'", 1.0);
  // ---- active run: re-attach, status line, pause prompt
  ev("window.__active='r4';loadState()", 1.8);
  check('a paused run is re-attached after a reload, with its prompt and buttons', count('#trace .gate') == 1 && q('#trace .gate').indexOf('Gate 1 decided: HOLD') == 0 && count('#trace .gate button') == 2, q('#trace').slice(0, 60));
  check('status line says Paused at Gate 1, with a spinner-free warn dot', q('#status .head') == 'Paused at Gate 1' && count('#status .dot.warn') == 1 && q('#status .statusline .mute').indexOf('Continue in Watch Logs') >= 0, q('#status .head'));
  check('the browser tab title shows the pause', ev('document.title') == 'Paused · Trainer · NBA Studio');
  ev("window.__active='r3';loadState()", 1.8);
  check('a running run shows Run in progress with a spinner', q('#status .head') == 'Run in progress' && count('#status .dot.run') == 1 && ev('document.title') == 'Trainer · NBA Studio');
  ev("window.__active=null;clearInterval(POLL);RUN=null;RUNLIVE=null;loadState()", 1.2);
  check('when no run is active, the freshness verdict returns', q('#status .head') == 'Model up to date' && count('#status .dot.ok') == 2);
  ev("window.__runBlocked=true;document.querySelector('#btnRun').click()", 1.0);
  check('a blocked run explains itself in normal text with a link to the active run', q('#runMsg').indexOf('A run is already active') == 0 && count('#runMsg a') == 1 && q('#runMsg', 'className') == 'mute', q('#runMsg'));
  ev("window.__runBlocked=false", 0.1);
  // ---- a failed background refresh must not wipe the status card
  ev("window.__stateFail=true;LASTFOCUS=0;window.dispatchEvent(new Event('focus'))", 1.0);
  check('a failed refresh on focus keeps the status card as it was', count('#status .facts') == 1 && ev("document.querySelector('#status').textContent.indexOf('Could not load')") == -1);
  ev("loadState()", 0.8);
  check('a failed explicit load still shows the error', ev("document.querySelector('#status').textContent.indexOf('Could not load status')") >= 0);
  ev("window.__stateFail=false;loadState()", 1.0);
  check('the card recovers when loading works again', count('#status .facts') == 1);
  ev("window.__optsFail=true;LASTFOCUS=0;window.dispatchEvent(new Event('focus'))", 1.0);
  check('a failed options refresh on focus keeps the cards and shows no error', count('#opts .opt') == 4 && ev("document.querySelector('#injMsg').textContent.indexOf('Could not load options')") == -1);
  ev("window.__optsFail=false", 0.1);
  // ---- recommendations status line
  check('current recommendations show their publish time', ev("document.querySelectorAll('#status .head')[1].textContent") == 'Recommendations current' && ev("document.querySelectorAll('#status .statusline .mute')[1].textContent.indexOf('2026-11-14 23:46:16')") >= 0);
  ev("window.__recsPending=true;loadState()", 1.0);
  check('pending changes show a warning line while the model line stays up to date', q('#status .head') == 'Model up to date' && ev("document.querySelectorAll('#status .head')[1].textContent") == 'Recommendations have pending changes' && count('#status .dot.warn') == 1 && ev("document.querySelectorAll('#status .statusline .mute')[1].textContent.indexOf('Preview shows what would change')") > 0);
  ev("window.__active='r3';loadState()", 1.8);
  check('during a run only the run line is shown', ev("Array.from(document.querySelectorAll('#status .status')).filter(function(r){return r.style.display!=='none'}).length") == 1 && q('#status .head') == 'Run in progress');
  ev("window.__active=null;clearInterval(POLL);RUN=null;RUNLIVE=null;window.__recsPending=false;loadState()", 1.2);
  // ---- windows tell each other when something changed
  ev("window.__stateCalls=0;LASTFOCUS=0;new BroadcastChannel('nba-studio').postMessage({t:'changed'})", 1.2);
  check('a message from another window refreshes the status immediately', ev('window.__stateCalls') >= 1, ev('window.__stateCalls'));
  ev("window.__got=0;window.__spy=new BroadcastChannel('nba-studio');window.__spy.onmessage=function(m){if(m.data&&m.data.t==='changed')window.__got++};location.hash='#admin'", 0.5);
  ev("document.querySelector('#opts .opt .btn').click()", 1.8);
  check('applying a change tells the other windows', ev('window.__got') >= 1, ev('window.__got'));
  ev("window.__busy=\"'reset' is still running\";document.querySelector('#injMsg').textContent='';loadState()", 1.0);
  check('a window that did not start a task shows that one is running', q('#injMsg').indexOf('Working: reset is still running.') == 0, q('#injMsg'));
  ev("window.__busy=null;loadState()", 1.0);
  check('...and clears it when the task is done', q('#injMsg').indexOf('Working') == -1, q('#injMsg'));
  ev("location.hash='#trainer'", 0.3);
  // ---- tab titles
  ev("location.hash='#admin'", 0.4);
  check('Admin tab title names the page', ev('document.title') == 'Admin · NBA Studio', ev('document.title'));
  ev("location.hash='#recommender'", 0.4);
  check('Recommender tab title names the page', ev('document.title') == 'Recommender · NBA Studio', ev('document.title'));
  ev("location.hash='#trainer'", 0.4);
  check('Trainer tab title names the page', ev('document.title') == 'Trainer · NBA Studio', ev('document.title'));
  // ---- Recommender
  ev("document.querySelector('[data-tab=recommender]').click()", 0.4);
  check('Recommender tab shows with the sidebar groups', count('#tab-recommender.active') == 1 && count('#starters-lookup li') == 5 && count('#starters-sql li') == 3, count('#starters-lookup li'));
  check('Helpful hints is open and says Recommender capabilities', count('#hints.open') == 1 && q('#hints').indexOf('Recommender capabilities') >= 0);
  check('the greeting and the limit footnote are gone', ev("document.body.textContent.indexOf('Hi. Ask me')") == -1 && ev("document.body.textContent.indexOf('Limit: 20 questions')") == -1);
  check('Clear chat is hidden before the first question', q('#clearChat', 'style.visibility') == 'hidden');
  ev("document.querySelector('#starters-sql .sug__btn').click()", 1.2);
  check('clicking a suggestion asks it and renders the answer', count('#chat .msg.me') == 1 && count('#chat .msg.bot') == 1 && count('#chat .msg.bot table') == 1 && count('#chat .msg.bot li') == 2, count('#chat .msg'));
  check('no answer badges are shown', count('.answered') == 0 && count('#chat .pill') == 0);
  check('live-SQL answers show SQL used with a copy icon', q('#chat .sqlbox summary').indexOf('SQL used') >= 0 && count('#chat .sqlwrap .iconbtn') == 1 && count('#chat .sqlwrap pre') == 1);
  check('Clear chat appears after a question', q('#clearChat', 'style.visibility') == 'visible');
  ev("document.querySelector('#clearChat').click()", 0.3);
  check('Clear chat empties the conversation and hides itself', count('#chat .msg') == 0 && q('#clearChat', 'style.visibility') == 'hidden');
  ev("document.querySelector('#q').value='<img src=x onerror=window.__errs.push(\"XSS\")>';document.querySelector('#askForm').requestSubmit()", 1.2);
  check('a hostile question is shown as text, never as HTML', count('#chat img') == 0 && ev('window.__errs.indexOf("XSS")') == -1);
  // ---- navigation + final error check
  ev("location.hash='#trainer'", 0.4);
  check('hash links switch tabs', count('#tab-trainer.active') == 1);
  check('no JavaScript errors anywhere during the whole run', ev('window.__errs.length') == 0, ev('window.__errs.join(" | ")'));
  console.log(pass + ' checks passed, ' + fails.length + ' failed');
  if (fails.length) $.exit(1);
  return '';
}
