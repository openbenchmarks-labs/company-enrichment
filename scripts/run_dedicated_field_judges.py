#!/usr/bin/env python3
"""Checkpointed dedicated one-field judges for a firmographic snapshot."""
import argparse, concurrent.futures, json, os
from pathlib import Path
from dotenv import load_dotenv
from openai import OpenAI
from pydantic import BaseModel
from field_judge_prompts import MODEL, REASONING_EFFORT, PROMPTS, PROMPT_VERSION
from firmographic.common import SCORED_ATTRIBUTES, recompute_leaderboard, now_iso
class Verdict(BaseModel): case_slug:str; provider_present:bool; is_correct:bool; rationale:str
class Output(BaseModel): provider_slug:str; verdicts:list[Verdict]
ROOT=Path(__file__).resolve().parents[1]; SNAP=ROOT/'data/latest-firmographic.json'; OUT=ROOT/'data/firmographic/dedicated-field-judge-v1'
def val(n,f):
 if not n:return None
 if f=='hq_location':
  value={'country':n.get('hq_country'),'city':n.get('hq_city')}
  return value if any(value.values()) else None
 if f=='headcount_band':
  value={'min':n.get('headcount_min'),'max':n.get('headcount_max')}
  return value if any(v is not None for v in value.values()) else None
 if f=='industry': return [x for x in [n.get('industry'),*(n.get('industries') or [])] if x] or None
 if f=='primary_domain': return [x for x in [n.get('primary_domain'),*(n.get('domains') or [])] if x] or None
 return n.get(f)
def has_reference(case,f):
 if f=='linkedin_url':
  alts=case['reference'].get('linkedin_url_alternates') or []
  if isinstance(alts,str): alts=[x.strip() for x in alts.replace('\n',',').split(',') if x.strip()]
  return bool(case['reference'].get('linkedin_url') or alts)
 return val(case['reference'],f) not in (None,[],{})
def path(p,f): return OUT/'results'/p/f'{f}.json'
def work(s,p,f):
 q=path(p,f)
 if q.exists(): return
 runs={r['case_slug']:r for r in s['runs'] if r['provider_slug']==p}; rows=[]
 for c in s['cases']:
  ref=val(c['reference'],f)
  if ref in (None,[],{}): continue
  if f=='linkedin_url':
   alts=c['reference'].get('linkedin_url_alternates') or []
   if isinstance(alts,str): alts=[x.strip() for x in alts.replace('\n',',').split(',') if x.strip()]
   ref=[x for x in [c['reference'].get('linkedin_url'),*alts] if x]
  rows.append({'case_slug':c['case_slug'],'reference':ref,'provider_value':val(runs[c['case_slug']].get('normalized'),f),'alternate_primary_domains':c['reference'].get('alternate_primary_domains',[]),'reference_exact_headcount':c['reference'].get('headcount_exact')})
 client=OpenAI(api_key=os.environ['OPENAI_API_KEY'],max_retries=0,timeout=900)
 r=client.responses.parse(model=MODEL,reasoning={'effort':REASONING_EFFORT},input=[{'role':'system','content':PROMPTS[f]},{'role':'user','content':json.dumps({'provider_slug':p,'field':f,'cases':rows},separators=(',',':'))}],text_format=Output,max_output_tokens=50000,store=False)
 out=r.output_parsed
 if not out or out.provider_slug!=p or {x.case_slug for x in out.verdicts}!={x['case_slug'] for x in rows} or len(out.verdicts)!=len(rows): raise ValueError(f'bad output {p}/{f}')
 q.parent.mkdir(parents=True,exist_ok=True); q.write_text(json.dumps({'model':MODEL,'reasoning_effort':REASONING_EFFORT,'prompt_version':PROMPT_VERSION,'provider_slug':p,'field':f,'verdicts':[x.model_dump() for x in out.verdicts]},indent=2)+'\n')
def apply(s):
    cases={case['case_slug']:case for case in s['cases']}
    for p in {r['provider_slug'] for r in s['runs']}:
        for f in PROMPTS:
            d=json.loads(path(p,f).read_text()); verdict={x['case_slug']:x for x in d['verdicts']}
            for r in [x for x in s['runs'] if x['provider_slug']==p]:
                name='reference_correct_'+f
                r['metrics']=[m for m in r['metrics'] if m['metric_name']!=name]
                if r['case_slug'] not in verdict or not has_reference(cases[r['case_slug']],f): continue
                v=verdict[r['case_slug']]
                r['metrics'].append({'metric_name':name,'metric_value':int(v['is_correct']),'detail':{'judge_method':'openai_dedicated_field','judge_model':MODEL,'judge_reasoning_effort':REASONING_EFFORT,'judge_prompt_version':PROMPT_VERSION,'rationale':v['rationale']}})
    for r in s['runs']:
        metrics={m['metric_name']:m for m in r['metrics']}
        evaluable=[f for f in SCORED_ATTRIBUTES if f'reference_correct_{f}' in metrics]
        returned=[f for f in evaluable if metrics.get(f'coverage_{f}',{}).get('metric_value')==1]
        correct=sum(metrics[f'reference_correct_{f}']['metric_value'] for f in evaluable)
        correct_returned=sum(metrics[f'reference_correct_{f}']['metric_value'] for f in returned)
        r['metrics']=[m for m in r['metrics'] if m['metric_name'] not in {'attribute_coverage_pct','reference_accuracy_when_present_pct','correct_field_yield_pct'}]
        r['metrics'] += [
            {'metric_name':'attribute_coverage_pct','metric_value':round(100*sum(metrics.get(f'coverage_{f}',{}).get('metric_value',0) for f in SCORED_ATTRIBUTES)/len(SCORED_ATTRIBUTES),2),'detail':{'attribute_count':len(SCORED_ATTRIBUTES)}},
            {'metric_name':'reference_accuracy_when_present_pct','metric_value':round(100*correct_returned/len(returned),2) if returned else None,'detail':{'scored_field_count':len(SCORED_ATTRIBUTES)}},
            {'metric_name':'correct_field_yield_pct','metric_value':round(100*correct/len(evaluable),2) if evaluable else None,'detail':{'scored_field_count':len(SCORED_ATTRIBUTES)}}]
    s['leaderboard']=recompute_leaderboard(s['runs'],len(s['cases'])); s['updated_at']=now_iso(); s['dedicated_field_judge_version']=PROMPT_VERSION; SNAP.write_text(json.dumps(s,indent=2)+'\n')
def main():
 a=argparse.ArgumentParser();a.add_argument('--confirm-paid',action='store_true');a.add_argument('--workers',type=int,default=8);a.add_argument('--apply',action='store_true');z=a.parse_args();
 if not z.confirm_paid:a.error('--confirm-paid required')
 load_dotenv(ROOT/'.env.local'); s=json.loads(SNAP.read_text()); jobs=[(p,f) for p in sorted({r['provider_slug'] for r in s['runs']}) for f in PROMPTS]
 with concurrent.futures.ThreadPoolExecutor(max_workers=z.workers) as e:
  list(e.map(lambda x:work(s,*x),jobs))
 if z.apply: apply(s)
if __name__=='__main__':main()
