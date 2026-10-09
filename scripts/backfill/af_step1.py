from apifootball_daily import *
summary={}
for d in ['2026-10-05','2026-10-06','2026-10-07']:
    b=get('fixtures',date=d,timezone='Asia/Shanghai')
    save(f'fixtures_{d}_full.json',b)
    slim=[{'id':x['fixture']['id'],'date':x['fixture']['date'],'status':x['fixture']['status']['short'],'league_id':x['league']['id'],'league':x['league']['name'],'country':x['league']['country'],'season':x['league']['season'],'round':x['league'].get('round'),'home':x['teams']['home']['name'],'away':x['teams']['away']['name'],'goals':x['goals'],'ht':x['score']['halftime']} for x in b.get('response',[])]
    save(f'fixtures_{d}_slim.json',slim)
    summary[d]={'n':len(slim),'errors':b.get('errors')}
print(json.dumps(summary,ensure_ascii=False))
