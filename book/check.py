import sys, html.parser
VOID={'br','hr','img','input','meta','link','area','base','col','embed','source','track','wbr','path','line','rect','circle','ellipse','polygon','polyline','stop','use'}
class P(html.parser.HTMLParser):
    def __init__(s): super().__init__(); s.st=[]; s.err=[]; s.ids={}
    def handle_starttag(s,t,a):
        d=dict(a)
        if 'id' in d:
            if d['id'] in s.ids: s.err.append(f"dup id {d['id']} line {s.getpos()[0]}")
            s.ids[d['id']]=1
        if t not in VOID: s.st.append((t,s.getpos()[0]))
    def handle_startendtag(s,t,a):
        d=dict(a)
        if 'id' in d: s.ids[d['id']]=1
    def handle_endtag(s,t):
        if t in VOID: return
        if not s.st: s.err.append(f"stray </{t}> line {s.getpos()[0]}"); return
        if s.st[-1][0]==t: s.st.pop(); return
        for i in range(len(s.st)-1,-1,-1):
            if s.st[i][0]==t:
                for x in s.st[i+1:]: s.err.append(f"unclosed <{x[0]}> line {x[1]} (closed by </{t}> line {s.getpos()[0]})")
                del s.st[i:]; return
        s.err.append(f"stray </{t}> line {s.getpos()[0]}")
bad=0
for f in sys.argv[1:]:
    p=P(); p.feed(open(f,encoding='utf-8').read()); p.close()
    for x in p.st: p.err.append(f"unclosed <{x[0]}> line {x[1]}")
    for e in p.err[:40]: print(f"{f}: {e}")
    bad+=len(p.err)
    print(f"{f}: {'OK' if not p.err else str(len(p.err))+' problems'}")
sys.exit(1 if bad else 0)
