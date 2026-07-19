import cProfile, pstats, io
from app.api import episode_detail

pr = cProfile.Profile()
pr.enable()
ep = episode_detail("ep_fe1c5f7ebeb5")
pr.disable()

s = io.StringIO()
ps = pstats.Stats(pr, stream=s).sort_stats("cumulative")
ps.print_stats(30)
print(s.getvalue())
print("shots:", len(ep.get("shots") or []))
