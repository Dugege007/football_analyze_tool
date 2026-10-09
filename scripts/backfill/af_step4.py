from apifootball_daily import *
full={}
for d in ['2026-10-06','2026-10-07']:
    for x in json.load(open(f'{OUT}/fixtures_{d}_full.json'))['response']: full[x['fixture']['id']]=x
res={}; h={}
for fid in [1528952,1528953,1629006,1629007,1632359,1629005,1638603,1644165]:
    x=full[fid]; a=x['teams']['home']['id']; c=x['teams']['away']['id']
    if fid==1528952: b=json.load(open(f'{OUT}/h2h_probe_3-9_nolast.json'))
    else: b=get('fixtures/headtohead',h2h=f'{a}-{c}')
    ms=sorted(b.get('response',[]),key=lambda m:m['fixture']['date'],reverse=True)
    h[fid]={'home':x['teams']['home']['name'],'away':x['teams']['away']['name'],'errors':b.get('errors'),'matches':[{'date':m['fixture']['date'],'league':m['league']['name'],'status':m['fixture']['status']['short'],'home':m['teams']['home']['name'],'away':m['teams']['away']['name'],'goals':m['goals']} for m in ms]}
    res[fid]=len(ms)
save('h2h_sample_2026-10-06.json',h)
fo=json.load(open(f'{OUT}/odds_fixture_sample_2026-10-06.json'))
for fid in [1629006,1632359,1640517,1635591]:
    b=get('odds',fixture=fid); fo[str(fid)]=b.get('response',[]); res[f'odds_{fid}']=b.get('results')
save('odds_fixture_sample_2026-10-06.json',fo)
print(json.dumps(res))
