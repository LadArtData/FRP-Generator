#!/usr/bin/env python3
"""Strip client ciphertext from WARDEN_Console_4.html and wire ORDS like SCOUT.

Does not add demo data. Tenant payloads are loaded at runtime from
/warden-hooks after unlock; they are never embedded in source.
"""
from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "WARDEN_Console_4.html"
OUT = ROOT / "web" / "WARDEN.html"

CONFIG_BLOCK = """
<script id="warden-config" type="application/json">
{
  "ordsBase": "/ords/admin/warden-hooks",
  "apiKey": "",
  "env": "DEV",
  "build": "1.0.0"
}
</script>
"""

CLIENT_SCRUB = [
    ("Stanford grants access through three tiers", "Organizations grant access through three tiers"),
    ("Stanford\\u2019s policy", "your organization\\u2019s policy"),
    ("Build Stanford\\u2019s own conflicts", "Build your organization\\u2019s own conflicts"),
    ("ESS request 2958991", "security model export"),
    ("User-level SOD and SA analysis, Production", "User-level SOD analysis"),
    ("SU_REC_HIRING_MANAGER_JR", "HIRING_MANAGER_JR"),
]

PROV_BLOCK = """let PROV={
  conflict:{pretty:'—', source:'Not loaded'},
  model:{pretty:'—', source:'Not loaded', scope:'HCM application stripe only', roles:0, grants:0},
  drift_days:0
};"""

CFG_HELPERS = r"""
const CFG=JSON.parse(document.getElementById('warden-config').textContent);
function wardenUrl(path){
  const base=CFG.ordsBase.replace(/\/$/,'');
  const q=CFG.apiKey?('?api_key='+encodeURIComponent(CFG.apiKey)):'';
  return base+path+q;
}
async function wardenFetch(path,opts={}){
  const res=await fetch(wardenUrl(path),opts);
  return res;
}
"""

UNLOCK_BLOCK = r"""async function unlock(){
  const pw=document.getElementById('pw').value,err=document.getElementById('err');err.textContent='Unlocking\u2026';
  try{
    const res=await wardenFetch('/tenant/unlock',{method:'POST',headers:{'Content-Type':'application/json','api-key':CFG.apiKey||''},body:JSON.stringify({passphrase:pw})});
    const j=await res.json();
    if(!j.ok) throw new Error(j.error||'Incorrect access key.');
    D=j.data;
    if(j.prov) Object.assign(PROV,j.prov);
    document.getElementById('gate').style.display='none';document.getElementById('app').style.display='block';
    boot();
  }catch(e){err.textContent=e.message||'Incorrect access key.';}
}
document.getElementById('go').onclick=unlock;
document.getElementById('pw').addEventListener('keydown',e=>{if(e.key==='Enter')unlock();});"""

UNLOCK_IP_BLOCK = r"""async function unlockIP(){
  const inp=document.getElementById('ipkey'); if(!inp) return;
  const pw=inp.value, err=document.getElementById('iperr'); if(err) err.textContent='Unlocking\u2026';
  try{
    const res=await wardenFetch('/baseline/unlock',{method:'POST',headers:{'Content-Type':'application/json','api-key':CFG.apiKey||''},body:JSON.stringify({passphrase:pw})});
    const j=await res.json();
    if(!j.ok) throw new Error(j.error||'Incorrect baseline key.');
    D.baseline=j.baseline;
    document.getElementById('nb-rulecfg').textContent=(D.baseline||[]).filter(r=>r.status==='active').length;
    renderRuleConfig();
  }catch(e){ if(err) err.textContent=e.message||'Incorrect baseline key.'; }
}"""


def main() -> None:
    text = SRC.read_text(encoding="utf-8")

    text = re.sub(r"^const ENC=\{.*\};?\s*$", "", text, flags=re.M)
    text = re.sub(r"^const ENC_IP=\{.*\};?\s*$", "", text, flags=re.M)

    for old, new in CLIENT_SCRUB:
        text = text.replace(old, new)

    # Single client paragraph inside renderRuns — replace exactly, not with regex.
    text = text.replace(
        "Between the May and June extracts the two recruiting rules moved from 380 people each to 4,759 each. Every other rule in the ruleset stayed broadly flat. The whole movement traces to one role, <span class=\"mono\" style=\"color:var(--teal)\">HIRING_MANAGER_JR</span>, which grants both sides of both rules on its own and was provisioned to 4,588 people inside that window. It is not inherited from anything else in the security model, so it was granted directly.",
        "${D.runs.narrative || 'Movement detail is produced by the detection engine when a run comparison is loaded.'}",
    )

    text = re.sub(r"const PROV=\{[^;]+\};", PROV_BLOCK, text, count=1)

    unlock_start = text.index("async function unlock(){")
    unlock_end = text.index("document.getElementById('pw').addEventListener('keydown'")
    unlock_tail_end = text.index("});", unlock_end) + 3
    text = text[:unlock_start] + UNLOCK_BLOCK + text[unlock_tail_end:]

    ip_start = text.index("async function unlockIP(){")
    ip_end = text.index("function lockPrompt(", ip_start)
    text = text[:ip_start] + UNLOCK_IP_BLOCK + "\n" + text[ip_end:]

    text = text.replace(
        "const titles={home:'Command Center',people:'People by Risk',rules:'Conflict Rules',roles:'Roles by Risk',sensitive:'Sensitive Access',violations:'Violations',triage:'Review Queue',remediation:'Remediation',report:'Assessment Report',catalog:'Role & Privilege Reference',rulecfg:'Rule Configuration',config:'Configuration'};",
        "const titles={home:'Command Center',runs:'Run Comparison',people:'People by Risk',units:'Schools & Units',rules:'Conflict Rules',roles:'Roles by Risk',sensitive:'Sensitive Access',crossunit:'Cross-Unit Access',violations:'Violations',triage:'Review Queue',remediation:'Remediation',ledger:'Disposition Ledger',report:'Assessment Report',catalog:'Role & Privilege Reference',rulecfg:'Rule Configuration',config:'Configuration'};",
    )

    text = text.replace("<script>", CONFIG_BLOCK + "\n<script>\n" + CFG_HELPERS, 1)

    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(text, encoding="utf-8")
    print(f"Wrote {OUT} ({OUT.stat().st_size:,} bytes)")


if __name__ == "__main__":
    main()
