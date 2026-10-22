"""The provisional viewer's stylesheet (08 §6).

The identity is **instrument panel**: hairline rules rather than shadows, near-square
corners, monospaced tabular numerics so a changing readout does not make the strip jitter,
and colour reserved almost entirely for severity. Anything coloured here carries meaning,
which is what makes a red border register instantly instead of competing with a palette.
"""

VIEWER_STYLES = r"""<style>
  :root {
    --void:#0a0b0d; --panel:#12141a; --raised:#1a1d25; --line:#262a35;
    --text:#e6e9ef; --text2:#9aa3b2; --dim:#6b7383; --signal:#2dd4bf;
    --warn:#e0a93b; --bad:#e5484d;
    --mono: ui-monospace, "Cascadia Mono", "JetBrains Mono", Consolas, monospace;
  }
  * { box-sizing: border-box; }
  body { margin:0; background:var(--void); color:var(--text);
         font:13px/1.4 system-ui, "Segoe UI", sans-serif; }
  .num { font-family:var(--mono); font-variant-numeric:tabular-nums; }
  #strip { display:flex; gap:20px; align-items:center; flex-wrap:wrap; padding:10px 16px;
           background:var(--panel); border-bottom:1px solid var(--line); position:sticky; top:0; }
  #strip .brand { font-weight:700; letter-spacing:.12em; }
  #strip .k { color:var(--dim); margin-right:6px; }
  #strip .warn { color:var(--warn); } #strip .bad { color:var(--bad); }
  #grid { display:grid; gap:12px; padding:16px;
          grid-template-columns:repeat(auto-fit,minmax(420px,1fr)); }
  .tile { background:var(--panel); border:1px solid var(--line); border-radius:2px; }
  .tile header { display:flex; justify-content:space-between; gap:8px; padding:6px 10px;
                 border-bottom:1px solid var(--line); }
  .tile .state { text-transform:uppercase; letter-spacing:.06em; font-size:11px; }
  .state.online { color:var(--signal); } .state.degraded,.state.reconnecting { color:var(--warn); }
  .state.failed,.state.offline { color:var(--bad); }
  .tile img { display:block; width:100%; background:#000; min-height:120px; }
  .tile footer { display:flex; gap:14px; padding:6px 10px; border-top:1px solid var(--line);
                 color:var(--text2); flex-wrap:wrap; }
  label { cursor:pointer; user-select:none; }
  #empty, #note { color:var(--dim); padding:16px; }

  /* Incident desk. Dense, hairline-ruled, colour reserved for severity (08 section 6). */
  #desk { padding:0 16px 16px; }
  #desk h2 { font-size:12px; letter-spacing:.12em; text-transform:uppercase; color:var(--text2);
             margin:16px 0 8px; font-weight:600; }
  #desk table { width:100%; border-collapse:collapse; font-size:12px; }
  #desk th { text-align:left; color:var(--dim); font-weight:500; padding:4px 8px;
             border-bottom:1px solid var(--line); white-space:nowrap; }
  #desk td { padding:4px 8px; border-bottom:1px solid var(--line); white-space:nowrap; }
  #desk tbody tr { cursor:pointer; }
  #desk tbody tr:hover { background:var(--raised); }
  #desk tbody tr.selected { background:var(--raised); }
  .sev { font-weight:600; }
  .sev-INFO { color:#5a93d4; } .sev-LOW { color:#8a9bb0; } .sev-MODERATE { color:var(--warn); }
  .sev-HIGH { color:#e8762c; } .sev-CRITICAL { color:var(--bad); }
  .pill { border:1px solid var(--line-strong,#3a404f); border-radius:2px; padding:0 5px;
          font-size:11px; color:var(--text2); }
  #detail { background:var(--panel); border:1px solid var(--line); border-radius:2px;
            margin-top:12px; padding:12px; }
  #detail h3 { margin:0 0 8px; font-size:13px; }
  #detail dl { display:grid; grid-template-columns:max-content 1fr; gap:2px 12px; margin:0 0 10px; }
  #detail dt { color:var(--dim); } #detail dd { margin:0; }
  #detail section { border-top:1px solid var(--line); padding-top:8px; margin-top:8px; }
  #detail ul { margin:0; padding-left:16px; }
  #detail li { margin:1px 0; }
  #actions { display:flex; gap:8px; flex-wrap:wrap; margin-bottom:10px; align-items:center; }
  #actions button { background:var(--raised); color:var(--text); border:1px solid var(--line-strong,#3a404f);
                    border-radius:2px; padding:4px 10px; font:inherit; cursor:pointer; }
  #actions button:disabled { color:var(--dim); cursor:not-allowed; border-color:var(--line); }
  #actions input { background:var(--void); color:var(--text); border:1px solid var(--line);
                   border-radius:2px; padding:4px 6px; font:inherit; min-width:220px; }
  .verdict-pass { color:var(--text2); } .verdict-reject { color:var(--bad); }
  .verdict-defer { color:var(--warn); }
  .warnbox { color:var(--warn); }

  /* Analytics. Inline SVG, no chart library (03 section 5). */
  #stats { display:grid; gap:12px; grid-template-columns:repeat(auto-fit,minmax(260px,1fr));
           margin-top:8px; }
  .card { background:var(--panel); border:1px solid var(--line); border-radius:2px; padding:10px; }
  .card h4 { margin:0 0 8px; font-size:11px; letter-spacing:.1em; text-transform:uppercase;
             color:var(--text2); font-weight:600; }
  .card .big { font-size:24px; }
  .bars { display:grid; gap:3px; }
  .bar { display:grid; grid-template-columns:110px 1fr 44px; gap:8px; align-items:center;
         font-size:12px; }
  .bar .track { background:var(--void); border:1px solid var(--line); height:10px; }
  .bar .fill { background:var(--signal); height:100%; }
  .bar .n { text-align:right; color:var(--text2); }
  .na { color:var(--dim); }
  #stat-controls { display:flex; gap:8px; align-items:center; flex-wrap:wrap; margin-top:8px; }
  #stat-controls select, #stat-controls button { background:var(--raised); color:var(--text);
         border:1px solid var(--line-strong,#3a404f); border-radius:2px; padding:4px 8px;
         font:inherit; }
  #stat-controls button[disabled] { color:var(--dim); border-color:var(--line);
         cursor:not-allowed; }
  #stat-caveats { color:var(--warn); font-size:12px; margin-top:8px; }
  #stat-caveats li { margin:2px 0; }
  #trend { width:100%; height:70px; display:block; }
</style>"""
