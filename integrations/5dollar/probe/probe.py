import os,sys,json,time,urllib.request,urllib.error,urllib.parse
BASE="https://api.5dollarfootballapi.com/v1/"
KEY=os.environ.get("FIVEDOLLAR_FOOTBALL_API_KEY", "").strip()
if not KEY:
    sys.exit("缺少 FIVEDOLLAR_FOOTBALL_API_KEY（见 config.example.env / docs/SENSITIVE.md）")
D=os.path.dirname(os.path.abspath(__file__))
LOG=os.path.join(D,"call_log.tsv")
def call(name,path,params=None):
    url=BASE+path+("?"+urllib.parse.urlencode(params) if params else "")
    req=urllib.request.Request(url,headers={"Authorization":"Bearer "+KEY,"Accept":"application/json"})
    t=time.time()
    try:
        r=urllib.request.urlopen(req,timeout=30); code=r.status; body=r.read(); h=r.headers
    except urllib.error.HTTPError as e:
        code=e.code; body=e.read(); h=e.headers
    el=time.time()-t
    if not os.path.exists(LOG):
        open(LOG,"w").write("ts\tname\tpath\thttp\telapsed_s\tlimit\tremaining\treset\n")
    open(LOG,"a").write(f"{time.strftime('%H:%M:%S')}\t{name}\t{path}?{urllib.parse.urlencode(params or {})}\t{code}\t{el:.2f}\t{h.get('X-RateLimit-Limit')}\t{h.get('X-RateLimit-Remaining')}\t{h.get('X-RateLimit-Reset')}\n")
    open(os.path.join(D,"raw",name+".json"),"wb").write(body)
    open(os.path.join(D,"raw",name+".hdr"),"w").write(f"HTTP {code}\n"+"".join(f"{k}: {v}\n" for k,v in h.items() if k.lower()!="set-cookie"))
    time.sleep(1.6)
    try: return code,json.loads(body),h
    except Exception: return code,body,h
if __name__=="__main__":
    name,path=sys.argv[1],sys.argv[2]
    params=dict(a.split("=",1) for a in sys.argv[3:])
    c,j,h=call(name,path,params)
    print(c,h.get('X-RateLimit-Limit'),h.get('X-RateLimit-Remaining'),h.get('X-RateLimit-Reset'))
    print(json.dumps(j,ensure_ascii=False)[:3000] if not isinstance(j,bytes) else j[:500])
