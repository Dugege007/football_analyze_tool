from apifootball_daily import *
res={}
def odds_paged(tag, **p):
    page=1; allr=[]
    while True:
        b=get('odds',page=page,**p); allr+=b.get('response',[])
        if b.get('errors'): res[tag+'_err']=b['errors']
        tot=(b.get('paging') or {}).get('total',1)
        if page>=tot or page>=6: break
        page+=1
    save(f'odds_{tag}.json',allr); res[tag]=len(allr); return allr
b=get('odds/bookmakers'); save('odds_bookmakers.json',b.get('response',[])); res['bookmakers']=len(b.get('response',[]))
b=get('odds/bets'); save('odds_bets.json',b.get('response',[])); res['bets']=len(b.get('response',[]))
odds_paged('unl_2026', league=5, season=2026)
odds_paged('date_2026-10-07', date='2026-10-07')
b=get('standings',league=5,season=2026); save('standings_unl_2026.json',b); res['standings_unl']={'n':b.get('results'),'err':b.get('errors')}
print(json.dumps(res,ensure_ascii=False))
