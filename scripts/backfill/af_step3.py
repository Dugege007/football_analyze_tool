from apifootball_daily import *
full={}
for d in ['2026-10-06','2026-10-07']:
    for x in json.load(open(f'{OUT}/fixtures_{d}_full.json'))['response']: full[x['fixture']['id']]=x
res={}
pg=1; allr=[]
while True:
    b=get('odds',date='2026-10-06',page=pg); allr+=b.get('response',[])
    tot=(b.get('paging') or {}).get('total',1)
    if pg>=tot or pg>=4: break
    pg+=1
save('odds_date_2026-10-06.json',allr); res['odds_10-06']=len(allr)
fo={}
for fid in [1528952,1528953,1545657,1629005,1638603]:
    b=get('odds',fixture=fid); fo[fid]=b.get('response',[]); res[f'odds_fx_{fid}']=(b.get('results'),b.get('errors'))
save('odds_fixture_sample_2026-10-06.json',fo)
h={}
for fid in [1528952,1528953,1629006,1629007,1632359,1629005,1638603,1644165]:
    x=full[fid]; a=x['teams']['home']['id']; c=x['teams']['away']['id']
    b=get('fixtures/headtohead',h2h=f'{a}-{c}',last=10)
    h[fid]={'home':x['teams']['home']['name'],'away':x['teams']['away']['name'],'errors':b.get('errors'),'matches':[{'date':m['fixture']['date'],'league':m['league']['name'],'home':m['teams']['home']['name'],'away':m['teams']['away']['name'],'goals':m['goals']} for m in b.get('response',[])]}
    res[f'h2h_{fid}']=(len(h[fid]['matches']),b.get('errors'))
save('h2h_sample_2026-10-06.json',h)
print(json.dumps(res,ensure_ascii=False))
