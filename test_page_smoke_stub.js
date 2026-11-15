// Injected BEFORE the page's own script by test_page_smoke.js: records every JavaScript error and fakes the backend,
// so the real, unmodified page code runs end to end.
window.__errs = [];
window.addEventListener('error', function(e){ window.__errs.push('error: ' + e.message); });
window.addEventListener('unhandledrejection', function(e){ window.__errs.push('rejection: ' + (e.reason && e.reason.message || e.reason)); });
(function(){
  var tables = ['features','raw_channel_affinity','raw_touch_log','recommendations','recommendations_final','ref_partd_benchmark','uplift_train','v_recommendations'];
  var cols = ['hcp_id','specialty','region','state','practice_setting','years_in_practice','pct_medicare','pct_commercial','pct_medicaid','mean_patient_age','pct_cvd_comorbid','glucoren_reject_rate','segment','growth_group','decile'];
  var runPolls = 0;
  function opt(t,l,w,e){ return {title:t,layer:l,what:w,expect:e}; }
  var routes = {
    '/api/state': function(){ window.__stateCalls=(window.__stateCalls||0)+1; if(window.__stateFail) return {__status:500,error:'Job exceeded rate limits'}; return {state:{layer1_model_version:'v2-thru-2026-06-29',model_cutoff:'2026-06-29',latest_outcome_week:'2026-06-29',new_outcome_weeks:0,outcome_weeks_total:26},
        published:{model_version:'v2-thru-2026-06-29',contacts:1800,call:807,email:262,digital:731,substituted:725,compliance_failures:0,withheld:0,hcps:6000},
        demo:window.__demo?{changes:[{option:'A',hcp_id:'H104623'}],overrides:[]}:{changes:[],overrides:[]},busy:window.__busy||null,rules:{min_new_weeks:2},operator:window.__operator!==false,active_run:window.__active||null,recs:window.__recsPending?{status:'pending',published_at:'2026-11-14 23:46:16',source_changed_at:'2026-11-15 21:26:52'}:{status:'current',published_at:'2026-11-14 23:46:16',source_changed_at:'2026-11-14 23:00:00'}}; },
    '/api/options': function(){ if(window.__optsFail) return {__status:500,error:'Job exceeded rate limits'}; var o={}; ['A','B'].forEach(function(k){ o[k]=Object.assign(opt(k+'. Option','Layer 2','what','expect'),{candidates:[{hcp_id:'H100001',priority_rank:1,segment:'Movers'},{hcp_id:'H100002',priority_rank:2,segment:'Core'}]}); });
        o.C=Object.assign(opt('C. Option','Layer 2','what','expect'),{channels:[{channel:'digital',contacts:731},{channel:'call',contacts:807}]});
        o.E=Object.assign(opt('E. Option','Layer 1','what','expect'),{choices:[{weeks_back:1,model_cutoff:'2026-06-22',new_outcome_weeks:1,gate1:'HOLD'},{weeks_back:4,model_cutoff:'2026-06-01',new_outcome_weeks:4,gate1:'RETRAIN'}]}); return o; },
    '/api/datasets': function(){ return tables.map(function(t,i){ return {table_id:t,row_count:i*1000,last_modified:'2026-11-14 23:46:00'}; }); },
    '/api/table': function(u){ var n=decodeURIComponent((u.split('name=')[1]||'')); var restricted=['run_log','demo_changes','demo_overrides'].indexOf(n)>=0;
        return {table:n,columns:restricted?['run_ts','agent','step']:cols,rows:[1,2,3,4,5].map(function(i){ return (restricted?['run_ts','agent','step']:cols).map(function(c){ return c+'_'+i; }); }),restricted:restricted,note:restricted?'Sensitive columns are hidden for this table.':null}; },
    '/api/inject': function(){ return {summary:'Applied.'}; },
    '/api/preview': function(){ return {state:{},gate1:{decision:'HOLD',reason:'Nothing new.'},gate2:{decision:'SKIP',reason:'No differences.'}}; },
    '/api/ask': function(){ return {answer:'The top **HCP** is H1.\n\n| Region | Call |\n| :--- | :--- |\n| West | 133 |\n\n* one\n* two',tools_used:[{tool:'ask_data'}],sql:'SELECT region, COUNT(*) FROM nba_chat.recommendations GROUP BY region',sql_rows:5}; },
    '/api/run': function(){ if(window.__runBlocked) return {__status:409,error:'a workflow run is paused at a gate (continue or abort it first)'}; runPolls=0; return {run_id:window.__runId||'r1'}; },
    '/api/run/r3': function(){ return {id:'r3',mode:'auto',status:'running',error:null,elapsed:5,steps:[{id:'refresh',title:'Refresh features',status:'running',reason:'',facts:{},narration:null,seconds:null}],waiting:null,before:null,after:null,verify:null,report:null}; },
    '/api/run/r4': function(){ return {id:'r4',mode:'stepper',status:'waiting',error:null,elapsed:9,steps:[{id:'freshness',title:'Freshness check',status:'done',reason:'Hold.',facts:{},narration:null,seconds:4}],waiting:{key:'gate1',prompt:'Gate 1 decided: HOLD. Nothing new. Continue to the Layer 2 check?',options:['continue','abort']},before:null,after:null,verify:null,report:null}; },
    '/api/run/r2': function(){ return {id:'r2',mode:'stepper',status:'waiting',error:null,elapsed:3,steps:[{id:'layer2_gate',title:'Layer 2 gate',status:'done',reason:'Nothing would change.',facts:{},narration:null,seconds:1}],
        waiting:{key:'gate2',prompt:'Layer 2 will be SKIPPED. Nothing would change. Continue to skip it.',options:['continue','recheck','abort']},before:null,after:null,verify:null,report:null}; },
    '/api/run/r2/control': function(){ window.__rechecks=(window.__rechecks||0)+1; return {ok:true}; },
    '/api/run/r1': function(){ runPolls++; return {id:'r1',mode:'auto',status:'done',waiting:null,error:null,elapsed:12.3,
        steps:[{id:'refresh',title:'Refresh features',status:'done',reason:'Rebuilt.',facts:{},narration:'It refreshed.',seconds:4.1},
               {id:'layer2_gate',title:'Layer 2 gate',status:'done',reason:'Differs.',facts:{examples:[{hcp_id:'H1',before:'call',after:'digital',why:'cap'}],decision:'RUN'},narration:null,seconds:6.2}],
        before:{model_version:'v',contacts:1,call:1,email:0,digital:0,substituted:0,compliance_failures:0},after:{model_version:'v',contacts:1,call:1,email:0,digital:0,substituted:0,compliance_failures:0},
        verify:{status:'PASS',checks:[{name:'check',ok:true,detail:'fine'}]},report:'- all good'}; }
  };
  window.fetch = function(u, o){
    var path = String(u).split('?')[0]; var fn = routes[path];
    var body = fn ? fn(String(u)) : {error:'no route '+path}; var st = fn ? (body.__status||200) : 404;
    return Promise.resolve({status:st, ok:st<300, json:function(){ return Promise.resolve(body); }});
  };
})();
